from __future__ import annotations

import importlib.metadata
import json
import zipfile
from pathlib import Path

import pytest

from scripts.build_portable import _check_contents, _remove_build_metadata
from scripts.smoke_portable import verify_zip
from wechat_local_archive.cli import _runtime_diagnostics, build_parser
from wechat_local_archive.version import __version__


def test_version_uses_installed_package_metadata(capsys):
    assert __version__ == importlib.metadata.version("wechat-local-archive")
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


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


def test_portable_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.txt", "not allowed")
    with pytest.raises(ValueError, match="Unsafe ZIP path"):
        verify_zip(archive)
