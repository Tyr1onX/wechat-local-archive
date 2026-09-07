from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .archive import Archive, ArchiveMessage, CURRENT_SCHEMA_VERSION, TranscriptReview, archive_from_dict, selected_media_type
from .cancellation import ExportCancelled
from .transcribe import TranscriptionError
from .whisper_verify import WhisperVerifier, model_repository

ProgressCallback = Callable[[int, int, str], None]
_UNRECOGNIZED = {"", "[语音]", "[语音未转写]", "[语音内容未识别]"}
_REPETITION = re.compile(r"(.{2,12}?)\1{3,}")


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    selected: int
    reviewed: int
    applied: int
    changed: bool
    warnings: tuple[str, ...]


def suspicious_reason(message: ArchiveMessage) -> str | None:
    """Conservative, explainable flags; these are not model confidence scores."""
    text = (message.transcript or "").strip()
    if text in _UNRECOGNIZED:
        return "missing transcript"
    plain = "".join(char for char in text if not unicodedata.category(char).startswith(("P", "Z", "C")))
    if _REPETITION.search(plain):
        return "repeated text"
    if len(plain) > 3:
        return None
    try:
        root = ET.fromstring(message.content)
        node = root.find(".//voicemsg")
        duration = float(node.get("voicelength", "0")) / 1000 if node is not None else 0
    except (ET.ParseError, TypeError, ValueError):
        duration = 0
    return "short text for duration" if duration >= 5 else None


def _audio(root: Path, message: ArchiveMessage) -> Path | None:
    if not message.attachment:
        return None
    candidate = Path(message.attachment)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def review_messages(
    archive: Archive,
    root: Path,
    *,
    ids: tuple[str, ...] = (),
    start: date | None = None,
    end: date | None = None,
    limit: int = 20,
    model: str = "small",
    model_dir: Path | None = None,
    allow_download: bool = False,
    asr_preset: str = "background",
    apply: bool = False,
    review_key: str | None = None,
    progress: ProgressCallback | None = None,
    cancel: Callable[[], None] | None = None,
    verifier: WhisperVerifier | None = None,
) -> ReviewSummary:
    if limit < 1:
        raise ValueError("review limit must be at least 1")
    if start and end and start > end:
        raise ValueError("start date must not be after end date")
    explicit = bool(ids or start or end)
    if apply and not explicit:
        raise ValueError("Applying a review requires explicit message IDs or a date range")
    if review_key is not None and not apply:
        raise ValueError("--review-key requires --apply-review")
    repo_prefix = f"faster-whisper:{model_repository(model)}@"
    if ids:
        known = {message.id for message in archive.messages if selected_media_type(message.type_code) == 34}
        unknown = set(ids) - known
        if unknown:
            raise ValueError("Unknown or non-voice message ID: " + ", ".join(sorted(unknown)))
    selected = []
    for message in archive.messages:
        if selected_media_type(message.type_code) != 34:
            continue
        if ids and message.id not in ids:
            continue
        day = message.timestamp[:10]
        if start and day < start.isoformat():
            continue
        if end and day > end.isoformat():
            continue
        if not explicit and suspicious_reason(message) is None:
            continue
        selected.append(message)
        if len(selected) >= limit:
            break
    if not selected:
        return ReviewSummary(0, 0, 0, False, ())

    root = root.expanduser().resolve()
    warnings: list[str] = []
    reviewed = applied = 0
    changed = False
    engine = verifier
    for index, message in enumerate(selected, 1):
        if cancel:
            cancel()
        if progress:
            progress(index - 1, len(selected), f"Reviewing voice {index}/{len(selected)}")
        if cancel:
            cancel()
        audio = _audio(root, message)
        if audio is None:
            warnings.append(f"Voice attachment is unavailable or outside the archive for message {message.id}")
            continue
        digest = _digest(audio)
        record = None
        if apply and verifier is None and (model_dir is None or review_key is not None):
            matches = [r for r in message.transcript_reviews if r.audio_sha256 == digest and
                       (r.model == review_key if review_key is not None else r.model.startswith(repo_prefix))]
            if len(matches) > 1:
                keys = ", ".join(r.model for r in matches)
                raise ValueError(f"Multiple reviews for message {message.id}; select an exact --review-key: {keys}")
            if matches:
                record = matches[0]
            elif review_key is not None:
                raise ValueError(f"Requested review does not exist for message {message.id}")
        if record is None:
            if engine is None:
                engine = WhisperVerifier(model=model, preset=asr_preset, model_dir=model_dir, allow_download=allow_download)
            key = engine.cache_key
            record = next((r for r in message.transcript_reviews if r.model == key and r.audio_sha256 == digest), None)
        if record is None:
            try:
                text = engine.transcribe(audio, cancel=cancel)
            except ExportCancelled:
                raise
            except TranscriptionError as exc:
                # Dependency/model failures must be visible, not counted as successful reviews.
                raise exc
            except Exception as exc:
                if cancel:
                    cancel()
                warnings.append(f"Whisper failed for message {message.id}: {exc}")
                continue
            record = TranscriptReview(
                model=key, text=text,
                baseline=message.transcript or "",
                baseline_source=message.transcript_source or "legacy/unknown",
                audio_sha256=digest,
            )
            message.transcript_reviews.append(record)
            reviewed += 1
            changed = True
        if apply:
            if not record.text.strip() or record.text.strip() in _UNRECOGNIZED:
                warnings.append(f"Review has no usable text for message {message.id}; original preserved")
            elif message.transcript != record.text or message.transcript_source != record.model:
                message.transcript = record.text
                message.transcript_source = record.model
                applied += 1
                changed = True
        if progress:
            progress(index, len(selected), f"Reviewed {index}/{len(selected)} voices")
    return ReviewSummary(len(selected), reviewed, applied, changed, tuple(warnings))


def review_archive(root: Path, **kwargs) -> ReviewSummary:
    """Review a committed archive without opening WeChat or mutating its audio."""
    root = root.expanduser().resolve()
    source = root / "archive.json"
    try:
        original = source.read_bytes()
        payload = json.loads(original)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read archive.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("archive.json root must be an object")
    archive = archive_from_dict(payload)
    result = review_messages(archive, root, **kwargs)
    if result.changed:
        if source.read_bytes() != original:
            raise RuntimeError("Archive changed during review; reload before saving")
        from .exporter import _write_outputs_atomic
        archive.schema_version = CURRENT_SCHEMA_VERSION
        archive.exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_outputs_atomic(source, root / "chat.md", archive)
    from .ai_export import rebuild_ai_jsonl
    from .html_export import rebuild_html
    rebuild_ai_jsonl(root)
    rebuild_html(root, convert_audio=False)
    return result
