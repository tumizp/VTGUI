"""Whole-series archiving, with a staged copy for cross-volume moves."""
from __future__ import annotations

import errno
import fnmatch
import os
from pathlib import Path
import shutil
import uuid
from typing import Callable


TEMP_SUFFIXES = (".part", ".partial", ".tmp", ".temp", ".download", ".downloading", ".crdownload", ".aria2")


def is_link(path: Path) -> bool:
    stat = path.lstat()
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def folder_snapshot(folder: Path, patterns: str, require_media=True) -> dict[str, list]:
    """Inspect every entry, including sidecars and empty directories, without following links."""
    if is_link(folder) or not folder.is_dir():
        raise OSError("剧集目录不存在或是链接目录")
    found = {}
    media = False
    globs = [p.strip().lower() for p in patterns.split(";") if p.strip()] if require_media else []
    pending = [folder]
    while pending:
        path = pending.pop()
        if is_link(path):
            raise OSError(f"包含链接或重解析点：{path.name}")
        if path.name.lower().endswith(TEMP_SUFFIXES):
            raise OSError(f"包含未完成的临时下载项：{path.name}")
        stat = path.stat()
        relative = str(path.relative_to(folder))
        if path.is_dir():
            found[relative] = ["dir", 0, stat.st_mtime_ns]
            pending.extend(path.iterdir())
        elif path.is_file():
            found[relative] = ["file", stat.st_size, stat.st_mtime_ns]
            if any(fnmatch.fnmatch(path.name.lower(), pattern) for pattern in globs):
                if not stat.st_size:
                    raise OSError(f"包含空的成品文件：{path.name}")
                media = True
        else:
            raise OSError(f"包含非常规文件：{path.name}")
    if require_media and not media:
        raise OSError("目录内没有匹配通配符的非空成品")
    return found


def archive_plan(source: Path, files: list[str]) -> tuple[list[Path], list[str]]:
    """Use the first directory below the configured downloads root as a series."""
    source = source.resolve()
    folders, loose = set(), set()
    for name in files:
        path = Path(name).resolve()
        if source not in path.parents:
            continue
        relative = path.relative_to(source)
        if len(relative.parts) > 1:
            folders.add(source / relative.parts[0])
        else:
            loose.add(str(path))
    return sorted(folders), sorted(loose)


def _copy_folder(folder: Path, stage: Path, snapshot: dict[str, list], cancelled):
    stage.mkdir()
    for name, (kind, _, _) in sorted(snapshot.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
        if name == ".":
            continue
        if cancelled():
            raise InterruptedError("归档已取消，源目录保留")
        src, dst = folder / name, stage / name
        if is_link(src) or folder not in src.resolve().parents:
            raise OSError(f"复制时发现链接或范围外路径：{name}")
        if kind == "dir":
            dst.mkdir()
        else:
            with src.open("rb") as reader, dst.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError("归档已取消，源目录保留")
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            shutil.copystat(src, dst)
    for name, (kind, _, _) in sorted(snapshot.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
        if kind == "dir":
            shutil.copystat(folder / name, stage / name)


def _remove_copied_source(folder: Path, snapshot: dict[str, list], cancelled):
    # Remove only manifest entries. A concurrently added file must never be recursively deleted.
    for name, (kind, size, modified) in snapshot.items():
        if kind != "file":
            continue
        if cancelled():
            raise InterruptedError("目标副本已完成；清理已取消，剩余源文件保留")
        path = folder / name
        stat = path.stat()
        if is_link(path) or folder not in path.resolve().parents or (stat.st_size, stat.st_mtime_ns) != (size, modified):
            raise OSError("清理时源文件发生变化；目标副本及剩余源文件保留")
        path.unlink()
    for name, (kind, _, _) in sorted(snapshot.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
        if kind == "dir" and name != ".":
            (folder / name).rmdir()
    folder.rmdir()


def target_directory(base: Path, path: Path):
    """Create descendants one at a time without traversing a target junction/link."""
    base.mkdir(parents=True, exist_ok=True)
    current = base
    for part in path.relative_to(base).parts:
        current /= part
        if current.exists() or current.is_symlink():
            if is_link(current) or not current.is_dir():
                raise OSError(f"目标目录存在文件或链接冲突：{current}")
        else:
            current.mkdir()


def move_files(source: Path, destination: Path, files: list[str], log: Callable[[str], None],
               cancelled: Callable[[], bool] = lambda: False, *, preserve_relative_paths=False) -> int:
    """Move files directly into destination; folder merges retain their tree."""
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("源目录和目标目录互相包含")
    count = 0
    for name in files:
        if cancelled():
            break
        path = Path(name)
        real = path.resolve()
        if path.is_symlink() or source not in real.parents or not real.is_file():
            log(f"跳过范围外或不存在的文件：{name}\n")
            continue
        if any(is_link(p) for p in [path, *path.parents] if p != source.parent):
            log(f"跳过链接路径：{name}\n")
            continue
        target = destination / (real.relative_to(source) if preserve_relative_paths else real.name)
        temp = None
        reserved = False
        try:
            initial = real.stat()
            if initial.st_size == 0:
                log(f"跳过空文件：{name}\n")
                continue
            target_directory(destination, target.parent)
            if target.exists() or target.is_symlink():
                raise FileExistsError(str(target))
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
            with real.open("rb") as src, temp.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError("移动已取消，源文件保留")
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            current = real.stat()
            if (initial.st_size, initial.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                raise OSError("复制期间源文件仍在变化，源文件保留")
            shutil.copystat(real, temp)
            with target.open("xb"):
                reserved = True
            os.replace(temp, target)
            reserved = False
            real.unlink()
            count += 1
            log(f"已移动：{real} → {target}\n")
        except FileExistsError:
            log(f"同名文件或项目已存在，跳过并保留源文件：{target}\n")
        except OSError as exc:
            log(f"移动失败，源文件保留：{real}：{exc}\n")
        finally:
            if temp:
                temp.unlink(missing_ok=True)
            if reserved:
                target.unlink(missing_ok=True)
    return count


def merge_folder(folder: Path, target: Path, expected: dict, patterns: str, log, cancelled,
                 require_media=True) -> int:
    if is_link(target) or not target.is_dir():
        raise OSError(f"同名目标不是普通目录，保留源目录：{target}")
    if folder_snapshot(folder, patterns, require_media) != expected:
        raise OSError("合并前源目录发生变化，保留源目录")
    for name, (kind, _, _) in expected.items():
        if cancelled():
            return 0
        if kind == "dir":
            try:
                target_directory(target, target / name)
            except OSError as exc:
                log(f"跳过子目录冲突：{exc}\n")
    files = [str(folder / name) for name, data in expected.items() if data[0] == "file"]
    moved = move_files(folder, target, files, log, cancelled, preserve_relative_paths=True)
    # rmdir removes only empty source directories. Duplicate and failed files remain.
    if not cancelled():
        for name, data in sorted(expected.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
            if data[0] == "dir" and name != ".":
                try:
                    (folder / name).rmdir()
                except OSError:
                    pass
        try:
            folder.rmdir()
        except OSError:
            pass
    log(f"已合并剧集文件夹：{folder} → {target}；移入 {moved} 个文件；同名或未移动文件留在源目录\n")
    return moved


def move_folders(source: Path, destination: Path, snapshots: dict[str, dict], patterns: str,
                 log: Callable[[str], None], cancelled=lambda: False, require_media=True) -> int:
    """Merge an existing series; use rename or a staged copy for a new series."""
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("源目录和目标目录互相包含")
    count = 0
    for name, expected in snapshots.items():
        if cancelled():
            break
        folder = Path(name)
        target = destination / folder.name
        stage = None
        published = False
        try:
            real = folder.resolve()
            if real.parent != source or is_link(folder):
                raise OSError("剧集目录必须是成品目录下的一级普通目录")
            if folder_snapshot(folder, patterns, require_media) != expected:
                raise OSError("剧集目录内容仍在变化，源目录保留")
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / folder.name
            if target.exists() or target.is_symlink():
                moved = merge_folder(folder, target, expected, patterns, log, cancelled, require_media)
                count += int(moved > 0 or not folder.exists())
                continue
            if cancelled():
                break
            try:
                # On Windows rename never replaces an existing target directory.
                os.rename(folder, target)
            except OSError as exc:
                if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                    raise
                stage = destination / f".{folder.name}.{uuid.uuid4().hex}.partial"
                _copy_folder(folder, stage, expected, cancelled)
                if folder_snapshot(folder, patterns, require_media) != expected:
                    raise OSError("复制期间源目录发生变化，未移动源目录")
                if cancelled():
                    raise InterruptedError("归档已取消，源目录保留")
                if target.exists() or target.is_symlink():
                    raise FileExistsError("复制期间目标出现同名目录")
                os.rename(stage, target)
                published = True
                if folder_snapshot(folder, patterns, require_media) != expected:
                    raise OSError("目标副本已完成；源目录发生变化，暂不清理")
                _remove_copied_source(folder, expected, cancelled)
            count += 1
            log(f"已移动剧集文件夹：{folder} → {target}\n")
        except FileExistsError:
            if target.is_dir() and not is_link(target):
                try:
                    moved = merge_folder(folder, target, expected, patterns, log, cancelled, require_media)
                    count += int(moved > 0 or not folder.exists())
                except OSError as exc:
                    log(f"剧集目录合并失败，剩余源文件保留：{folder}：{exc}\n")
            else:
                log(f"同名目标不是普通目录，保留源目录：{folder.name}\n")
        except OSError as exc:
            log(f"剧集文件夹归档未完成：{folder}：{exc}{'；目标副本已保留' if published else '；源目录保留'}\n")
        finally:
            if stage and stage.exists() and stage.parent.resolve() == destination and stage.name.endswith(".partial"):
                # This uniquely named directory contains only copies created by this attempt.
                shutil.rmtree(stage, ignore_errors=True)
    return count
