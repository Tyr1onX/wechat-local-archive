from __future__ import annotations

import json
from pathlib import Path

import pytest

from wechat_local_archive.ai_export import iter_ai_rows, rebuild_ai_jsonl, write_ai_jsonl
from wechat_local_archive.archive import Archive, ArchiveMessage
from wechat_local_archive.cli import main
from wechat_local_archive.exporter import render_markdown
from wechat_local_archive.verify import verify_archive


def _message(number: int, type_code: int, content: str = "", **changes) -> ArchiveMessage:
    values = dict(
        id=str(number), local_id=number, server_id=number, sort_seq=number,
        timestamp="2026-01-01T12:00:00+08:00", timestamp_unix=1767240000,
        sender="我自己", type="消息", type_code=type_code, content=content,
    )
    values.update(changes)
    return ArchiveMessage(**values)


def _archive(messages: list[ArchiveMessage], conversation_id: str = "wxid_friend") -> Archive:
    return Archive(3, "2026-01-01T12:00:00+08:00", "wxid_me", "我自己",
                   conversation_id, "朋友", messages)


def _save_archive(root: Path, archive: Archive) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "archive.json").write_text(json.dumps(archive.to_dict(), ensure_ascii=False), encoding="utf-8")
    (root / "chat.md").write_text(render_markdown(archive), encoding="utf-8")


def test_ai_rows_preserve_order_and_render_message_types(tmp_path: Path) -> None:
    messages = [
        _message(1, 1, "你好 <3"),
        _message(2, 34, "<msg><voicemsg /></msg>", sender="朋友", transcript="明天见", attachment="assets/voice/2.silk"),
        _message(3, 34, "<msg />", sender="朋友"),
        _message(4, 3, "<msg><img md5='secret' /></msg>", attachment="assets/image/4.jpg"),
        _message(5, 43, "<msg />", attachment="assets/video/5.mp4"),
        _message(6, 49, "<msg><appmsg><type>6</type><title>报告.pdf</title><md5>secret</md5></appmsg></msg>", attachment="assets/file/报告.pdf"),
        _message(7, 10000, "<msg><content>你撤回了一条消息</content></msg>"),
        _message(8, 47, "<msg><emoji md5='secret' /></msg>"),
        _message(9, 48, "<msg><location label='x'><label>图书馆</label></location></msg>"),
        _message(10, 49, "<msg><appmsg><type>5</type><title>新闻</title></appmsg></msg>"),
        _message(11, 1, "<msg><content>正文</content><secret>hidden</secret></msg>"),
        _message(12, 999, "<private><md5>secret</md5></private>"),
    ]
    rows = list(iter_ai_rows(_archive(messages), tmp_path))
    assert len(rows) == len(messages)
    assert [row["type"] for row in rows] == [
        "text", "voice", "voice", "image", "video", "file", "system", "sticker", "location", "link", "text", "unknown"
    ]
    assert [row["text"] for row in rows] == [
        "你好 <3", "明天见", "[语音未转写]", "[图片]", "[视频]", "[文件] 报告.pdf",
        "你撤回了一条消息", "[表情]", "[位置] 图书馆", "[链接] 新闻", "正文", "[消息]",
    ]
    assert rows[0]["sender"] == "我"
    assert rows[1]["sender"] == "朋友"
    assert rows[6]["sender"] == "系统"
    assert rows[1]["asset"] == "assets/voice/2.silk"
    assert "asset" not in rows[2]
    assert all(set(row) <= {"time", "sender", "type", "text", "asset"} for row in rows)
    assert all("<msg" not in row["text"] and "<appmsg" not in row["text"] for row in rows)
    assert list(iter_ai_rows(_archive(messages, "group@chatroom"), tmp_path))[1]["sender"] == "朋友"


def test_ai_output_is_utf8_jsonl_and_idempotent(tmp_path: Path) -> None:
    archive = _archive([_message(1, 1, "中文\n第二行"), _message(2, 34, transcript="转写")])
    _save_archive(tmp_path, archive)
    target = rebuild_ai_jsonl(tmp_path)
    original = target.read_bytes()
    mtime = target.stat().st_mtime_ns
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["text"] == "中文\n第二行"
    assert len(original) < (tmp_path / "archive.json").stat().st_size
    assert rebuild_ai_jsonl(tmp_path) == target
    assert target.read_bytes() == original
    assert target.stat().st_mtime_ns == mtime
    assert not list(tmp_path.glob("ai.jsonl.*.tmp"))


def test_rebuild_is_offline_and_rejects_invalid_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _archive([_message(1, 1, "原文")])
    _save_archive(tmp_path, archive)
    monkeypatch.setattr("wechat_local_archive.cli.open_offline", lambda: pytest.fail("WeChat must not be opened"))
    assert main(["ai", str(tmp_path)]) == 0
    target = tmp_path / "ai.jsonl"
    original = target.read_bytes()
    (tmp_path / "archive.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="Unable to read archive.json"):
        rebuild_ai_jsonl(tmp_path)
    assert target.read_bytes() == original


def test_ai_write_failure_preserves_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wechat_local_archive import ai_export

    archive = _archive([_message(1, 1, "旧内容")])
    target = write_ai_jsonl(archive, tmp_path)
    original = target.read_bytes()
    archive.messages[0].content = "新内容"

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(ai_export.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_ai_jsonl(archive, tmp_path)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("ai.jsonl.*.tmp"))


def test_malformed_structured_text_does_not_leak_xml(tmp_path: Path) -> None:
    rows = list(iter_ai_rows(_archive([
        _message(1, 1, "<msg><content>broken"),
        _message(2, 1, "<msg><content>&lt;private&gt;secret&lt;/private&gt;</content></msg>"),
    ]), tmp_path))
    assert [row["text"] for row in rows] == ["[未解析的消息]", "[未解析的消息]"]


def test_ai_paths_are_relative_and_do_not_escape_archive(tmp_path: Path) -> None:
    archive = _archive([
        _message(1, 3, attachment="assets/../assets/image/1.jpg"),
        _message(2, 3, attachment="../private.jpg"),
    ])
    rows = list(iter_ai_rows(archive, tmp_path / "archive"))
    assert rows[0]["asset"] == "assets/image/1.jpg"
    assert "asset" not in rows[1]


def test_verify_detects_stale_ai_and_rebuild_repairs_it(tmp_path: Path) -> None:
    archive = _archive([_message(1, 1, "原文")])
    _save_archive(tmp_path, archive)
    write_ai_jsonl(archive, tmp_path)
    assert verify_archive(tmp_path).ok
    (tmp_path / "ai.jsonl").write_text('{"text":"错误"}\n', encoding="utf-8")
    result = verify_archive(tmp_path)
    assert not result.ok
    assert any("ai.jsonl is stale" in error for error in result.errors)
    assert json.loads((tmp_path / "archive.json").read_text(encoding="utf-8"))["messages"][0]["content"] == "原文"
    rebuild_ai_jsonl(tmp_path)
    assert verify_archive(tmp_path).ok
    (tmp_path / "ai.jsonl").write_text("{invalid\n", encoding="utf-8")
    assert not verify_archive(tmp_path).ok
