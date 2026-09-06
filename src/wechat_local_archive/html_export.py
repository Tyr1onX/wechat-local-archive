from __future__ import annotations

import filecmp
import hashlib
import html
import json
import os
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .ai_export import _asset_path, iter_ai_rows
from .archive import Archive, archive_from_dict

HTML_FILENAME = "chat.html"
_AUDIO_DIR = Path("assets") / "reader-audio"
_AUDIO_VERSION = b"wechat-local-archive-reader-mp3-v1\0"
ProgressCallback = Callable[[int, int, str], None]


def _local_asset(root: Path, attachment: str | None) -> Path | None:
    relative = _asset_path(root, attachment)
    if relative is None:
        return None
    candidate = (root / relative).resolve()
    return candidate if candidate.is_file() else None


def _reader_url(relative: str) -> str:
    # Encode each segment, not the separators; never construct a URL from raw XML.
    return "/".join(quote(part, safe="") for part in Path(relative).parts)


def _audio_path(root: Path, source: Path) -> Path:
    digest = hashlib.sha256()
    digest.update(_AUDIO_VERSION)
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return root / _AUDIO_DIR / f"{digest.hexdigest()}.mp3"


def _convert_audio(source: Path, destination: Path, cancel: Callable[[], None] | None = None) -> None:
    from .transcribe import decode_silk_to_wav

    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("Audio conversion requires the existing imageio-ffmpeg dependency") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reader-audio-", dir=str(destination.parent)) as name:
        temporary = Path(name)
        wav = decode_silk_to_wav(source, temporary / "input.wav")
        if cancel:
            cancel()
        encoded = temporary / "audio.mp3"
        result = subprocess.run(
            [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(wav), "-vn", "-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame",
             "-b:a", "48k", str(encoded)],
            capture_output=True, timeout=120,
        )
        if result.returncode or not encoded.is_file() or not encoded.stat().st_size:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "MP3 conversion failed")
        if cancel:
            cancel()
        os.replace(encoded, destination)


def render_html(
    archive: Archive,
    root: Path,
    *,
    convert_audio: bool = True,
    progress: ProgressCallback | None = None,
    cancel: Callable[[], None] | None = None,
) -> tuple[str, tuple[str, ...]]:
    root = root.expanduser().resolve()
    rows = list(iter_ai_rows(archive, root))
    warnings: list[str] = []
    voice_count = sum(row["type"] == "voice" and "asset" in row for row in rows)
    current = 0
    for row in rows:
        if cancel:
            cancel()
        if "asset" not in row:
            continue
        source = _local_asset(root, row["asset"])
        if source is None:
            row.pop("asset", None)
            continue
        row["asset"] = _reader_url(source.relative_to(root).as_posix())
        if row["type"] != "voice":
            continue
        if source.suffix.lower() == ".silk":
            target = _audio_path(root, source)
            if convert_audio and (not target.is_file() or not target.stat().st_size):
                try:
                    _convert_audio(source, target, cancel=cancel)
                except Exception as exc:
                    # Keep the transcript and original file available.
                    if cancel:
                        cancel()
                    warnings.append(f"Audio conversion failed for {source.name}: {exc}")
            if target.is_file() and target.stat().st_size:
                row["audio"] = _reader_url(target.relative_to(root).as_posix())
        elif source.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".opus"}:
            row["audio"] = row["asset"]
        current += 1
        if progress:
            progress(current, voice_count, f"Preparing audio {current}/{voice_count}")
    payload = {"title": archive.conversation_name, "messages": rows}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = files("wechat_local_archive").joinpath("reader.html").read_text(encoding="utf-8")
    script = template.split('<script id="reader-code">', 1)[1].split("</script>", 1)[0]
    script_hash = hashlib.sha256(script.encode("utf-8")).digest()
    import base64
    csp_hash = base64.b64encode(script_hash).decode("ascii")
    return (template.replace("__READER_TITLE__", html.escape(archive.conversation_name, quote=True))
            .replace("__READER_DATA__", encoded)
            .replace("__READER_SCRIPT_HASH__", csp_hash)), tuple(warnings)


def write_html(
    archive: Archive,
    root: Path,
    *,
    convert_audio: bool = True,
    progress: ProgressCallback | None = None,
    cancel: Callable[[], None] | None = None,
) -> tuple[Path, tuple[str, ...]]:
    root = root.expanduser().resolve()
    target = root / HTML_FILENAME
    content, warnings = render_html(archive, root, convert_audio=convert_audio, progress=progress, cancel=cancel)
    if cancel:
        cancel()
    root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=HTML_FILENAME + ".", suffix=".tmp", dir=str(root))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_file() and filecmp.cmp(temporary, target, shallow=False):
            return target, warnings
        if cancel:
            cancel()
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, warnings


def rebuild_html(
    root: Path,
    *,
    convert_audio: bool = True,
    progress: ProgressCallback | None = None,
    cancel: Callable[[], None] | None = None,
) -> tuple[Path, tuple[str, ...]]:
    root = root.expanduser().resolve()
    try:
        payload = json.loads((root / "archive.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read archive.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("archive.json root must be an object")
    return write_html(archive_from_dict(payload), root, convert_audio=convert_audio, progress=progress, cancel=cancel)
