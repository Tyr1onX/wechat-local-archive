from __future__ import annotations

import json
from pathlib import Path

import pytest

from wechat_local_archive import service as module
from wechat_local_archive.service import ArchiveService, ServiceStatus
from wechat_local_archive.state import AppConfig, UiConfig, load_ui_config, save_ui_config


def test_destination_reuses_matching_legacy_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "load_config", lambda: AppConfig("local", "wxid_me"))
    old = tmp_path / "朋友"
    old.mkdir()
    (old / "archive.json").write_text(json.dumps({"account": "wxid_me", "conversation": {"id": "wxid_friend"}}), encoding="utf-8")
    chat = {"username": "wxid_friend", "name": "朋友"}
    assert ArchiveService().destination(tmp_path, chat) == old
    # A Windows account directory suffix must not be mistaken for the wxid.
    monkeypatch.setattr(module, "load_config", lambda: AppConfig("local", "wxid_me_suffix"))
    assert ArchiveService().destination(tmp_path, {**chat, "account_id": "wxid_me"}) == old
    (old / "archive.json").write_text(json.dumps({"account": "other", "conversation": {"id": "wxid_friend"}}), encoding="utf-8")
    assert ArchiveService().destination(tmp_path, chat) == tmp_path / "wxid_friend"


def test_initialize_requires_explicit_account_when_multiple_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "load_config", lambda: None)
    monkeypatch.setattr(module, "discover_accounts", lambda db_dir=None: [{"account": "one"}, {"account": "two"}])
    calls = []
    monkeypatch.setattr(module, "bootstrap", lambda **kwargs: calls.append(kwargs))
    service = ArchiveService()
    monkeypatch.setattr(service, "status", lambda: ServiceStatus(ready=True, account="two"))
    with pytest.raises(ValueError, match="select an account"):
        service.initialize()
    assert not calls
    assert service.initialize(account="two").account == "two"
    assert calls == [{"db_dir": None, "account": "two"}]


def test_ui_config_contains_only_output_and_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wechat_local_archive.state.ui_config_path", lambda: tmp_path / "ui.json")
    config = UiConfig(output_root=str(tmp_path), asr_preset="background")
    save_ui_config(config)
    assert load_ui_config() == config
    assert set(json.loads((tmp_path / "ui.json").read_text(encoding="utf-8"))) == {"output_root", "asr_preset"}
    (tmp_path / "ui.json").write_text("[]", encoding="utf-8")
    assert load_ui_config() is None
