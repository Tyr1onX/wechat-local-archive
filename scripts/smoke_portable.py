"""Exercise the standalone executable without host Python or real WeChat state."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _isolated_env(root: Path) -> dict[str, str]:
    state = root / "state"
    profile = root / "profile"
    for directory in (state, profile, root / "tmp", root / "empty-db"):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in (
        "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX",
        "HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE", "XDG_CACHE_HOME",
    ):
        env.pop(key, None)
    env.update({
        "LOCALAPPDATA": str(state),
        "APPDATA": str(root / "roaming"),
        "USERPROFILE": str(profile),
        "HOME": str(profile),
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "HF_HOME": str(root / "models"),
        "MODELSCOPE_CACHE": str(root / "models"),
        "PATH": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        + ";"
        + os.environ.get("SystemRoot", r"C:\Windows"),
    })
    return env


def run(*, executable: Path) -> None:
    import pysilk
    from wechat_local_archive.archive import Archive, ArchiveMessage, CURRENT_SCHEMA_VERSION
    from wechat_local_archive.exporter import render_markdown
    from wechat_local_archive.version import __version__

    executable = executable.resolve()
    if executable.name != "WeChatLocalArchive.exe" or not executable.is_file():
        raise ValueError("Expected built WeChatLocalArchive.exe")
    cli_source = executable.with_name("WeChatLocalArchiveCLI.exe")
    if not cli_source.is_file():
        raise ValueError("CI console helper is missing")

    with tempfile.TemporaryDirectory(prefix="wechat-onefile-smoke-") as temporary:
        root = Path(temporary)
        app = root / "app"
        app.mkdir()
        standalone = app / executable.name
        shutil.copy2(executable, standalone)
        env = _isolated_env(root)

        # Acceptance gate: the user-facing executable must work when it is literally
        # the only application file present. No _internal directory or source tree.
        assert [path.name for path in app.iterdir()] == ["WeChatLocalArchive.exe"]
        result = subprocess.run(
            [str(standalone), "web", "--smoke"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"standalone web smoke: exit={result.returncode}\n"
                f"{result.stdout}\n{result.stderr}"
            )
        assert not (app / "_internal").exists()

        # Keep a separate console onefile only for CI so the packaged native runtime
        # and offline archive commands remain deeply exercised. It is not uploaded.
        cli = app / cli_source.name
        shutil.copy2(cli_source, cli)

        def call(*args: str, expected: int = 0) -> str:
            result = subprocess.run(
                [str(cli), *args],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != expected:
                raise AssertionError(
                    f"{args}: exit={result.returncode}\n{result.stdout}\n{result.stderr}"
                )
            return result.stdout

        assert f"wechat-archive {__version__}" in call("--version")
        assert "verify" in call("--help")
        assert "runtime: PASS" in call(
            "doctor", "--db-dir", str(root / "empty-db"), "--runtime"
        )

        fixture = root / "fixture"
        voice = fixture / "assets" / "voice" / "sample.silk"
        voice.parent.mkdir(parents=True)
        with voice.open("wb") as stream:
            pysilk.encode(io.BytesIO(b"\0\0" * 24000), stream, 24000, 24000)
        messages = [
            ArchiveMessage(
                "1", 1, 1, 1, "2026-01-01T00:00:00+08:00", 1767196800,
                "我自己", "text", 1, "离线测试",
            ),
            ArchiveMessage(
                "2", 2, 2, 2, "2026-01-01T00:01:00+08:00", 1767196860,
                "朋友", "voice", 34, "",
                attachment="assets/voice/sample.silk",
                transcript="测试语音",
            ),
        ]
        archive = Archive(
            CURRENT_SCHEMA_VERSION,
            "2026-01-01T00:00:00+08:00",
            "wxid_test",
            "我自己",
            "wxid_friend",
            "朋友",
            messages,
        )
        source = fixture / "archive.json"
        source.write_text(
            json.dumps(archive.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
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
        assert not (root / "state" / "WeChatLocalArchive" / "secrets.dpapi").exists()

        print("Standalone onefile smoke: PASS")
        print("Single EXE Web UI, native runtime, offline verify/AI/HTML/audio: PASS")
        print("No _internal directory, host Python, WeChat state, or real conversation data used")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    run(executable=args.executable)
