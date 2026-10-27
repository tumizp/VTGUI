"""Project identification and the shared DRMLAB/unshackle output protocol."""
from __future__ import annotations

from pathlib import Path
import re


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FINAL = re.compile(r"^===VT_FINAL_(DIR|FILE)===\s+(.+)$")


def project_name(root: str) -> str:
    path = Path(root)
    if (path / "unshackle").is_dir():
        return "unshackle"
    if (path / "vinetrimmer").is_dir():
        return "DRMLAB"
    return "命令项目"


class FinalOutputs:
    """Collect complete marker lines even when process reads split them."""
    def __init__(self, root: str):
        self.root = Path(root)
        self.pending = ""
        self.seen = False
        self.directories: set[Path] = set()
        self.files: set[Path] = set()

    def feed(self, text: str, final=False):
        self.pending += text
        lines = self.pending.split("\n")
        self.pending = lines.pop()
        if final:
            lines.append(self.pending)
            self.pending = ""
        for line in lines:
            match = FINAL.fullmatch(ANSI.sub("", line).strip())
            if not match:
                continue
            self.seen = True
            value = match[2].strip()
            if not value or "\x00" in value:
                continue
            path = Path(value).expanduser()
            try:
                path = (path if path.is_absolute() else self.root / path).resolve()
            except (OSError, ValueError):
                continue
            (self.directories if match[1] == "DIR" else self.files).add(path)

    def select(self, candidates: list[str]) -> list[str]:
        # Candidates already passed the configured source/pattern/snapshot checks.
        if not self.seen:
            return candidates
        return [name for name in candidates if Path(name) in self.files or
                any(directory in Path(name).parents for directory in self.directories)]
