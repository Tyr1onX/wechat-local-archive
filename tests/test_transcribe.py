from __future__ import annotations

import wave
from pathlib import Path

from wechat_local_archive.transcribe import split_wav


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
