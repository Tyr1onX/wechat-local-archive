from __future__ import annotations

import json
from pathlib import Path

import pytest

import wechat_local_archive.exporter as exporter_module
from wechat_local_archive.exporter import export_chat
from wechat_local_archive.cancellation import CancellationToken, ExportCancelled
from wechat_local_archive.source import VoiceRow


class _FakeSession:
    account_id = "wxid_me"

    def __init__(self) -> None:
        self.export_calls = 0
        self.changed = False
        self.voice_reads = 0
        self.messages = [
            {
                "chat": "wxid_friend",
                "local_id": 1,
                "server_id": 10,
                "sort_seq": 1,
                "create_time": 1_725_264_000,
                "sender_name": "朋友",
                "type": "文本",
                "type_code": 1,
                "content": "你好",
            }
        ]

    def export_chat_payload(self, username: str) -> dict:
        self.export_calls += 1
        return {
            "wxid": "wxid_me",
            "nick_name": "我自己",
            "messages": [{**message, "chat": username} for message in self.messages],
        }

    def has_new_messages(self, _username: str, _since_seq: int) -> bool:
        return self.changed

    def chat_message_count(self, _username: str) -> int:
        return len(self.messages)

    def iter_voice_rows(self, _username: str):
        self.voice_reads += 1
        yield VoiceRow(local_id=1, server_id=10, data=b"silk")



def test_cancelled_export_preserves_existing_archive(tmp_path: Path) -> None:
    session = _FakeSession()
    first = export_chat(session, conversation_id="wxid_friend", conversation_name="朋友", output_dir=tmp_path)
    original_json = first.archive_path.read_bytes()
    original_md = first.markdown_path.read_bytes()
    original_ai = (tmp_path / "ai.jsonl").read_bytes()
    session.messages.append({
        "chat": "wxid_friend", "local_id": 2, "server_id": 11, "sort_seq": 2,
        "create_time": 1_725_264_100, "sender_name": "朋友", "type": "voice",
        "type_code": 34, "content": "",
    })
    session.changed = True
    token = CancellationToken()

    def progress(current, total, label):
        if label.startswith("Extracting voices"):
            token.cancel()

    with pytest.raises(ExportCancelled):
        export_chat(session, conversation_id="wxid_friend", conversation_name="朋友",
                    output_dir=tmp_path, media="voice", progress=progress, cancel=token.check)
    assert first.archive_path.read_bytes() == original_json
    assert first.markdown_path.read_bytes() == original_md
    assert (tmp_path / "ai.jsonl").read_bytes() == original_ai


def test_selected_media_types_are_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.messages.append({
        "chat": "wxid_friend", "local_id": 2, "server_id": 11, "sort_seq": 2,
        "create_time": 1_725_264_100, "sender_name": "朋友", "type": "voice",
        "type_code": 34, "content": "",
    })
    summary = export_chat(session, conversation_id="wxid_friend", conversation_name="朋友",
                          output_dir=tmp_path, media_types=frozenset({3}))
    assert summary.attachment_count == 0
    assert session.voice_reads == 0
    with pytest.raises(ValueError, match="Voice must be selected"):
        export_chat(session, conversation_id="wxid_friend", conversation_name="朋友",
                    output_dir=tmp_path, media_types=frozenset({3}), transcribe=True)


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
    assert payload["account_name"] == "我自己"
    assert payload["schema_version"] == 4
    assert payload["messages"][0]["content"] == "你好"
    assert "你好" in markdown
    assert "· 朋友" in markdown
    assert summary.message_count == 1
    assert (tmp_path / "ai.jsonl").is_file()
    assert (tmp_path / "chat.html").is_file()
    ai_rows = [json.loads(line) for line in (tmp_path / "ai.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ai_rows == [{"time": payload["messages"][0]["timestamp"], "sender": "朋友", "type": "text", "text": "你好"}]


def test_unchanged_second_export_does_not_read_full_history_or_rewrite(tmp_path: Path) -> None:
    session = _FakeSession()
    first = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )
    archive_before = first.archive_path.read_bytes()
    markdown_before = first.markdown_path.read_bytes()
    archive_mtime = first.archive_path.stat().st_mtime_ns
    markdown_mtime = first.markdown_path.stat().st_mtime_ns
    ai_path = tmp_path / "ai.jsonl"
    ai_before = ai_path.read_bytes()
    ai_mtime = ai_path.stat().st_mtime_ns

    second = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )

    assert session.export_calls == 1
    assert second.message_count == 1
    assert first.archive_path.read_bytes() == archive_before
    assert first.markdown_path.read_bytes() == markdown_before
    assert first.archive_path.stat().st_mtime_ns == archive_mtime
    assert first.markdown_path.stat().st_mtime_ns == markdown_mtime
    assert ai_path.read_bytes() == ai_before
    assert ai_path.stat().st_mtime_ns == ai_mtime


def test_incremental_export_merges_new_messages_without_duplicates(tmp_path: Path) -> None:
    session = _FakeSession()
    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )
    session.messages.append(
        {
            "chat": "wxid_friend",
            "local_id": 2,
            "server_id": 11,
            "sort_seq": 2,
            "create_time": 1_725_264_100,
            "sender_name": "朋友",
            "type": "文本",
            "type_code": 1,
            "content": "新增",
        }
    )
    session.changed = True

    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )
    session.changed = False
    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )

    payload = json.loads((tmp_path / "archive.json").read_text(encoding="utf-8"))
    assert [message["id"] for message in payload["messages"]] == ["10", "11"]
    assert session.export_calls == 2


def test_existing_voice_attachment_is_not_rewritten(tmp_path: Path) -> None:
    session = _FakeSession()
    session.messages[0].update({"type": "语音", "type_code": 34, "content": "[语音]"})
    first = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
        media="voice",
    )
    payload = json.loads(first.archive_path.read_text(encoding="utf-8"))
    attachment = tmp_path / payload["messages"][0]["attachment"]
    first_mtime = attachment.stat().st_mtime_ns
    assert session.voice_reads == 1

    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
        media="voice",
    )

    assert session.voice_reads == 1
    assert attachment.stat().st_mtime_ns == first_mtime


def test_missing_voice_attachment_is_repaired_without_full_history_reload(tmp_path: Path) -> None:
    session = _FakeSession()
    session.messages[0].update({"type": "语音", "type_code": 34, "content": "[语音]"})
    first = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
        media="voice",
    )
    payload = json.loads(first.archive_path.read_text(encoding="utf-8"))
    attachment = tmp_path / payload["messages"][0]["attachment"]
    attachment.unlink()

    second = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
        media="voice",
    )

    assert session.export_calls == 1
    assert session.voice_reads == 2
    assert attachment.read_bytes() == b"silk"
    assert second.attachment_count == 1


def test_unchanged_export_repairs_derived_ai_without_source_work(tmp_path: Path) -> None:
    session = _FakeSession()
    first = export_chat(session, conversation_id="wxid_friend", conversation_name="朋友", output_dir=tmp_path)
    original = first.archive_path.read_bytes()
    ai_path = tmp_path / "ai.jsonl"
    ai_path.write_text("stale\n", encoding="utf-8")

    export_chat(session, conversation_id="wxid_friend", conversation_name="朋友", output_dir=tmp_path)

    assert session.export_calls == 1
    assert first.archive_path.read_bytes() == original
    assert json.loads(ai_path.read_text(encoding="utf-8").splitlines()[0])["text"] == "你好"


def test_refresh_forces_full_rebuild(tmp_path: Path) -> None:
    session = _FakeSession()
    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )

    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
        refresh=True,
    )

    assert session.export_calls == 2


def test_atomic_output_replaces_archive_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    first = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )
    old_archive = first.archive_path.read_bytes()
    session.messages.append(
        {
            "chat": "wxid_friend",
            "local_id": 2,
            "server_id": 11,
            "sort_seq": 2,
            "create_time": 1_725_264_100,
            "sender_name": "朋友",
            "type": "文本",
            "type_code": 1,
            "content": "新增",
        }
    )
    session.changed = True
    original_replace = exporter_module.os.replace
    calls = 0

    def fail_archive_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption before canonical archive replace")
        return original_replace(source, destination)

    monkeypatch.setattr(exporter_module.os, "replace", fail_archive_replace)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        export_chat(
            session,  # type: ignore[arg-type]
            conversation_id="wxid_friend",
            conversation_name="朋友",
            output_dir=tmp_path,
        )

    assert first.archive_path.read_bytes() == old_archive
    json.loads(first.archive_path.read_text(encoding="utf-8"))


def test_complete_voice_transcript_does_not_initialize_asr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.messages[0].update({"type": "语音", "type_code": 34, "content": "[语音]"})
    first = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
        media="voice",
    )
    payload = json.loads(first.archive_path.read_text(encoding="utf-8"))
    payload["messages"][0]["transcript"] = "已有转写"
    first.archive_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    class _ForbiddenTranscriber:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("ASR should not be initialized")

    monkeypatch.setattr("wechat_local_archive.exporter.SenseVoiceTranscriber", _ForbiddenTranscriber)
    summary = export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
        media="voice",
        transcribe=True,
    )

    assert summary.transcript_count == 1


def test_verify_mode_skips_clean_existing_voice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    session.messages[0].update({"type": "语音", "type_code": 34, "content": "[语音]"})
    first = export_chat(session, conversation_id="wxid_friend", conversation_name="朋友", output_dir=tmp_path, media="voice")
    payload = json.loads(first.archive_path.read_text(encoding="utf-8"))
    payload["messages"][0]["transcript"] = "这是一条正常的完整转写"
    first.archive_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr("wechat_local_archive.exporter.SenseVoiceTranscriber", lambda **kwargs: (_ for _ in ()).throw(AssertionError("primary ASR initialized")))
    monkeypatch.setattr("wechat_local_archive.quality.WhisperVerifier", lambda **kwargs: (_ for _ in ()).throw(AssertionError("Whisper initialized")))
    summary = export_chat(session, conversation_id="wxid_friend", conversation_name="朋友", output_dir=tmp_path,
                          media="voice", transcribe=True, voice_quality="verify")
    assert summary.transcript_count == 1
    assert not json.loads(first.archive_path.read_text(encoding="utf-8"))["messages"][0].get("transcript_reviews")


def test_existing_archive_account_mismatch_fails_closed(tmp_path: Path) -> None:
    session = _FakeSession()
    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )
    session.account_id = "wxid_other"

    with pytest.raises(ValueError, match="different WeChat account"):
        export_chat(
            session,  # type: ignore[arg-type]
            conversation_id="wxid_friend",
            conversation_name="朋友",
            output_dir=tmp_path,
        )


def test_existing_archive_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    session = _FakeSession()
    export_chat(
        session,  # type: ignore[arg-type]
        conversation_id="wxid_friend",
        conversation_name="朋友",
        output_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="different conversation"):
        export_chat(
            session,  # type: ignore[arg-type]
            conversation_id="wxid_other",
            conversation_name="其他",
            output_dir=tmp_path,
        )
