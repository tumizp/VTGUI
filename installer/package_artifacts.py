"""Collect Windows downloads for Actions, Releases and OCI archive storage."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    payload = ROOT / "dist" / "VTGUI"
    setup = ROOT / "dist" / f"VTGUI-{version}-Setup-x64.exe"
    if not (payload / "VTGUI.exe").is_file() or not setup.is_file():
        raise RuntimeError("Run build-installer.ps1 before collecting release files")
    files = sorted(payload.rglob("*"))
    for path in files:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError(f"Refusing to archive links: {path}")
        if path.suffix.lower() in {".db", ".db-shm", ".db-wal", ".log", ".cmd"} or path.name == "instance.lock":
            raise ValueError(f"Refusing to archive user data: {path}")
    output = ROOT / "dist" / "releases"
    output.mkdir(exist_ok=True)
    setup_copy = output / setup.name
    shutil.copy2(setup, setup_copy)
    portable = output / f"VTGUI-{version}-Portable-x64.zip"
    with zipfile.ZipFile(portable, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_file():
                archive.write(path, path.relative_to(payload.parent).as_posix())
    for path in (setup_copy, portable):
        path.with_suffix(path.suffix + ".sha256").write_text(f"{sha256(path)}  {path.name}\n", encoding="ascii")
    commit = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    info = output / "build-info.json"
    info.write_text(json.dumps({
        "application": "VTGUI", "version": version, "platform": "windows-x64",
        "commit": commit, "python": platform.python_version(),
        "run_url": (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                    f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                    if os.environ.get("GITHUB_RUN_ID") else None),
        "files": {p.name: sha256(p) for p in (setup_copy, portable)},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    downloads = [setup_copy, setup_copy.with_suffix(".exe.sha256"), portable,
                 portable.with_suffix(".zip.sha256"), info]
    (output / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in downloads), encoding="ascii")
    print(f"Windows release files: {output}")


if __name__ == "__main__":
    main()
