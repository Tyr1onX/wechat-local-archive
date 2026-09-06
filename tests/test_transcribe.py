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


def test_split_wav_splits_long_voice_into_short_chunks(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _write_silence(source, 61)
    parts = split_wav(source, tmp_path / "parts", max_seconds=28)
    assert len(parts) == 3
    durations = []
    for part in parts:
        with wave.open(str(part), "rb") as wav:
            durations.append(wav.getnframes() / wav.getframerate())
    assert durations[0] == 28
    assert durations[1] == 28
    assert 4.9 < durations[2] < 5.1
