"""Ordered filename rules and a preflighted, reversible rename transaction."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

from .archive import folder_snapshot, is_link


class RenameError(Exception):
    pass


RENAME_DEFAULTS = {"rename_season": "", "rename_episode": "", "rename_replacements": ""}


def number_rule(text: str, marker: str):
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(r"[+-][0-9]{1,6}", text):
        return "offset", int(text), 0
    if re.fullmatch(rf"(?i){marker}?[0-9]{{1,6}}", text):
        digits = text[1:] if text[0].upper() == marker else text
        return "set", int(digits), len(digits)
    raise ValueError(f"{'季号' if marker == 'S' else '集号'}规则格式应为 {marker}02、02、+1 或 -1。")


def replacement_rules(text: str):
    rules = []
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if "=>" not in line:
            raise ValueError(f"替换词第 {index} 行缺少 =>。")
        old, new = (part.strip() for part in line.split("=>", 1))
        if not old:
            raise ValueError(f"替换词第 {index} 行的原词不能为空。")
        if any(ch in old + new for ch in '\\/:*?"<>|') or any(ord(ch) < 32 for ch in old + new):
            raise ValueError(f"替换词第 {index} 行包含文件名不允许的字符。")
        rules.append((old, new))
    return rules


def validate_rename(values: dict) -> dict:
    rules = {key: str(values.get(key, default)).strip() for key, default in RENAME_DEFAULTS.items()}
    number_rule(rules["rename_season"], "S")
    number_rule(rules["rename_episode"], "E")
    replacement_rules(rules["rename_replacements"])
    return rules


def rename_enabled(values: dict) -> bool:
    return any(values.get(key, "").strip() for key in RENAME_DEFAULTS)


def valid_name(name: str):
    if not name or name in (".", "..") or name.endswith((" ", ".")):
        raise RenameError(f"结果名称为空或以点/空格结尾：{name!r}")
    if any(ch in name for ch in '\\/:*?"<>|') or any(ord(ch) < 32 for ch in name):
        raise RenameError(f"结果名称包含非法字符：{name!r}")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".", 1)[0]):
        raise RenameError(f"结果名称是 Windows 保留名称：{name}")
    if len(name.encode("utf-16-le")) // 2 > 255:
        raise RenameError("结果名称超过 255 个 UTF-16 字符")


def renamed_name(name: str, values: dict, is_directory=False) -> str:
    suffix = "" if is_directory else Path(name).suffix
    body = name[:-len(suffix)] if suffix else name
    for marker, key in (("S", "rename_season"), ("E", "rename_episode")):
        rule = number_rule(values.get(key, ""), marker)
        if not rule:
            continue
        mode, value, width = rule
        def change(match):
            result = int(match[2]) + value if mode == "offset" else value
            if not 0 <= result <= 999999:
                raise RenameError(f"{marker}{match[2]} 变换后超出 0～999999：{result}")
            return match[1] + str(result).zfill(max(2, len(match[2]), width))
        body = re.sub(rf"(?i)({marker})([0-9]{{1,6}})(?![0-9])", change, body)
    for old, new in replacement_rules(values.get("rename_replacements", "")):
        body = body.replace(old, new)
    if not body:
        raise RenameError(f"文件名主体不能为空：{name}")
    result = body + suffix
    valid_name(result)
    return result


def _key(path: Path) -> str:
    return str(path.absolute()).casefold()


def file_identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime_ns", 0)]


def _write_journal(path: Path, record: dict):
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def rename_items(source: Path, snapshots: dict[str, dict], loose: list[str], patterns: str,
                 rules: dict, log, cancelled=lambda: False, journal: Path | None = None,
                 destination: Path | None = None, folder_move=True, selected_files=None,
                 preserved_names=None, explicit_names=None, require_media=True,
                 allow_overwrite=True, merged_directories=None) -> dict[str, str]:
    """Preflight all names; rename children before parents; undo completed steps on error.

    Returns original absolute path -> final absolute path for every selected entry.
    The journal is retained for diagnosis after interruption; recovery is never guessed.
    """
    source = source.resolve()
    entries = {}  # Original path -> [kind, size, mtime_ns].
    for name, expected in snapshots.items():
        folder = Path(name)
        if folder.resolve().parent != source or is_link(folder):
            raise RenameError("只能重命名成品目录下的普通剧集目录")
        try:
            if folder_snapshot(folder, patterns, require_media) != expected:
                raise RenameError(f"剧集内容仍在变化：{folder}")
        except OSError as exc:
            raise RenameError(str(exc)) from exc
        entries.update({folder / relative: data for relative, data in expected.items()})
    for name in loose:
        path = Path(name)
        if path.resolve().parent != source or is_link(path) or not path.is_file():
            raise RenameError(f"散文件不在成品根目录或不是普通文件：{path}")
        stat = path.stat()
        entries[path] = ["file", stat.st_size, stat.st_mtime_ns]

    mapping = {}
    targets = {}
    basenames = {}
    preserved_names = preserved_names or set()
    explicit_names = explicit_names or {}
    for path, data in sorted(entries.items(), key=lambda item: len(item[0].parts)):
        parent = mapping.get(path.parent, path.parent)
        basenames[path] = explicit_names.get(_key(path)) or (path.name if _key(path) in preserved_names else renamed_name(path.name, rules, data[0] == "dir"))
        valid_name(basenames[path])
        target = parent / basenames[path]
        key = _key(target)
        if key in targets:
            raise RenameError(f"多个项目重命名后同名：{targets[key]} / {path} → {target}")
        targets[key] = path
        mapping[path] = target
    originals = {_key(p) for p in entries}
    existing = {}
    existing_paths = set()
    existing_directories = set()
    for path, target in mapping.items():
        if _key(target) in originals or not (target.exists() or target.is_symlink()):
            continue
        if is_link(target) or (entries[path][0] == "dir") != target.is_dir() or not (target.is_dir() or target.is_file()):
            raise RenameError(f"重命名目标类型冲突或包含链接：{target}")
        if target.is_file() and not allow_overwrite:
            raise RenameError(f"重命名目标已存在：{target}")
        found = folder_snapshot(target, patterns, require_media=False) if target.is_dir() else {".": ["file"]}
        for relative, data in found.items():
            current = target / relative
            existing[_key(current)] = file_identity(current)
            existing_paths.add(current)
            if data[0] == "dir":
                existing_directories.add(current)

    # Ordinary target directories can merge; occupied files are skipped by the mover.
    if destination is not None:
        destination = destination.resolve()
        check = [mapping[Path(p)] for p in snapshots] if folder_move else []
        for target in check:
            archive_target = destination / target.relative_to(source)
            if archive_target.exists() or archive_target.is_symlink():
                if is_link(archive_target) or not archive_target.is_dir():
                    raise RenameError(f"同名移动目标不是普通目录，未开始重命名：{archive_target}")

    groups = {}
    for path, data in entries.items():
        basename = basenames[path]
        if basename != path.name:
            groups.setdefault(path.parent, []).append((path, basename))
    steps = []
    token = uuid.uuid4().hex
    for parent, items in sorted(groups.items(), key=lambda item: len(item[0].parts), reverse=True):
        staged = []
        for path, basename in sorted(items):
            temporary = parent / f".vtgui-rename-{token}-{len(steps)}"
            steps.append((path, temporary))
            staged.append((temporary, parent / basename))
        steps.extend(staged)
    if not steps:
        return {str(p): str(target) for p, target in mapping.items()}
    record = {"status": "applying", "source": str(source), "rules": validate_rename(rules),
              "planned_steps": [[str(a), str(b)] for a, b in steps], "steps": [],
              "completed": 0, "pending": None, "mapping": {str(a): str(b) for a, b in mapping.items()},
              "merged_directories": sorted(str(p) for p in existing_directories), "backups": []}
    completed = []
    backups = []
    overwritten = []

    def move(old, new):
        if cancelled():
            raise InterruptedError("重命名已取消")
        if new.exists() or new.is_symlink():
            raise FileExistsError(f"执行时目标出现同名项目：{new}")
        record["pending"] = len(record["steps"])
        record["steps"].append([str(old), str(new)])
        if journal:
            _write_journal(journal, record)
        os.rename(old, new)
        completed.append((old, new))
        record.update(completed=len(completed), pending=None)
        if journal:
            _write_journal(journal, record)

    def backup(path):
        temporary = source / f".vtgui-rename-backup-{token}-{len(backups)}"
        backups.append(temporary)
        record["backups"].append(str(temporary))
        move(path, temporary)

    def apply(old, new):
        if cancelled():
            raise InterruptedError("重命名已取消")
        if new.exists() or new.is_symlink():
            if is_link(new) or existing.get(_key(new)) != file_identity(new):
                raise FileExistsError(f"执行时目标出现同名项目或发生变化：{new}")
            if old.is_dir() and new.is_dir():
                for child in sorted(old.iterdir()):
                    apply(child, new / child.name)
                # Keep the emptied directory so every operation remains reversible.
                backup(old)
                return
            if not old.is_file() or not new.is_file() or not allow_overwrite:
                raise FileExistsError(f"重命名目标类型冲突或已存在：{new}")
            backup(new)
            overwritten.append(new)
        move(old, new)

    try:
        if journal:
            _write_journal(journal, record)
        for old, new in steps:
            apply(old, new)
        # Verify the full result, including sidecars, before permitting any movement.
        for original, expected in entries.items():
            target = mapping[original]
            if is_link(target):
                raise OSError(f"发现链接：{target}")
            stat = target.stat()
            if (expected[0] == "dir" and not target.is_dir()) or (expected[0] == "file" and not target.is_file()):
                raise OSError(f"重命名时项目类型发生变化：{target}")
            if expected[0] == "file" and (stat.st_size, stat.st_mtime_ns) != tuple(expected[1:]):
                raise OSError(f"重命名时文件内容发生变化：{target}")
        for folder in snapshots:
            renamed_folder = mapping[Path(folder)]
            actual = folder_snapshot(renamed_folder, patterns, require_media=False)
            expected_paths = {str(mapping[p].relative_to(renamed_folder)) for p in entries if p == Path(folder) or Path(folder) in p.parents}
            retained = {str(p.relative_to(renamed_folder)) for p in existing_paths
                        if p == renamed_folder or renamed_folder in p.parents}
            if set(actual) != expected_paths | retained:
                raise OSError(f"重命名期间目录内容发生变化：{renamed_folder}")
        record["identities"] = {str(target): file_identity(target)
                                for target in set(mapping.values()) | existing_paths}
        record["status"] = "completed"
        if journal:
            _write_journal(journal, record)
    except Exception as exc:
        failures = []
        record["status"] = "rolling_back"
        for old, new in reversed(completed):
            try:
                if old.exists() or old.is_symlink():
                    raise FileExistsError(f"回滚路径已存在：{old}")
                os.rename(new, old)
            except OSError as rollback_error:
                failures.append(str(rollback_error))
        record.update(status="rollback_failed" if failures else "rolled_back", error=str(exc), rollback_errors=failures)
        if journal:
            try:
                _write_journal(journal, record)
            except OSError as journal_error:
                failures.append(str(journal_error))
        detail = "部分名称未能恢复，请查看重命名记录" if failures else "已恢复原名称"
        raise RenameError(f"重命名失败：{exc}；{detail}；未执行移动") from exc
    if merged_directories is not None:
        merged_directories.update(str(p) for p in existing_directories)
    for backup_path in backups:
        try:
            if backup_path.is_dir():
                backup_path.rmdir()
            else:
                backup_path.unlink()
        except OSError as exc:
            log(f"重命名已完成，覆盖备份暂未清理：{backup_path}：{exc}\n")
    for target in overwritten:
        log(f"已覆盖同名文件：{target}\n")
    for old, new in mapping.items():
        if old != new:
            log(f"已重命名：{old} → {new}\n")
    return {str(p): str(target) for p, target in mapping.items()}


def restore_unmoved(source: Path, mapping: dict[str, str], patterns: str, log, journal: Path,
                    merged_directories=None):
    """Restore original names only for items still at source after a merge/skip.

    Already moved items are absent and never touched. This keeps the next download
    from colliding with renamed leftovers or receiving the same offset twice.
    """
    source = source.resolve()
    inverse = {Path(new): Path(old) for old, new in mapping.items()}
    roots = [Path(new) for old, new in mapping.items() if Path(old).parent == source]
    snapshots, loose = {}, []
    retained_directories = {Path(p) for p in (merged_directories or ())}
    for root in roots:
        if not root.exists():
            continue
        if is_link(root):
            raise RenameError(f"未移动项目变为链接，无法恢复原名：{root}")
        if root.is_dir():
            snapshot = folder_snapshot(root, patterns, require_media=False)
            if any(root / relative not in inverse and not any(
                    directory == root / relative or directory in (root / relative).parents
                    for directory in retained_directories) for relative in snapshot):
                raise RenameError(f"源目录出现其他新项目，暂不恢复名称：{root}")
            snapshots[str(root)] = snapshot
        else:
            loose.append(str(root))
    if not snapshots and not loose:
        return
    names = {_key(new): old.name for new, old in inverse.items()}
    # Existing series directories and their older episodes keep their names.
    names.update({_key(p): p.name for p in retained_directories})
    try:
        rename_items(source, snapshots, loose, patterns, {}, log, journal=journal,
                     explicit_names=names, require_media=False, allow_overwrite=False)
        log("未移动的同名/失败项目已恢复源名称，已移入目标的文件保持新名称。\n")
    except (RenameError, OSError) as exc:
        raise RenameError(f"移动后恢复剩余源名称失败；已移动文件仍保留在目标目录：{exc}") from exc
