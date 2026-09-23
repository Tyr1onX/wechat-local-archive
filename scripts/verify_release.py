"""Check a tagged standalone executable against the release checksum."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def verify_release(executable: Path) -> None:
    executable = executable.resolve()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    tag = f"v{version}"
    commit = git("rev-parse", "HEAD")
    if git("rev-parse", f"{tag}^{{commit}}") != commit:
        raise ValueError("Release tag does not point to HEAD")
    if executable.name != "WeChatLocalArchive.exe":
        raise ValueError("Unexpected release executable name")
    if not executable.is_file():
        raise ValueError("Release executable is missing")
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Release executable is not a Windows PE file")

    checksum_path = executable.with_name(executable.name + ".sha256")
    recorded = checksum_path.read_text(encoding="ascii").split()
    if recorded != [digest(executable), executable.name]:
        raise ValueError("Release executable checksum mismatch")

    if sys.platform == "win32":
        result = subprocess.run(
            [str(executable), "web", "--smoke"],
            cwd=executable.parent,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"Standalone release smoke failed with exit {result.returncode}")

    print(f"Release provenance: PASS ({tag}, {commit[:12]})")


if __name__ == "__main__":
    verify_release(Path(sys.argv[1]))
