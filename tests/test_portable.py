from __future__ import annotations

import hashlib
import importlib.metadata
import subprocess
from pathlib import Path

import pytest

from scripts.build_portable import (
    _check_contents,
    _check_outputs,
    _release_tag,
    committed_digest,
    tracked_source_is_dirty,
)
from wechat_local_archive.cli import _runtime_diagnostics, build_parser
from wechat_local_archive.version import __version__


def test_version_uses_installed_package_metadata(capsys):
    assert __version__ == importlib.metadata.version("wechat-local-archive")
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_release_tag_uses_verified_ci_ref(monkeypatch):
    from scripts import build_portable

    commit = "a" * 40
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    expected = f"v{__version__}"
    monkeypatch.setenv("GITHUB_REF_NAME", expected)
    monkeypatch.setattr(build_portable, "git", lambda *args: commit)
    assert _release_tag(__version__, commit) == expected

    monkeypatch.setenv("GITHUB_REF_NAME", "v0.0.0")
    with pytest.raises(RuntimeError, match="does not match"):
        _release_tag(__version__, commit)

    monkeypatch.setenv("GITHUB_REF_NAME", expected)
    monkeypatch.setattr(build_portable, "git", lambda *args: "b" * 40)
    with pytest.raises(RuntimeError, match="does not point"):
        _release_tag(__version__, commit)

    monkeypatch.delenv("GITHUB_REF_TYPE")
    monkeypatch.delenv("GITHUB_REF_NAME")
    monkeypatch.setattr(build_portable, "git", lambda *args: expected)
    assert _release_tag(__version__, commit) == expected


def test_release_source_check_ignores_line_ending_changes_but_rejects_real_edits(
    tmp_path, monkeypatch
):
    from scripts import build_portable

    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=tmp_path, text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "core.autocrlf", "true")
    (tmp_path / ".gitattributes").write_text(
        "requirements-win*.lock text eol=lf\n", encoding="utf-8"
    )
    lock = tmp_path / "requirements-win.lock"
    lock.write_bytes(b"example==1.0\n")
    git("add", ".gitattributes", "requirements-win.lock")
    git("commit", "-qm", "Initial")
    monkeypatch.setattr(build_portable, "ROOT", tmp_path)

    commit = git("rev-parse", "HEAD")
    expected = hashlib.sha256(b"example==1.0\n").hexdigest()
    assert committed_digest(commit, "requirements-win.lock") == expected

    lock.write_bytes(b"example==1.0\r\n")
    assert not tracked_source_is_dirty()

    lock.write_bytes(b"example==2.0\n")
    assert tracked_source_is_dirty()
    git("add", "requirements-win.lock")
    assert tracked_source_is_dirty()
    assert committed_digest(commit, "requirements-win.lock") == expected


def test_native_runtime_diagnostics(capsys):
    _runtime_diagnostics()
    assert "runtime: PASS" in capsys.readouterr().out


def test_onefile_output_guard_accepts_only_standalone_launchers(tmp_path):
    main = tmp_path / "WeChatLocalArchive.exe"
    cli = tmp_path / "WeChatLocalArchiveCLI.exe"
    main.write_bytes(b"MZ" + b"\0" * (1024 * 1024))
    cli.write_bytes(b"MZ" + b"\0" * (1024 * 1024))

    _check_outputs(main, cli)
    _check_contents(tmp_path)

    assert not (tmp_path / "_internal").exists()


@pytest.mark.parametrize(
    "relative",
    (
        "archive.json",
        "secrets.dpapi",
        "db_storage/message.db",
        "models/model_quant.onnx",
        "direct_url.json",
        "_internal/python312.dll",
    ),
)
def test_onefile_output_guard_rejects_private_models_and_legacy_runtime(
    tmp_path, relative
):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"unexpected")

    with pytest.raises(RuntimeError, match="Unexpected private data"):
        _check_contents(tmp_path)


def test_onefile_output_guard_rejects_legacy_application_directory(tmp_path):
    main = tmp_path / "WeChatLocalArchive.exe"
    cli = tmp_path / "WeChatLocalArchiveCLI.exe"
    main.write_bytes(b"MZ" + b"\0" * (1024 * 1024))
    cli.write_bytes(b"MZ" + b"\0" * (1024 * 1024))
    (tmp_path / "wechat-local-archive-windows-x64").mkdir()

    with pytest.raises(RuntimeError, match="Legacy onedir output"):
        _check_outputs(main, cli)


def test_source_check_fails_closed_on_git_error(monkeypatch):
    from scripts import build_portable

    monkeypatch.setattr(
        build_portable.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 128})(),
    )
    with pytest.raises(RuntimeError, match="Unable to check source changes"):
        tracked_source_is_dirty()


def test_release_verifier_rejects_checksum_mismatch(tmp_path, monkeypatch):
    from scripts import verify_release as module

    commit = "a" * 40
    version = __version__
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "git", lambda *args: commit)

    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n',
        encoding="utf-8",
    )
    executable = tmp_path / "WeChatLocalArchive.exe"
    executable.write_bytes(b"MZ" + b"payload")
    (tmp_path / "WeChatLocalArchive.exe.sha256").write_text(
        f"{'0' * 64}  WeChatLocalArchive.exe\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        module.verify_release(executable)


def test_release_verifier_rejects_non_pe_file(tmp_path, monkeypatch):
    from scripts import verify_release as module

    commit = "a" * 40
    version = __version__
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "git", lambda *args: commit)

    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n',
        encoding="utf-8",
    )
    executable = tmp_path / "WeChatLocalArchive.exe"
    executable.write_bytes(b"not-an-exe")
    (tmp_path / "WeChatLocalArchive.exe.sha256").write_text(
        f"{hashlib.sha256(executable.read_bytes()).hexdigest()}  WeChatLocalArchive.exe\n",
        encoding="ascii",
    )

    with pytest.raises(ValueError, match="Windows PE"):
        module.verify_release(executable)
