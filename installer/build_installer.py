"""Build the offline Windows installer from a PyInstaller application bundle."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NSIS_VERSION = "3.12"
# Published by the official NSIS SourceForge download page:
# https://sourceforge.net/projects/nsis/files/NSIS%203/3.12/nsis-3.12.zip/download
NSIS_SHA256 = "56581f90db321581c5381193d796fffcf2d24b2f8fed2160a6c6a3baa67f2c4f"
NSIS_URL = f"https://downloads.sourceforge.net/project/nsis/NSIS%203/{NSIS_VERSION}/nsis-{NSIS_VERSION}.zip"


def compiler_path(explicit):
    if explicit:
        result = Path(explicit).resolve(strict=True)
        if not result.is_file():
            raise ValueError("--compiler must name makensis.exe")
        return result
    cache = ROOT / ".tools"
    result = cache / f"nsis-{NSIS_VERSION}" / "makensis.exe"
    if result.is_file():
        return result
    cache.mkdir(exist_ok=True)
    archive = cache / f"nsis-{NSIS_VERSION}.zip"
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != NSIS_SHA256:
        print(f"Downloading official NSIS {NSIS_VERSION} (build tool only)...", flush=True)
        with urllib.request.urlopen(NSIS_URL, timeout=60) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != NSIS_SHA256:
            raise RuntimeError("NSIS download SHA-256 mismatch; refusing to run the compiler")
        archive.write_bytes(payload)
    with zipfile.ZipFile(archive) as package:
        package.extractall(cache)
    return result


def nsis_literal(value):
    return str(value).replace("$", "$$").replace('"', '$\\"')


def build(compiler=None):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Installer version must contain three numeric components")
    payload = ROOT / "dist" / "VTGUI"
    if not (payload / "VTGUI.exe").is_file() or not list((payload / "_internal").glob("python3*.dll")):
        raise RuntimeError("Build dist/VTGUI first using build.ps1")
    shutil.copy2(ROOT / "README.md", payload / "README.md")
    shutil.copy2(ROOT / "installer" / "使用说明.txt", payload / "使用说明.txt")
    licenses = payload / "licenses"
    licenses.mkdir(exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Missing Python runtime license: {python_license}")
    shutil.copy2(python_license, licenses / "Python.txt")
    import PyInstaller
    pyinstaller_root = Path(PyInstaller.__file__).resolve().parent.parent
    pyinstaller_license = next(pyinstaller_root.glob("pyinstaller-*.dist-info/licenses/COPYING.txt"), None)
    if pyinstaller_license is None:
        raise RuntimeError("Missing PyInstaller redistribution license")
    shutil.copy2(pyinstaller_license, licenses / "PyInstaller.txt")
    tk_license = Path(sys.base_prefix) / "tcl"
    tk_license = next(tk_license.glob("tk*/demos/license.terms"), None)
    if tk_license is None:
        raise RuntimeError("Missing Tcl/Tk runtime license")
    shutil.copy2(tk_license, licenses / "Tcl-Tk.txt")
    compiler_exe = compiler_path(compiler)
    nsis_license = compiler_exe.parent / "COPYING"
    if not nsis_license.is_file():
        raise RuntimeError(f"Missing NSIS redistribution license: {nsis_license}")
    shutil.copy2(nsis_license, licenses / "NSIS.txt")
    output = ROOT / "dist" / f"VTGUI-{version}-Setup-x64.exe"
    generated = ROOT / "build" / "installer"
    generated.mkdir(parents=True, exist_ok=True)

    # Uninstall only files shipped by this build; never recursively delete a
    # user-selected directory or any files the user added after installation.
    files = sorted(p for p in payload.rglob("*") if p.is_file())
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in payload.rglob("*")):
        raise ValueError("Installer payload cannot contain links")
    for path in files:
        if path.suffix.lower() in {".db", ".log", ".cmd"} or path.name in {"startup-error.log", "instance.lock"}:
            raise ValueError(f"Refusing to include user data in installer: {path.name}")
    directives = [f'  Delete "$INSTDIR\\{nsis_literal(p.relative_to(payload))}"' for p in files]
    directories = sorted((p for p in payload.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
    directives.extend(f'  RMDir "$INSTDIR\\{nsis_literal(p.relative_to(payload))}"' for p in directories)
    manifest = generated / "uninstall-files.nsh"
    manifest.write_text("\n".join(directives) + "\n", encoding="utf-8-sig")
    size_kb = (sum(p.stat().st_size for p in files) + 1023) // 1024
    command = [str(compiler_exe), "/V2", "/INPUTCHARSET", "UTF8",
               f"/DAPP_VERSION={version}", f"/DPAYLOAD_DIR={payload}", f"/DOUTPUT_FILE={output}",
               f"/DUNINSTALL_MANIFEST={manifest}", f"/DESTIMATED_SIZE={size_kb}",
               str(ROOT / "installer" / "vtgui.nsi")]
    subprocess.run(command, check=True, cwd=ROOT)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".exe.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"Created: {output}\nSize: {output.stat().st_size / 1024 / 1024:.2f} MiB\nSHA-256: {digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", help="Path to an existing makensis.exe; otherwise use a verified local download")
    build(parser.parse_args().compiler)
