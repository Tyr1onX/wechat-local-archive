from __future__ import annotations

import json
from pathlib import Path

from wechat_local_archive.exporter import export_chat


class _FakeSession:
    def export_chat_payload(self, username: str) -> dict:
        return {
            "wxid": "wxid_me",
            "messages": [
                {
                    "chat": username,
                    "local_id": 1,
                    "server_id": 10,
                    "sort_seq": 1,
                    "create_time": 1_725_264_000,
                    "sender_name": "朋友",
                    "type": "文本",
                    "type_code": 1,
                    "content": "你好",
                }
            ],
        }


def test_export_chat_writes_json_and_markdown(tmp_path: Path) -> None:
    summary = export_chat(
        _FakeSession(),  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )
    payload = json.loads(summary.archive_path.read_text(encoding="utf-8"))
    markdown = summary.markdown_path.read_text(encoding="utf-8")
    assert payload["conversation"]["id"] == "wxid_friend"
    assert payload["messages"][0]["content"] == "你好"
    assert "你好" in markdown
    assert summary.message_count == 1
