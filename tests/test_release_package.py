"""Verify that release downloads are self-contained and exclude local data."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from installer import package_artifacts


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="VTGUI-release-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
        self.payload = self.root / "dist" / "VTGUI"
        (self.payload / "_internal").mkdir(parents=True)
        (self.payload / "VTGUI.exe").write_bytes(b"application")
        (self.payload / "_internal" / "python314.dll").write_bytes(b"runtime")
        (self.root / "dist" / "VTGUI-1.2.3-Setup-x64.exe").write_bytes(b"installer")
        self.output = self.root / "dist" / "releases"
        root_patch = patch.object(package_artifacts, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        env_patch = patch.dict("os.environ", {"GITHUB_SHA": "a" * 40,
                                             "GITHUB_REPOSITORY": "example/VTGUI", "GITHUB_RUN_ID": "42"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_downloads_include_runtime_and_verifiable_checksums(self):
        package_artifacts.main()
        with zipfile.ZipFile(self.output / "VTGUI-1.2.3-Portable-x64.zip") as archive:
            self.assertEqual(set(archive.namelist()), {"VTGUI/VTGUI.exe", "VTGUI/_internal/python314.dll"})
            self.assertEqual(archive.read("VTGUI/_internal/python314.dll"), b"runtime")
        checksums = (self.output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
        self.assertEqual(len(checksums), 5)
        for line in checksums:
            digest, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
        info = json.loads((self.output / "build-info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["commit"], "a" * 40)
        self.assertTrue(info["run_url"].endswith("/example/VTGUI/actions/runs/42"))

    def test_user_data_is_rejected_before_writing_downloads(self):
        for name in ("vtgui.db", "vtgui.db-wal", "vtgui.db-shm", "task.log", "task.cmd", "instance.lock"):
            with self.subTest(name=name):
                private = self.payload / name
                private.write_bytes(b"local private data")
                with self.assertRaises(ValueError):
                    package_artifacts.main()
                self.assertFalse(self.output.exists())
                private.unlink()

    def test_missing_installer_is_rejected(self):
        (self.root / "dist" / "VTGUI-1.2.3-Setup-x64.exe").unlink()
        with self.assertRaises(RuntimeError):
            package_artifacts.main()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
