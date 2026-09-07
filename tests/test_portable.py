from __future__ import annotations

import hashlib
import importlib.metadata
import json
import zipfile
from pathlib import Path

import pytest

from scripts.build_portable import _check_contents, _release_tag, _remove_build_metadata
from scripts.smoke_portable import verify_zip
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


def test_native_runtime_diagnostics(capsys):
    _runtime_diagnostics()
    assert "runtime: PASS" in capsys.readouterr().out


def test_remove_only_distribution_build_metadata(tmp_path):
    metadata = tmp_path / "_internal" / "package-1.0.dist-info" / "direct_url.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"url":"file:///private/build"}', encoding="utf-8")
    other = tmp_path / "other" / "direct_url.json"
    other.parent.mkdir()
    other.write_text("keep", encoding="utf-8")
    _remove_build_metadata(tmp_path)
    assert not metadata.exists()
    assert other.read_text(encoding="utf-8") == "keep"


def test_portable_content_guard_rejects_private_data_and_models(tmp_path):
    for name in ("WeChatLocalArchive.exe", "WeChatLocalArchiveCLI.exe"):
        (tmp_path / name).write_bytes(b"launcher")
    _check_contents(tmp_path)
    for relative in ("archive.json", "secrets.dpapi", "db_storage/message.db", "models/model_quant.onnx", "direct_url.json"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"private")
        with pytest.raises(RuntimeError, match="Unexpected private data"):
            _check_contents(tmp_path)
        target.unlink()


def test_release_verifier_reports_mismatched_field(tmp_path, monkeypatch):
    from scripts import verify_release as module
    commit = "a" * 40
    version = __version__
    tag = f"v{version}"
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "git", lambda *args: commit)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    archive = tmp_path / f"wechat-local-archive-windows-x64-{tag}.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("wechat-local-archive-windows-x64/build-info.json", json.dumps({"version": version, "commit": commit, "tag": None, "dirty": False}))
    (tmp_path / (archive.name + ".sha256")).write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="ascii")
    with pytest.raises(ValueError, match="Release provenance mismatch: tag"):
        module.verify_release(archive)


def test_portable_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.txt", "not allowed")
    with pytest.raises(ValueError, match="Unsafe ZIP path"):
        verify_zip(archive)
