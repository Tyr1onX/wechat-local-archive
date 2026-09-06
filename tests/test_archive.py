from __future__ import annotations

from datetime import date

import pytest

from wechat_local_archive.archive import (
    archive_from_dict,
    merge_archives,
    normalize_payload,
    parse_date,
    selected_media_type,
)


def test_normalize_payload_filters_dates_and_sorts() -> None:
    payload = {
        "wxid": "wxid_me",
        "messages": [
            {
                "chat": "wxid_friend",
                "local_id": 2,
                "server_id": 102,
                "sort_seq": 20,
                "create_time": 1_725_264_000,
                "sender_name": "朋友",
                "type": "文本",
                "type_code": 1,
                "content": "第二条",
            },
            {
                "chat": "wxid_friend",
                "local_id": 1,
                "server_id": 101,
                "sort_seq": 10,
                "create_time": 1_725_177_600,
                "sender_name": "我",
                "type": "语音",
                "type_code": 34,
                "content": "[语音]",
            },
            {
                "chat": "other",
                "local_id": 3,
                "server_id": 103,
                "sort_seq": 30,
                "create_time": 1_725_264_100,
                "sender_name": "其他",
                "type": "文本",
                "type_code": 1,
                "content": "不应出现",
            },
        ],
    }
    archive = normalize_payload(
        payload,
        conversation_id="wxid_friend",
        conversation_name="朋友",
        start=date(2024, 9, 1),
        end=date(2024, 9, 3),
    )
    assert [message.local_id for message in archive.messages] == [1, 2]
    assert archive.messages[0].sender == "我"
    assert archive.messages[1].content == "第二条"


def test_normalize_payload_prefers_timestamp_over_cross_shard_sort_seq() -> None:
    payload = {
        "wxid": "wxid_me",
        "nick_name": "我自己",
        "messages": [
            {
                "chat": "wxid_friend",
                "local_id": 2,
                "server_id": 102,
                "sort_seq": 1_725_264_001_000,
                "create_time": 1_725_264_000,
                "sender_name": "朋友",
                "type": "文本",
                "type_code": 1,
                "content": "后发",
            },
            {
                "chat": "wxid_friend",
                "local_id": 1,
                "server_id": 101,
                "sort_seq": 1_725_264_000_000,
                "create_time": 1_725_264_001,
                "sender_name": "我自己",
                "type": "文本",
                "type_code": 1,
                "content": "更晚",
            },
        ],
    }
    archive = normalize_payload(payload, "wxid_friend", "朋友")
    assert [message.local_id for message in archive.messages] == [2, 1]
    assert archive.account_name == "我自己"
    assert archive.schema_version == 3


def test_archive_merge_preserves_existing_derived_fields() -> None:
    existing = normalize_payload(
        {
            "wxid": "wxid_me",
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
                }
            ],
        },
        "wxid_friend",
        "朋友",
    )
    existing.messages[0].attachment = "assets/voice/1.silk"
    existing.messages[0].transcript = "旧转写"
    loaded = archive_from_dict(existing.to_dict())
    fresh = normalize_payload(
        {
            "wxid": "wxid_me",
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
                    "content": "新增",
                },
            ],
        },
        "wxid_friend",
        "朋友",
    )

    merged = merge_archives(loaded, fresh)

    assert [message.id for message in merged.messages] == ["101", "102"]
    assert merged.messages[0].attachment == "assets/voice/1.silk"
    assert merged.messages[0].transcript == "旧转写"


def test_archive_loader_accepts_schema_two_without_range() -> None:
    archive = normalize_payload({"wxid": "wxid_me", "messages": []}, "wxid_friend", "朋友")
    payload = archive.to_dict()
    payload["schema_version"] = 2
    payload.pop("range")

    loaded = archive_from_dict(payload)

    assert loaded.schema_version == 2
    assert loaded.range_start is None
    assert loaded.range_end is None


def test_archive_loader_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="Unsupported archive schema"):
        archive_from_dict(
            {
                "schema_version": 99,
                "account": "wxid_me",
                "conversation": {"id": "wxid_friend", "name": "朋友"},
                "messages": [],
            }
        )


def test_parse_date_and_wrapped_media_type() -> None:
    assert parse_date("2026-09-06") == date(2026, 9, 6)
    assert selected_media_type((57 << 32) | 49) == 49
