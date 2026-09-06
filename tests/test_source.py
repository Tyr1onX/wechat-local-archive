from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from wechat_local_archive.source import SourceError, WeChatSourceAdapter, resolve_chat


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


class _Rows:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _VoiceConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.closed = False

    def execute(self, sql: str, _params: tuple):
        if "Name2Id" in sql:
            return _Rows([(7,)])
        if "VoiceInfo" in sql:
            return _Rows(self.rows)
        raise AssertionError(sql)

    def close(self) -> None:
        self.closed = True


class _FakeDB:
    def __init__(self, account_dir: Path, shards: dict[str, _VoiceConnection] | None = None) -> None:
        self.account_dir = str(account_dir)
        self.wxid = "wxid_me"
        self._connections = shards or {}
        self._db_files = [
            (name, str(account_dir / "db_storage" / "message" / name), 0)
            for name in self._connections
        ]
        self._new_messages: list[dict] = []

    def _open(self, rel: str):
        return self._connections[rel]

    def list_message_chats(self) -> list[dict]:
        return [{"username": "wxid_friend", "message_count": 12}]

    def get_new_messages(self, _user: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        return [item for item in self._new_messages if item["sort_seq"] > since_seq][:limit]


def test_source_adapter_searches_all_media_shards(tmp_path: Path) -> None:
    first = _VoiceConnection([])
    second = _VoiceConnection(
        [{"local_id": 22, "svr_id": 2200, "voice_data": b"silk-data"}]
    )
    adapter = WeChatSourceAdapter(
        _FakeDB(tmp_path, {"media_0.db": first, "media_1.db": second}),  # type: ignore[arg-type]
        cfg_dword=None,
        installed_version="1.2.0.3",
    )

    rows = list(adapter.iter_voice_rows("wxid_friend"))

    assert [(row.local_id, row.server_id, row.data) for row in rows] == [
        (22, 2200, b"silk-data")
    ]
    assert first.closed and second.closed


def test_source_adapter_finds_image_variants(tmp_path: Path) -> None:
    conversation = "wxid_friend"
    digest = "a" * 32
    chat_hash = hashlib.md5(conversation.encode("utf-8")).hexdigest()
    image_dir = tmp_path / "msg" / "attach" / chat_hash / "2026-09" / "Img"
    image_dir.mkdir(parents=True)
    normal = image_dir / f"{digest}.dat"
    high = image_dir / f"{digest}_h.dat"
    normal.write_bytes(b"normal")
    high.write_bytes(b"high")

    adapter = WeChatSourceAdapter(
        _FakeDB(tmp_path),  # type: ignore[arg-type]
        cfg_dword=None,
        installed_version="1.2.0.3",
    )
    found = adapter.find_image_sources(conversation, {digest})

    assert found[digest]["normal"] == normal
    assert found[digest]["high"] == high


def test_source_adapter_finds_requested_files_only(tmp_path: Path) -> None:
    file_dir = tmp_path / "msg" / "file" / "2026-09"
    file_dir.mkdir(parents=True)
    wanted = file_dir / "report.docx"
    other = file_dir / "other.pdf"
    wanted.write_bytes(b"doc")
    other.write_bytes(b"pdf")

    adapter = WeChatSourceAdapter(
        _FakeDB(tmp_path),  # type: ignore[arg-type]
        cfg_dword=None,
        installed_version="1.2.0.3",
    )
    found = adapter.find_file_sources({"report.docx"})

    assert found == {"report.docx": [wanted]}


def test_source_adapter_exposes_lightweight_incremental_probe(tmp_path: Path) -> None:
    db = _FakeDB(tmp_path)
    db._new_messages = [{"sort_seq": 101}]
    adapter = WeChatSourceAdapter(
        db,  # type: ignore[arg-type]
        cfg_dword=None,
        installed_version="1.2.0.3",
    )

    assert adapter.account_id == "wxid_me"
    assert adapter.chat_message_count("wxid_friend") == 12
    assert adapter.has_new_messages("wxid_friend", 100) is True
    assert adapter.has_new_messages("wxid_friend", 101) is False


def test_source_adapter_accepts_supported_three_part_version(tmp_path: Path) -> None:
    WeChatSourceAdapter(
        _FakeDB(tmp_path),  # type: ignore[arg-type]
        cfg_dword=None,
        installed_version="1.2.1",
    )


def test_source_adapter_rejects_unsupported_dependency_version(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="Unsupported wechatauto-replica version"):
        WeChatSourceAdapter(
            _FakeDB(tmp_path),  # type: ignore[arg-type]
            cfg_dword=None,
            installed_version="1.3.0.0",
        )
