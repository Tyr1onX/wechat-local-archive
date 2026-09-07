"""Exercise the extracted application without using the host's Python or WeChat state."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*, package: Path) -> None:
    import pysilk
    from wechat_local_archive.archive import Archive, ArchiveMessage, CURRENT_SCHEMA_VERSION
    from wechat_local_archive.exporter import render_markdown
    from wechat_local_archive.version import __version__

    package = package.resolve()
    with tempfile.TemporaryDirectory(prefix="wechat-portable-smoke-") as temporary:
        root = Path(temporary)
        extracted = root / "app"
        shutil.copytree(package, extracted)
        state = root / "state"
        profile = root / "profile"
        for directory in (state, profile, root / "tmp", root / "empty-db"):
            directory.mkdir()
        env = os.environ.copy()
        for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE", "XDG_CACHE_HOME"):
            env.pop(key, None)
        env.update({
            "LOCALAPPDATA": str(state), "APPDATA": str(root / "roaming"),
            "USERPROFILE": str(profile), "HOME": str(profile),
            "TEMP": str(root / "tmp"), "TMP": str(root / "tmp"),
            "HF_HOME": str(root / "models"), "MODELSCOPE_CACHE": str(root / "models"),
            "PATH": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32") + ";" + os.environ.get("SystemRoot", r"C:\Windows"),
        })
        assert (extracted / "LICENSE.txt").is_file()
        assert (extracted / "licenses" / "FFmpeg" / "GPL-3.0.txt").is_file()
        assert (extracted / "licenses" / "FFmpeg" / "SOURCE.txt").is_file()
        assert (extracted / "THIRD_PARTY_NOTICES.txt").is_file()
        exe = extracted / "WeChatLocalArchiveCLI.exe"
        def call(*args: str, expected: int = 0) -> str:
            result = subprocess.run([str(exe), *args], cwd=root, env=env, capture_output=True, text=True, timeout=90)
            if result.returncode != expected:
                raise AssertionError(f"{args}: exit={result.returncode}\n{result.stdout}\n{result.stderr}")
            return result.stdout

        assert f"wechat-archive {__version__}" in call("--version")
        assert "verify" in call("--help")
        call("gui", "--smoke")
        assert "runtime: PASS" in call("doctor", "--db-dir", str(root / "empty-db"), "--runtime")

        fixture = root / "fixture"
        voice = fixture / "assets" / "voice" / "sample.silk"
        voice.parent.mkdir(parents=True)
        with voice.open("wb") as stream:
            pysilk.encode(io.BytesIO(b"\0\0" * 24000), stream, 24000, 24000)
        messages = [
            ArchiveMessage("1", 1, 1, 1, "2026-01-01T00:00:00+08:00", 1767196800, "我自己", "text", 1, "离线测试"),
            ArchiveMessage("2", 2, 2, 2, "2026-01-01T00:01:00+08:00", 1767196860, "朋友", "voice", 34, "", attachment="assets/voice/sample.silk", transcript="测试语音"),
        ]
        archive = Archive(CURRENT_SCHEMA_VERSION, "2026-01-01T00:00:00+08:00", "wxid_test", "我自己", "wxid_friend", "朋友", messages)
        source = fixture / "archive.json"
        source.write_text(json.dumps(archive.to_dict(), ensure_ascii=False), encoding="utf-8")
        (fixture / "chat.md").write_text(render_markdown(archive), encoding="utf-8")
        before = hashlib.sha256(source.read_bytes()).digest()
        assert "status: PASS" in call("verify", str(fixture))
        call("ai", str(fixture))
        call("html", str(fixture))
        assert "status: PASS" in call("verify", str(fixture))
        assert len((fixture / "ai.jsonl").read_text(encoding="utf-8").splitlines()) == 2
        assert list((fixture / "assets" / "reader-audio").glob("*.mp3"))
        assert "测试语音" in (fixture / "chat.html").read_text(encoding="utf-8")
        assert hashlib.sha256(source.read_bytes()).digest() == before
        assert not (state / "WeChatLocalArchive" / "secrets.dpapi").exists()
        print("Portable isolated smoke: PASS")
        print("Version, hidden GUI initialization, native runtime, offline verify/AI/HTML/audio: PASS")
        print("No host Python, WeChat state, or real conversation data used")


def verify_zip(path: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="wechat-portable-zip-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                target = (root / member.filename).resolve()
                if not target.is_relative_to(root.resolve()):
                    raise ValueError("Unsafe ZIP path")
            archive.extractall(root)
        packages = [p for p in root.iterdir() if p.is_dir()]
        if len(packages) != 1:
            raise ValueError("ZIP must contain exactly one application directory")
        run(package=packages[0])
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    if args.package.suffix.lower() == ".zip":
        verify_zip(args.package)
    else:
        run(package=args.package)
