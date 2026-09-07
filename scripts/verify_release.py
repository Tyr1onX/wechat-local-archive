"""Check a tagged release artifact against the checkout and its manifest."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def verify_release(archive: Path) -> None:
    archive = archive.resolve()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    tag = f"v{version}"
    commit = git("rev-parse", "HEAD")
    if git("rev-parse", f"{tag}^{{commit}}") != commit:
        raise ValueError("Release tag does not point to HEAD")
    expected_name = f"wechat-local-archive-windows-x64-v{version}.zip"
    if archive.name != expected_name:
        raise ValueError("Unexpected release archive name")
    recorded = archive.with_name(archive.name + ".sha256").read_text(encoding="ascii").split()
    if recorded != [digest(archive.read_bytes()), archive.name]:
        raise ValueError("Release ZIP checksum mismatch")
    prefix = "wechat-local-archive-windows-x64/"
    with zipfile.ZipFile(archive) as source:
        info = json.loads(source.read(prefix + "build-info.json"))
        expected = {"version": version, "commit": commit, "tag": tag, "dirty": False}
        for field, value in expected.items():
            if info.get(field) != value or (field == "dirty" and info.get(field) is not False):
                raise ValueError(f"Release provenance mismatch: {field}: {info.get(field)!r} != {value!r}")
        for field, name in (("lock_sha256", "requirements-win-build.lock"), ("base_lock_sha256", "requirements-win.lock")):
            if info[field] != digest(subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)):
                raise ValueError(f"Release dependency lock mismatch: {name}")
        for line in source.read(prefix + "SHA256SUMS.txt").decode("utf-8").splitlines():
            expected, relative = line.split("  ", 1)
            with source.open(prefix + relative) as stream:
                hasher = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.hexdigest() != expected:
                raise ValueError(f"Release member checksum mismatch: {relative}")
    print(f"Release provenance: PASS ({tag}, {commit[:12]})")


if __name__ == "__main__":
    verify_release(Path(sys.argv[1]))
