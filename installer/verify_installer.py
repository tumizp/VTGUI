"""Explicit integration check: installs into .qa, then removes its own installation.

Run on a Windows account without VTGUI installed. Existing installations and
shortcuts are never overwritten by this test. No real DRMLAB command is run.
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import winreg


ROOT = Path(__file__).resolve().parents[1]
KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VTGUI"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/VTGUI"


def registry_exists():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            return True
    except FileNotFoundError:
        return False


def install(setup, destination, expected=0):
    # NSIS /D must be the last parameter, without quotes even for paths with spaces.
    command = subprocess.list2cmdline([str(setup), "/S"]) + " /D=" + str(destination)
    result = subprocess.run(command, timeout=60)
    assert result.returncode == expected, f"Installer returned {result.returncode}, expected {expected}"


def uninstall(directory, scratch):
    uninstaller = scratch / "test-uninstaller.exe"
    shutil.copy2(directory / "Uninstall.exe", uninstaller)
    # Running a copy with _?= allows waiting for the actual uninstaller exit code.
    command = subprocess.list2cmdline([str(uninstaller), "/S"]) + " _?=" + str(directory)
    subprocess.run(command, check=True, timeout=30)


def main(setup):
    if registry_exists() or START_MENU.exists():
        raise RuntimeError("VTGUI installation or shortcuts already exist; use a clean Windows account for this check")
    scratch_parent = ROOT / ".qa"
    scratch_parent.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="installer 中文 ", dir=scratch_parent))
    destination = scratch / "应用 安装目录"
    installed = False
    try:
        occupied = scratch / "已有文件的目录"
        occupied.mkdir()
        sentinel = occupied / "keep.txt"
        sentinel.write_text("do not overwrite", encoding="utf-8")
        install(setup, occupied, expected=2)
        assert sentinel.read_text() == "do not overwrite"
        assert not (occupied / "VTGUI.exe").exists()
        assert not registry_exists()
        print("PASS: non-empty unrelated directory rejected", flush=True)

        install(setup, destination)
        installed = True
        assert registry_exists()
        assert (START_MENU / "VTGUI.lnk").is_file()
        for source in (ROOT / "dist/VTGUI").rglob("*"):
            if source.is_file():
                target = destination / source.relative_to(ROOT / "dist/VTGUI")
                assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest(), target
        print("PASS: Chinese/spaced installation path, all payload hashes and start menu", flush=True)

        extra = destination / "用户自己添加的文件.txt"
        extra.write_text("keep after upgrade and uninstall", encoding="utf-8")
        data = destination / "user-data"
        data.mkdir()
        (data / "keep.txt").write_text("preserved", encoding="utf-8")
        exe = destination / "VTGUI.exe"
        subprocess.run([str(exe), "--smoke-test", "--data-dir", str(data)], check=True, timeout=15)
        assert (data / "vtgui.db").is_file()
        assert not (data / "startup-error.log").exists()
        print("PASS: installed GUI launches without a console", flush=True)

        running = subprocess.Popen([str(exe), "--data-dir", str(data)], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            time.sleep(1)
            assert running.poll() is None
            install(setup, destination, expected=2)
        finally:
            running.terminate()
            running.wait(timeout=10)
        print("PASS: upgrade blocked while installed GUI is running", flush=True)

        install(setup, destination)
        assert extra.read_text(encoding="utf-8") == "keep after upgrade and uninstall"
        assert (data / "keep.txt").read_text() == "preserved"
        assert (data / "vtgui.db").is_file()
        print("PASS: same-directory upgrade preserves user files and configuration", flush=True)

        environment = dict(os.environ, VTGUI_TEST_EXE=str(exe))
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_packaged.py", "-v"],
                       cwd=ROOT, env=environment, check=True, timeout=90)
        print("PASS: installed executable command/retry/episodes/rename/merge tests", flush=True)

        uninstall(destination, scratch)
        installed = False
        assert not (destination / "VTGUI.exe").exists()
        assert not (destination / "_internal").exists()
        assert not START_MENU.exists()
        assert not registry_exists()
        assert extra.read_text(encoding="utf-8") == "keep after upgrade and uninstall"
        assert (data / "vtgui.db").is_file()
        assert (data / "keep.txt").read_text() == "preserved"
        print("PASS: uninstall removes program/shortcuts/registration, preserves user data", flush=True)
    finally:
        if installed and (destination / "Uninstall.exe").is_file():
            uninstall(destination, scratch)
        print(f"Verification artifacts: {scratch}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=Path)
    main(parser.parse_args().installer.resolve(strict=True))
