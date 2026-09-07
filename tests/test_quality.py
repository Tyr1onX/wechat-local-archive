from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from wechat_local_archive.archive import Archive, ArchiveMessage, TranscriptReview, archive_from_dict, merge_archives, refresh_archive
from wechat_local_archive.cancellation import CancellationToken, ExportCancelled
from wechat_local_archive.exporter import render_markdown
from wechat_local_archive.quality import review_archive, review_messages, suspicious_reason
from wechat_local_archive.transcribe import TranscriptCache, TranscriptionError
from wechat_local_archive.whisper_verify import WhisperVerifier


def _message(number: int, text: str = "原始转写") -> ArchiveMessage:
    return ArchiveMessage(
        id=str(number), local_id=number, server_id=number, sort_seq=number,
        timestamp="2026-01-01T00:00:00+08:00", timestamp_unix=number,
        sender="我", type="voice", type_code=34,
        content='<msg><voicemsg voicelength="6000" /></msg>',
        attachment=f"assets/voice/{number}.silk", transcript=text,
    )


def _archive(messages):
    return Archive(4, "2026-01-01T00:00:00+08:00", "account", "我", "friend", "朋友", messages)


def _fixture(root, messages):
    (root / "assets" / "voice").mkdir(parents=True, exist_ok=True)
    archive = _archive(messages)
    for message in messages:
        (root / message.attachment).write_bytes(message.id.encode())
    (root / "archive.json").write_text(json.dumps(archive.to_dict(), ensure_ascii=False), encoding="utf-8")
    (root / "chat.md").write_text(render_markdown(archive), encoding="utf-8")
    return archive


class FakeVerifier:
    cache_key = "faster-whisper:test@revision|zh-beam1-v1"

    def __init__(self, text="复核结果"):
        self.text = text
        self.calls = []

    def transcribe(self, audio, cancel=None):
        self.calls.append(audio.name)
        if cancel:
            cancel()
        return self.text


def test_suspicious_rules_are_explainable():
    message = _message(1, "嗯")
    assert suspicious_reason(message) == "short text for duration"
    message.transcript = "你好你好你好你好"
    assert suspicious_reason(message) == "repeated text"
    message.transcript = "[语音内容未识别]"
    assert suspicious_reason(message) == "missing transcript"
    message.transcript = "今天下午我们去图书馆"
    assert suspicious_reason(message) is None
    message.transcript = "嗯"
    message.content = "<msg><voicemsg /></msg>"
    assert suspicious_reason(message) is None


def test_review_preserves_original_and_is_idempotent(tmp_path):
    messages = [_message(1), _message(2)]
    archive = _fixture(tmp_path, messages)
    engine = FakeVerifier()
    result = review_messages(archive, tmp_path, ids=("1",), verifier=engine)
    assert result.selected == result.reviewed == 1
    assert result.applied == 0 and result.changed
    assert messages[0].transcript == "原始转写"
    record = messages[0].transcript_reviews[0]
    assert record.baseline == "原始转写"
    assert record.baseline_source == "legacy/unknown"
    assert record.text == "复核结果"
    assert record.audio_sha256 == hashlib.sha256(b"1").hexdigest()
    assert messages[1].transcript_reviews == []
    again = review_messages(archive, tmp_path, ids=("1",), verifier=engine)
    assert not again.changed and again.reviewed == 0
    assert len(engine.calls) == 1
    applied = review_messages(archive, tmp_path, ids=("1",), verifier=engine, apply=True)
    assert applied.applied == 1 and applied.reviewed == 0
    assert messages[0].transcript == "复核结果"
    assert messages[0].transcript_source == engine.cache_key
    assert messages[0].transcript_reviews[0].baseline == "原始转写"
    assert not review_messages(archive, tmp_path, ids=("1",), verifier=engine, apply=True).changed


def test_review_selection_limit_and_no_model_for_no_candidates(tmp_path, monkeypatch):
    archive = _fixture(tmp_path, [_message(1, "嗯"), _message(2, "正常的长句子"), _message(3, "[语音未转写]")])
    engine = FakeVerifier()
    result = review_messages(archive, tmp_path, limit=1, verifier=engine)
    assert result.selected == 1 and engine.calls == ["1.silk"]
    assert review_messages(archive, tmp_path, ids=("2",), verifier=engine).selected == 1
    with pytest.raises(ValueError, match="Unknown or non-voice"):
        review_messages(archive, tmp_path, ids=("missing",), verifier=engine)
    with pytest.raises(ValueError, match="requires explicit"):
        review_messages(archive, tmp_path, apply=True, verifier=engine)
    with pytest.raises(ValueError, match="limit"):
        review_messages(archive, tmp_path, limit=0, verifier=engine)
    monkeypatch.setattr("wechat_local_archive.quality.WhisperVerifier", lambda **kwargs: (_ for _ in ()).throw(AssertionError("model initialized")))
    empty = _fixture(tmp_path / "empty", [_message(1, "正常的长句子")])
    assert not review_messages(empty, tmp_path / "empty").changed


def test_review_missing_audio_and_empty_result_preserve_original(tmp_path):
    archive = _fixture(tmp_path, [_message(1)])
    (tmp_path / "assets/voice/1.silk").unlink()
    result = review_messages(archive, tmp_path, ids=("1",), verifier=FakeVerifier())
    assert result.warnings and not result.changed
    outside = tmp_path / "outside.silk"
    outside.write_bytes(b"outside")
    archive.messages[0].attachment = "../outside.silk"
    assert review_messages(archive, tmp_path, ids=("1",), verifier=FakeVerifier()).warnings
    archive.messages[0].attachment = "assets/voice/1.silk"
    (tmp_path / archive.messages[0].attachment).write_bytes(b"1")
    result = review_messages(archive, tmp_path, ids=("1",), verifier=FakeVerifier(""), apply=True)
    assert result.reviewed == 1 and result.applied == 0
    assert archive.messages[0].transcript == "原始转写"
    assert archive.messages[0].transcript_reviews[0].text == ""


def test_apply_stored_review_needs_no_model_and_resolves_ambiguity(tmp_path, monkeypatch):
    archive = _fixture(tmp_path, [_message(1)])
    digest = hashlib.sha256(b"1").hexdigest()
    first = TranscriptReview("faster-whisper:Systran/faster-whisper-small@old|v1", "旧结果", "原始转写", "legacy/unknown", digest)
    archive.messages[0].transcript_reviews.append(first)
    monkeypatch.setattr("wechat_local_archive.quality.WhisperVerifier", lambda **kwargs: (_ for _ in ()).throw(AssertionError("model initialized")))
    result = review_messages(archive, tmp_path, ids=("1",), apply=True)
    assert result.applied == 1 and result.reviewed == 0
    assert archive.messages[0].transcript == "旧结果"
    second = TranscriptReview("faster-whisper:Systran/faster-whisper-small@new|v1", "新结果", "原始转写", "legacy/unknown", digest)
    archive.messages[0].transcript_reviews.append(second)
    with pytest.raises(ValueError, match="Multiple reviews"):
        review_messages(archive, tmp_path, ids=("1",), apply=True)
    result = review_messages(archive, tmp_path, ids=("1",), apply=True, review_key=second.model)
    assert result.applied == 1 and archive.messages[0].transcript == "新结果"
    with pytest.raises(ValueError, match="does not exist"):
        review_messages(archive, tmp_path, ids=("1",), apply=True, review_key="missing")
    with pytest.raises(ValueError, match="requires --apply-review"):
        review_messages(archive, tmp_path, ids=("1",), review_key=second.model)


def test_schema_roundtrip_and_refresh_keep_review_provenance():
    old = _archive([_message(1)])
    old.messages[0].transcript_source = "original-model"
    old.messages[0].transcript_reviews = [TranscriptReview("second", "复核", "原始转写", "original-model", "a" * 64)]
    loaded = archive_from_dict(old.to_dict())
    assert loaded.messages[0].transcript_reviews == old.messages[0].transcript_reviews
    legacy = old.to_dict()
    legacy["schema_version"] = 3
    legacy["messages"][0].pop("transcript_source")
    legacy["messages"][0].pop("transcript_reviews")
    assert archive_from_dict(legacy).messages[0].transcript_source is None
    fresh = _archive([_message(1, None), _message(2)])
    merged = merge_archives(old, fresh)
    assert merged.messages[0].transcript_reviews == old.messages[0].transcript_reviews
    fresh_for_refresh = _archive([_message(1, None)])
    fresh_for_refresh.messages[0].attachment = None
    refreshed = refresh_archive(old, fresh_for_refresh)
    assert refreshed.messages[0].transcript is None
    assert refreshed.messages[0].attachment is None
    assert refreshed.messages[0].transcript_source is None
    assert refreshed.messages[0].transcript_reviews == old.messages[0].transcript_reviews
    changed = _archive([_message(1, None)])
    changed.messages[0].content = "<msg><voicemsg voicelength='7000'/></msg>"
    assert refresh_archive(old, changed).messages[0].transcript_reviews == []
    broken = old.to_dict()
    broken["messages"][0]["transcript_reviews"] = [{"model": "only"}]
    with pytest.raises(ValueError, match="review"):
        archive_from_dict(broken)


def test_standalone_review_is_atomic_and_rebuilds_derivatives(tmp_path, monkeypatch):
    archive = _fixture(tmp_path, [_message(1)])
    original = (tmp_path / "archive.json").read_bytes()
    engine = FakeVerifier()
    result = review_archive(tmp_path, ids=("1",), verifier=engine)
    assert result.changed and result.applied == 0
    assert archive_from_dict(json.loads((tmp_path / "archive.json").read_text(encoding="utf-8"))).messages[0].transcript == "原始转写"
    from wechat_local_archive.verify import verify_archive
    assert verify_archive(tmp_path).ok
    after = (tmp_path / "archive.json").read_bytes()
    assert after != original
    assert not review_archive(tmp_path, ids=("1",), verifier=engine).changed
    assert (tmp_path / "archive.json").read_bytes() == after
    assert len(engine.calls) == 1
    token = CancellationToken()
    token.cancel()
    with pytest.raises(ExportCancelled):
        review_archive(tmp_path, ids=("1",), verifier=engine, cancel=token.check)
    assert (tmp_path / "archive.json").read_bytes() == after
    assert verify_archive(tmp_path).ok


def test_review_cancellation_never_commits_partial_results(tmp_path):
    _fixture(tmp_path, [_message(1), _message(2)])
    original = (tmp_path / "archive.json").read_bytes()
    token = CancellationToken()
    engine = FakeVerifier()
    def progress(current, total, label):
        if label.startswith("Reviewing voice 2"):
            token.cancel()
    with pytest.raises(ExportCancelled):
        review_archive(tmp_path, ids=("1", "2"), verifier=engine, progress=progress, cancel=token.check)
    assert (tmp_path / "archive.json").read_bytes() == original
    assert engine.calls == ["1.silk"]


def test_whisper_model_cache_is_isolated_and_lazy(tmp_path, monkeypatch):
    from wechat_local_archive import whisper_verify as module
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.bin").write_bytes(b"fake-weights")
    audio = tmp_path / "voice.silk"
    audio.write_bytes(b"audio")
    cache = TranscriptCache(tmp_path / "cache")
    calls = []

    class Model:
        def transcribe(self, path, **kwargs):
            calls.append(kwargs)
            return iter([type("Segment", (), {"text": "复核"})()]), None

    def factory(path, **kwargs):
        assert kwargs["device"] == "cpu"
        assert kwargs["compute_type"] == "int8"
        assert kwargs["cpu_threads"] == 2
        return Model()

    monkeypatch.setattr(module, "decode_silk_to_wav", lambda source, target: target.write_bytes(b"wav") or target)
    first = WhisperVerifier(model_dir=model_dir, cache=cache, model_factory=factory)
    assert first.transcribe(audio) == "复核"
    assert first.transcribe(audio) == "复核"
    assert len(calls) == 1
    second = WhisperVerifier(model_dir=model_dir, cache=cache, model_factory=factory)
    assert second.transcribe(audio) == "复核"
    assert len(calls) == 1
    assert WhisperVerifier(model="large-v3-turbo", model_dir=model_dir, cache=cache, model_factory=factory).transcribe(audio) == "复核"
    assert len(calls) == 2
    assert cache.load(audio, "iic/SenseVoiceSmall-onnx") is None
    assert first.cache_key != WhisperVerifier(model="large-v3-turbo", model_dir=model_dir).cache_key
