from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable

from .transcribe import TranscriptCache, TranscriptionError, decode_silk_to_wav, resolve_asr_settings

_MODEL_REPOS = {
    "small": "Systran/faster-whisper-small",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
_DECODE_VERSION = "zh-beam1-vad1-no-context-v1"
_EMPTY = "[语音内容未识别]"


def model_repository(model: str) -> str:
    try:
        return _MODEL_REPOS[model]
    except KeyError as exc:
        raise ValueError(f"Unsupported Whisper model: {model}") from exc


class WhisperVerifier:
    """Optional local second opinion. Resolving a model never sends audio anywhere."""

    def __init__(
        self,
        model: str = "small",
        *,
        preset: str = "background",
        model_dir: Path | None = None,
        allow_download: bool = False,
        cache: TranscriptCache | None = None,
        model_factory=None,
    ) -> None:
        model_repository(model)
        self.model = model
        self.preset = resolve_asr_settings(preset)
        self.model_dir = model_dir
        self.allow_download = allow_download
        self.cache = cache or TranscriptCache()
        self._model_factory = model_factory
        self._model = None
        self._resolved: Path | None = None
        self._source_name: str | None = None

    def _prepare(self) -> Path:
        if self._resolved is not None:
            return self._resolved
        repo = _MODEL_REPOS[self.model]
        if self.model_dir is not None:
            path = Path(self.model_dir).expanduser().resolve()
            if not (path / "model.bin").is_file() or not (path / "config.json").is_file():
                raise TranscriptionError(f"Invalid local Whisper model directory: {path}")
            digest = hashlib.sha256()
            for name in ("config.json", "model.bin"):
                with (path / name).open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            revision = digest.hexdigest()[:16]
        else:
            try:
                from huggingface_hub import snapshot_download
                path = Path(snapshot_download(
                    repo_id=repo,
                    local_files_only=not self.allow_download,
                    allow_patterns=["config.json", "model.bin", "tokenizer.json", "preprocessor_config.json"],
                )).resolve()
            except Exception as exc:
                raise TranscriptionError(
                    f"Whisper {self.model} is not available locally. Install the optional verify dependencies "
                    "and use --download-model once, or supply --model-dir."
                ) from exc
            revision = path.name
        self._resolved = path
        self._source_name = f"faster-whisper:{repo}@{revision}"
        return path

    @property
    def source_name(self) -> str:
        self._prepare()
        assert self._source_name is not None
        return self._source_name

    @property
    def cache_key(self) -> str:
        return f"{self.source_name}|{_DECODE_VERSION}"

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        path = self._prepare()
        if self._model_factory is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptionError(
                    'Whisper verification is optional. Install with `pip install -e ".[voice,verify]"`.'
                ) from exc
            self._model_factory = WhisperModel
        try:
            self._model = self._model_factory(
                str(path), device="cpu", compute_type="int8",
                cpu_threads=min(self.preset.threads, os.cpu_count() or self.preset.threads),
                num_workers=1, local_files_only=True,
            )
        except Exception as exc:
            raise TranscriptionError(f"Unable to initialize local Whisper model: {exc}") from exc
        return self._model

    def transcribe(self, audio: Path, cancel: Callable[[], None] | None = None) -> str:
        if cancel:
            cancel()
        key = self.cache_key
        cached = self.cache.load(audio, key)
        if cached is not None:
            return "" if cached == _EMPTY else cached
        model = self._ensure_model()
        with tempfile.TemporaryDirectory(prefix="wechat-whisper-") as name:
            source = audio
            if audio.suffix.lower() == ".silk":
                source = decode_silk_to_wav(audio, Path(name) / "voice.wav")
            if cancel:
                cancel()
            segments, _info = model.transcribe(
                str(source), language="zh", beam_size=1, vad_filter=True,
                condition_on_previous_text=False,
            )
            parts = []
            for segment in segments:
                if cancel:
                    cancel()
                parts.append(segment.text)
            text = "".join(parts).strip()
        if cancel:
            cancel()
        self.cache.save(audio, key, text or _EMPTY)
        return text
