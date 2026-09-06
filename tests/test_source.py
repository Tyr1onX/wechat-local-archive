from __future__ import annotations

import pytest

from wechat_local_archive.source import SourceError, resolve_chat


def test_resolve_chat_prefers_exact_name() -> None:
    chats = [
        {"username": "wxid_a", "name": "Alice"},
        {"username": "wxid_b", "name": "Alice 2"},
    ]
    assert resolve_chat(chats, "Alice")["username"] == "wxid_a"


def test_resolve_chat_accepts_unique_substring() -> None:
    chats = [
        {"username": "wxid_a", "name": "Alice"},
        {"username": "wxid_b", "name": "Bob"},
    ]
    assert resolve_chat(chats, "lic")["username"] == "wxid_a"


def test_resolve_chat_rejects_ambiguous_selector() -> None:
    chats = [
        {"username": "wxid_a", "name": "Alice"},
        {"username": "wxid_b", "name": "Alice 2"},
    ]
    with pytest.raises(SourceError):
        resolve_chat(chats, "Ali")
