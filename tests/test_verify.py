from __future__ import annotations

import json
from pathlib import Path

from wechat_local_archive.archive import archive_from_dict, normalize_payload
from wechat_local_archive.exporter import render_markdown
from wechat_local_archive.verify import verify_archive


def _valid_archive(root: Path):
    archive = normalize_payload(
        {
            "wxid": "wxid_me",
            "nick_name": "我自己",
            "messages": [
                {
                    "chat": "wxid_friend",
                    "local_id": 1,
                    "server_id": 101,
                    "sort_seq": 10,
                    "create_time": 1_725_177_600,
                    "sender_name": "朋友",
                    "type": "语音",
                    "type_code": 34,
                    "content": "[语音]",
                },
                {
                    "chat": "wxid_friend",
                    "local_id": 2,
                    "server_id": 102,
                    "sort_seq": 20,
                    "create_time": 1_725_177_700,
                    "sender_name": "朋友",
                    "type": "文本",
                    "type_code": 1,
                    "content": "你好",
                },
            ],
        },
        "wxid_friend",
        "朋友",
    )
    voice = root / "assets" / "voice" / "1.silk"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"silk")
    archive.messages[0].attachment = "assets/voice/1.silk"
    archive.messages[0].transcript = "语音内容"
    _write(root, archive)
    return archive


def _write(root: Path, archive) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "archive.json").write_text(
        json.dumps(archive.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (root / "chat.md").write_text(render_markdown(archive), encoding="utf-8")


def _rewrite_raw(root: Path, payload: dict) -> None:
    (root / "archive.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    loaded = archive_from_dict(payload)
    (root / "chat.md").write_text(render_markdown(loaded), encoding="utf-8")


def test_verify_accepts_complete_archive(tmp_path: Path) -> None:
    _valid_archive(tmp_path)

    result = verify_archive(tmp_path)

    assert result.ok
    assert result.message_count == 2
    assert result.attachment_count == 1
    assert result.voice_count == 1
    assert result.voice_transcript_count == 1
    assert result.orphan_count == 0


def test_verify_detects_duplicate_message_id(tmp_path: Path) -> None:
    _valid_archive(tmp_path)
    payload = json.loads((tmp_path / "archive.json").read_text(encoding="utf-8"))
    payload["messages"].append(dict(payload["messages"][0]))
    _rewrite_raw(tmp_path, payload)

    result = verify_archive(tmp_path)

    assert not result.ok
    assert any("duplicate message id" in error for error in result.errors)


def test_verify_detects_out_of_order_messages(tmp_path: Path) -> None:
    _valid_archive(tmp_path)
    payload = json.loads((tmp_path / "archive.json").read_text(encoding="utf-8"))
    payload["messages"].reverse()
    _rewrite_raw(tmp_path, payload)

    result = verify_archive(tmp_path)

    assert not result.ok
    assert any("out of chronological order" in error for error in result.errors)


def test_verify_detects_missing_attachment(tmp_path: Path) -> None:
    _valid_archive(tmp_path)
    (tmp_path / "assets" / "voice" / "1.silk").unlink()

    result = verify_archive(tmp_path)

    assert not result.ok
    assert any("attachment is missing" in error for error in result.errors)


def test_verify_rejects_attachment_path_traversal(tmp_path: Path) -> None:
    archive = _valid_archive(tmp_path / "archive")
    outside = tmp_path / "outside.silk"
    outside.write_bytes(b"outside")
    archive.messages[0].attachment = "../outside.silk"
    _write(tmp_path / "archive", archive)

    result = verify_archive(tmp_path / "archive")

    assert not result.ok
    assert any("escapes archive root" in error for error in result.errors)


def test_verify_detects_stale_markdown(tmp_path: Path) -> None:
    _valid_archive(tmp_path)
    (tmp_path / "chat.md").write_text("stale\n", encoding="utf-8")

    result = verify_archive(tmp_path)

    assert not result.ok
    assert any("chat.md is stale" in error for error in result.errors)


def test_verify_prunes_orphans_only_when_archive_is_valid(tmp_path: Path) -> None:
    _valid_archive(tmp_path)
    orphan = tmp_path / "assets" / "orphan.bin"
    orphan.write_bytes(b"orphan")

    before = verify_archive(tmp_path)
    after = verify_archive(tmp_path, prune_orphans=True)

    assert before.ok and before.orphan_count == 1
    assert after.ok and after.pruned_count == 1 and after.orphan_count == 0
    assert not orphan.exists()
