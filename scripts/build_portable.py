"""Build standalone Windows onefile executables with a traceable checksum."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_EXE = "WeChatLocalArchive.exe"
CLI_EXE = "WeChatLocalArchiveCLI.exe"
LEGACY_DIR = "wechat-local-archive-windows-x64"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def committed_digest(commit: str, name: str) -> str:
    """Hash the committed lock, independent of Windows checkout line endings."""
    data = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
    return hashlib.sha256(data).hexdigest()


def tracked_source_is_dirty() -> bool:
    """Check actual tracked content, including staged changes, not Git's stat cache."""
    result = subprocess.run(
        ["git", "diff", "--quiet", "--exit-code", "HEAD", "--"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Unable to check source changes: git exited {result.returncode}")
    return result.returncode == 1


def _release_tag(version: str, commit: str) -> str | None:
    expected = f"v{version}"
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")
        if tag != expected:
            raise RuntimeError(f"Release ref {tag!r} does not match {expected}")
        if git("rev-parse", f"{tag}^{{commit}}") != commit:
            raise RuntimeError("Release tag does not point to the checked-out commit")
        return tag
    tags = set(git("tag", "--points-at", "HEAD").splitlines())
    return expected if expected in tags else None


def _clean_previous_outputs(out_dir: Path) -> None:
    for path in (
        out_dir / LEGACY_DIR,
        out_dir / MAIN_EXE,
        out_dir / CLI_EXE,
        out_dir / f"{MAIN_EXE}.sha256",
    ):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def build(out_dir: Path) -> Path:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Portable builds require 64-bit Windows")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Portable builds require Python 3.12")

    version = metadata.version("wechat-local-archive")
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if version != expected:
        raise RuntimeError(f"Expected project version {expected}, got {version}; reinstall the project")
    if importlib.util.find_spec("faster_whisper") is not None:
        raise RuntimeError("Use a clean base build environment without optional Whisper")

    lock_text = (ROOT / "requirements-win-build.lock").read_text(encoding="utf-8")
    for name, required in re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", lock_text, re.M):
        actual = metadata.version(name)
        if actual != required:
            raise RuntimeError(f"Build dependency mismatch: {name} {actual} != {required}")

    commit = git("rev-parse", "HEAD")
    epoch = int(git("show", "-s", "--format=%ct", "HEAD"))
    dirty = tracked_source_is_dirty()
    tag = _release_tag(version, commit)
    if tag and dirty:
        raise RuntimeError("Tagged release source is dirty; commit all changes before building")

    lock_digest = (
        committed_digest(commit, "requirements-win-build.lock")
        if not dirty else digest(ROOT / "requirements-win-build.lock")
    )
    base_lock_digest = (
        committed_digest(commit, "requirements-win.lock")
        if not dirty else digest(ROOT / "requirements-win.lock")
    )
    print(
        "Build provenance: "
        f"version={version}, commit={commit}, tag={tag}, dirty={dirty}, "
        f"build_lock={lock_digest}, base_lock={base_lock_digest}",
        flush=True,
    )

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean_previous_outputs(out_dir)
    (ROOT / "work").mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="portable-build-", dir=str(ROOT / "work")) as temporary:
        work = Path(temporary)
        env = dict(
            os.environ,
            SOURCE_DATE_EPOCH=str(epoch),
            PYTHONHASHSEED="0",
            PYINSTALLER_CONFIG_DIR=str(work / "pyinstaller-cache"),
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--distpath",
                str(out_dir),
                "--workpath",
                str(work / "build"),
                str(ROOT / "scripts" / "portable.spec"),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )

    main_exe = out_dir / MAIN_EXE
    cli_exe = out_dir / CLI_EXE
    _check_outputs(main_exe, cli_exe)

    checksum = digest(main_exe)
    checksum_path = out_dir / f"{MAIN_EXE}.sha256"
    checksum_path.write_text(f"{checksum}  {MAIN_EXE}\n", encoding="ascii")
    print(f"Standalone: {main_exe}")
    print(f"CI helper: {cli_exe}")
    print(f"SHA256: {checksum}")
    return main_exe


def _check_outputs(main_exe: Path, cli_exe: Path) -> None:
    for path in (main_exe, cli_exe):
        if not path.is_file():
            raise RuntimeError(f"Missing onefile launcher: {path.name}")
        if path.stat().st_size < 1024 * 1024:
            raise RuntimeError(f"Unexpectedly small onefile launcher: {path.name}")
    legacy = main_exe.parent / LEGACY_DIR
    if legacy.exists():
        raise RuntimeError("Legacy onedir output still exists")
    if any(path.name.casefold() == "_internal" for path in main_exe.parent.iterdir()):
        raise RuntimeError("Onefile build must not require an _internal directory")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.out_dir)
