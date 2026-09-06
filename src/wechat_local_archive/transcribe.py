from __future__ import annotations

import hashlib
import json
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .archive import ArchiveMessage
from .paths import model_cache_dir, transcript_cache_dir


ProgressCallback = Callable[[int, int, str], None]


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Chunk:
    message_index: int
    order: int
    path: Path


class TranscriptCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or transcript_cache_dir()

    def load(self, audio_path: Path, model_name: str) -> str | None:
        target = self._path(audio_path, model_name)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        value = payload.get("transcript") if isinstance(payload, dict) else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def save(self, audio_path: Path, model_name: str, transcript: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(audio_path, model_name)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"model": model_name, "transcript": transcript},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(target)

    def _path(self, audio_path: Path, model_name: str) -> Path:
        digest = hashlib.sha256()
        digest.update(model_name.encode("utf-8"))
        with audio_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return self.root / f"{digest.hexdigest()}.json"


class SenseVoiceTranscriber:
    def __init__(
        self,
        model_name: str = "iic/SenseVoiceSmall-onnx",
        batch_size: int = 16,
        cache: TranscriptCache | None = None,
        model_factory=None,
        postprocess=None,
        model_dir: Path | None = None,
    ) -> None:
        self.model_name = model_name
        self.batch_size = max(1, batch_size)
        self.cache = cache or TranscriptCache()
        self._model_factory = model_factory
        self._postprocess = postprocess
        self.model_dir = model_dir or model_cache_dir() / "sensevoice-small-onnx"
        self._model = None

    def transcribe_messages(
        self,
        messages: list[ArchiveMessage],
        archive_root: Path,
        progress: ProgressCallback | None = None,
    ) -> tuple[int, list[str]]:
        candidates: list[tuple[int, Path]] = []
        warnings: list[str] = []
        for index, message in enumerate(messages):
            if not message.attachment or selected_voice_type(message.type_code) != 34:
                continue
            audio_path = (archive_root / message.attachment).resolve()
            if not audio_path.is_file():
                warnings.append(f"voice attachment missing for message {message.id}")
                continue
            cached = self.cache.load(audio_path, self.model_name)
            if cached:
                message.transcript = cached
                continue
            candidates.append((index, audio_path))
        if not candidates:
            return 0, warnings

        model, postprocess = self._ensure_model()
        completed = 0
        with tempfile.TemporaryDirectory(prefix="wechat-local-voice-") as temp_name:
            temp_root = Path(temp_name)
            chunks: list[_Chunk] = []
            audio_by_message: dict[int, Path] = {}
            for current, (message_index, audio_path) in enumerate(candidates, start=1):
                if progress:
                    progress(current - 1, len(candidates), f"Decoding voice {current}/{len(candidates)}")
                try:
                    wav_path = temp_root / f"voice-{message_index:06d}.wav"
                    decode_silk_to_wav(audio_path, wav_path)
                    audio_by_message[message_index] = audio_path
                    parts = split_wav(wav_path, temp_root / f"parts-{message_index:06d}")
                    chunks.extend(
                        _Chunk(message_index=message_index, order=order, path=path)
                        for order, path in enumerate(parts)
                    )
                except Exception as exc:
                    warnings.append(f"voice decode failed for message {messages[message_index].id}: {exc}")
            if not chunks:
                return 0, warnings

            texts: dict[int, list[tuple[int, str]]] = {}
            total_batches = (len(chunks) + self.batch_size - 1) // self.batch_size
            for batch_index in range(total_batches):
                batch = chunks[batch_index * self.batch_size : (batch_index + 1) * self.batch_size]
                if progress:
                    progress(batch_index, total_batches, f"Transcribing batch {batch_index + 1}/{total_batches}")
                raw: list[object | None]
                try:
                    values = model(
                        [str(item.path) for item in batch],
                        language="zh",
                        textnorm="withitn",
                    )
                    if len(values) != len(batch):
                        raise RuntimeError(
                            f"SenseVoice returned {len(values)} results for {len(batch)} audio chunks"
                        )
                    raw = list(values)
                except Exception as batch_exc:
                    raw = []
                    for item in batch:
                        try:
                            values = model(
                                [str(item.path)],
                                language="zh",
                                textnorm="withitn",
                            )
                            if len(values) != 1:
                                raise RuntimeError(f"SenseVoice returned {len(values)} results for one audio chunk")
                            raw.append(values[0])
                        except Exception as exc:
                            raw.append(None)
                            warnings.append(
                                f"voice inference failed for message {messages[item.message_index].id}: {exc} "
                                f"(batch fallback after: {batch_exc})"
                            )
                for item, value in zip(batch, raw):
                    if value is None:
                        continue
                    try:
                        text = postprocess(value).strip()
                    except Exception:
                        text = str(value).strip()
                    texts.setdefault(item.message_index, []).append((item.order, text))

            for message_index, pieces in texts.items():
                transcript = "".join(text for _order, text in sorted(pieces) if text).strip()
                if not transcript:
                    transcript = "[语音内容未识别]"
                message = messages[message_index]
                message.transcript = transcript
                self.cache.save(audio_by_message[message_index], self.model_name, transcript)
                completed += 1
            if progress:
                progress(total_batches, total_batches, f"Transcribed {completed} voice messages")
        return completed, warnings

    def _ensure_model(self):
        if self._model is not None:
            return self._model, self._postprocess
        if self._model_factory is None or self._postprocess is None:
            try:
                from funasr_onnx import SenseVoiceSmall
                from funasr_onnx.utils.postprocess_utils import rich_transcription_postprocess
            except ImportError as exc:
                raise TranscriptionError(
                    'Voice transcription dependencies are missing. Install with `pip install -e ".[voice]"`.'
                ) from exc
            self._model_factory = SenseVoiceSmall
            self._postprocess = rich_transcription_postprocess
        try:
            prepared = prepare_sensevoice_model(self.model_dir)
            self._model = self._model_factory(
                str(prepared),
                batch_size=self.batch_size,
                quantize=True,
                intra_op_num_threads=min(8, os.cpu_count() or 4),
            )
        except Exception as exc:
            raise TranscriptionError(f"Unable to initialize SenseVoiceSmall: {exc}") from exc
        return self._model, self._postprocess


def prepare_sensevoice_model(model_dir: Path) -> Path:
    required = {
        "config.yaml",
        "am.mvn",
        "tokens.json",
        "model_quant.onnx",
        "chn_jpn_yue_eng_ko_spectok.bpe.model",
    }
    if model_dir.is_dir() and required.issubset({path.name for path in model_dir.iterdir()}):
        return model_dir
    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except ImportError as exc:
        raise TranscriptionError(
            'Model download support is missing. Install with `pip install -e ".[voice]"`.'
        ) from exc
    model_dir.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            "iic/SenseVoiceSmall-onnx",
            local_dir=str(model_dir),
            allow_patterns=[
                "config.yaml",
                "configuration.json",
                "am.mvn",
                "tokens.json",
                "model_quant.onnx",
            ],
        )
        snapshot_download(
            "iic/SenseVoiceSmall",
            local_dir=str(model_dir),
            allow_patterns=["chn_jpn_yue_eng_ko_spectok.bpe.model"],
        )
    except Exception as exc:
        raise TranscriptionError(f"Unable to download SenseVoiceSmall ONNX model: {exc}") from exc
    missing = sorted(name for name in required if not (model_dir / name).is_file())
    if missing:
        raise TranscriptionError(f"SenseVoice model is incomplete; missing: {', '.join(missing)}")
    return model_dir


def decode_silk_to_wav(source: Path, destination: Path, sample_rate: int = 24_000) -> Path:
    try:
        import pysilk
    except ImportError as exc:
        raise TranscriptionError(
            'SILK decoder is missing. Install with `pip install -e ".[voice]"`.'
        ) from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as silk_stream, tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as pcm:
        pysilk.decode(silk_stream, pcm, sample_rate)
        pcm.seek(0)
        with wave.open(str(destination), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm.read())
    return destination


def split_wav(source: Path, output_dir: Path, max_seconds: float = 28.0) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with wave.open(str(source), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        total_frames = wav.getnframes()
        if channels != 1 or width != 2:
            raise TranscriptionError("Expected mono 16-bit PCM after SILK decode")
        frames_per_part = max(1, int(rate * max_seconds))
        if total_frames <= frames_per_part:
            target = output_dir / "000.wav"
            with wave.open(str(target), "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(width)
                out.setframerate(rate)
                out.writeframes(wav.readframes(total_frames))
            return [target]
        part_count = (total_frames + frames_per_part - 1) // frames_per_part
        base_frames = total_frames // part_count
        extra_frames = total_frames % part_count
        paths: list[Path] = []
        for part in range(part_count):
            count = base_frames + (1 if part < extra_frames else 0)
            data = wav.readframes(count)
            target = output_dir / f"{part:03d}.wav"
            with wave.open(str(target), "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(width)
                out.setframerate(rate)
                out.writeframes(data)
            paths.append(target)
        return paths


def selected_voice_type(type_code: int) -> int:
    return (type_code & 0xFF) if type_code > 0xFFFF else (type_code & 0xFFFF)
