from __future__ import annotations

import wave
from pathlib import Path

import pytest

from wechat_local_archive.archive import ArchiveMessage
from wechat_local_archive.cancellation import CancellationToken, ExportCancelled
from wechat_local_archive.transcribe import (
    SenseVoiceTranscriber,
    TranscriptCache,
    resolve_asr_settings,
    split_wav,
)


def _write_silence(path: Path, seconds: float, rate: int = 24_000) -> None:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * frames)


def test_split_wav_balances_long_voice_without_tiny_tail(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _write_silence(source, 61)
    parts = split_wav(source, tmp_path / "parts", max_seconds=28)
    assert len(parts) == 3
    durations = []
    for part in parts:
        with wave.open(str(part), "rb") as wav:
            durations.append(wav.getnframes() / wav.getframerate())
    assert all(20.2 < duration < 20.5 for duration in durations)
    assert max(durations) <= 28


def test_split_wav_does_not_create_near_empty_remainder(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _write_silence(source, 28.02)
    parts = split_wav(source, tmp_path / "parts", max_seconds=28)
    assert len(parts) == 2
    durations = []
    for part in parts:
        with wave.open(str(part), "rb") as wav:
            durations.append(wav.getnframes() / wav.getframerate())
    assert all(14.0 <= duration <= 14.02 for duration in durations)


@pytest.mark.parametrize(
    ("preset", "batch_size", "threads"),
    [
        ("background", 4, 2),
        ("balanced", 8, 4),
        ("fast", 16, 8),
    ],
)
def test_asr_presets_resolve_expected_resources(preset: str, batch_size: int, threads: int) -> None:
    settings = resolve_asr_settings(preset)
    assert settings.preset == preset
    assert settings.batch_size == batch_size
    assert settings.threads == threads


def test_batch_size_override_keeps_preset_thread_limit() -> None:
    settings = resolve_asr_settings("background", batch_size=11)
    assert settings.batch_size == 11
    assert settings.threads == 2


def test_asr_preset_controls_onnx_model_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def factory(_path: str, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("wechat_local_archive.transcribe.prepare_sensevoice_model", lambda path: path)
    transcriber = SenseVoiceTranscriber(
        preset="background",
        model_factory=factory,
        postprocess=str,
        model_dir=tmp_path,
    )
    transcriber._ensure_model()

    assert captured["batch_size"] == 4
    assert captured["intra_op_num_threads"] == 2
    assert captured["quantize"] is True


def test_transcript_cache_is_shared_across_resource_presets(tmp_path: Path) -> None:
    audio = tmp_path / "voice.silk"
    audio.write_bytes(b"same-audio")
    cache = TranscriptCache(tmp_path / "cache")
    fast = SenseVoiceTranscriber(preset="fast", cache=cache)
    fast.cache.save(audio, fast.model_name, "同一条转写")

    for preset in ("background", "balanced", "fast"):
        transcriber = SenseVoiceTranscriber(preset=preset, cache=cache)
        assert transcriber.cache.load(audio, transcriber.model_name) == "同一条转写"


def _voice_message(index: int) -> ArchiveMessage:
    return ArchiveMessage(
        id=str(index), local_id=index, server_id=index, sort_seq=index,
        timestamp="2026-01-01T00:00:00+08:00", timestamp_unix=index,
        sender="me", type="voice", type_code=34, content="",
        attachment=f"{index}.silk",
    )


def test_cancelled_asr_checkpoints_complete_messages_and_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wechat_local_archive import transcribe as module

    messages = [_voice_message(1), _voice_message(2)]
    for message in messages:
        (tmp_path / message.attachment).write_bytes(message.id.encode())

    monkeypatch.setattr(module, "decode_silk_to_wav", lambda source, target: target.write_bytes(source.read_bytes()))
    monkeypatch.setattr(module, "split_wav", lambda source, destination: [source])
    cache = TranscriptCache(tmp_path / "cache")
    token = CancellationToken()
    calls: list[str] = []

    def model(paths, **kwargs):
        calls.extend(paths)
        return ["recognized"] * len(paths)

    transcriber = SenseVoiceTranscriber(batch_size=1, cache=cache)
    monkeypatch.setattr(transcriber, "_ensure_model", lambda: (model, str))

    def progress(current, total, label):
        if label.startswith("Transcribing batch 2"):
            token.cancel()

    with pytest.raises(ExportCancelled):
        transcriber.transcribe_messages(messages, tmp_path, progress=progress, cancel=token.check)
    assert messages[0].transcript == "recognized"
    assert cache.load(tmp_path / "1.silk", transcriber.model_name) == "recognized"
    assert messages[1].transcript is None
    assert len(calls) == 1

    resumed = SenseVoiceTranscriber(batch_size=1, cache=cache)
    monkeypatch.setattr(resumed, "_ensure_model", lambda: (model, str))
    completed, warnings = resumed.transcribe_messages(messages, tmp_path)
    assert completed == 1
    assert not warnings
    assert len(calls) == 2
    assert [message.transcript for message in messages] == ["recognized", "recognized"]


def test_asr_does_not_cache_incomplete_multichunk_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wechat_local_archive import transcribe as module

    message = _voice_message(1)
    (tmp_path / "1.silk").write_bytes(b"audio")
    monkeypatch.setattr(module, "decode_silk_to_wav", lambda source, target: target.write_bytes(b"wav"))
    monkeypatch.setattr(module, "split_wav", lambda source, destination: [source, source])
    cache = TranscriptCache(tmp_path / "cache")
    transcriber = SenseVoiceTranscriber(batch_size=2, cache=cache)
    calls = 0

    def model(paths, **kwargs):
        nonlocal calls
        if len(paths) == 2:
            raise ValueError("bad chunk")
        calls += 1
        if calls == 2:
            raise ValueError("bad chunk")
        return ["partial"]

    monkeypatch.setattr(transcriber, "_ensure_model", lambda: (model, str))
    completed, warnings = transcriber.transcribe_messages([message], tmp_path)
    assert completed == 0
    assert warnings
    assert message.transcript is None
    assert cache.load(tmp_path / "1.silk", transcriber.model_name) is None
