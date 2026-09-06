from __future__ import annotations

from datetime import date

from wechat_local_archive.archive import normalize_payload, parse_date, selected_media_type


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


def test_parse_date_and_wrapped_media_type() -> None:
    assert parse_date("2026-09-06") == date(2026, 9, 6)
    assert selected_media_type((57 << 32) | 49) == 49
