from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .archive import Archive, normalize_payload, relative_attachment, selected_media_type
from .source import SourceSession
from .transcribe import SenseVoiceTranscriber


@dataclass(frozen=True, slots=True)
class ExportSummary:
    archive_path: Path
    markdown_path: Path
    message_count: int
    attachment_count: int
    transcript_count: int
    warnings: tuple[str, ...]


def export_chat(
    session: SourceSession,
    conversation_id: str,
    conversation_name: str,
    output_dir: Path,
    start: date | None = None,
    end: date | None = None,
    media: str = "none",
    transcribe: bool = False,
    batch_size: int = 16,
    progress=None,
) -> ExportSummary:
    if media not in {"none", "voice", "all"}:
        raise ValueError("media must be one of: none, voice, all")
    if transcribe and media == "none":
        media = "voice"

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = session.export_chat_payload(conversation_id)
    archive = normalize_payload(
        payload,
        conversation_id=conversation_id,
        conversation_name=conversation_name,
        start=start,
        end=end,
    )

    warnings: list[str] = []
    attachment_count = 0
    if media != "none":
        attachment_count, media_warnings = _materialize_media(
            session,
            archive,
            output_dir,
            media=media,
            progress=progress,
        )
        warnings.extend(media_warnings)

    transcript_count = 0
    if transcribe:
        transcriber = SenseVoiceTranscriber(batch_size=batch_size)
        transcript_count, voice_warnings = transcriber.transcribe_messages(
            archive.messages,
            archive_root=output_dir,
            progress=progress,
        )
        warnings.extend(voice_warnings)

    archive_path = output_dir / "archive.json"
    markdown_path = output_dir / "chat.md"
    _write_json(archive_path, archive)
    _write_markdown(markdown_path, archive)
    return ExportSummary(
        archive_path=archive_path,
        markdown_path=markdown_path,
        message_count=len(archive.messages),
        attachment_count=attachment_count,
        transcript_count=transcript_count,
        warnings=tuple(warnings),
    )


def _materialize_media(
    session: SourceSession,
    archive: Archive,
    output_dir: Path,
    media: str,
    progress=None,
) -> tuple[int, list[str]]:
    supported = {34} if media == "voice" else {3, 34, 43, 49}
    targets = [message for message in archive.messages if selected_media_type(message.type_code) in supported]
    if not targets:
        return 0, []

    assets_dir = output_dir / "assets"
    downloader = session.media_downloader(assets_dir)
    warnings: list[str] = []
    count = 0
    for index, message in enumerate(targets, start=1):
        base_type = selected_media_type(message.type_code)
        category = {3: "image", 34: "voice", 43: "video", 49: "file"}.get(base_type, "other")
        destination = assets_dir / category
        destination.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(index - 1, len(targets), f"Extracting media {index}/{len(targets)}")
        try:
            if base_type == 3:
                if session.cfg_dword is None:
                    warnings.append(
                        f"image key is not cached; skipping image for message {message.id} to preserve offline-only mode"
                    )
                    continue
                resolved = downloader.download_image(
                    archive.conversation_id,
                    message.local_id,
                    save_dir=str(destination),
                )
            elif base_type == 34:
                resolved = downloader.download_voice(
                    archive.conversation_id,
                    message.local_id,
                    save_dir=str(destination),
                )
            elif base_type == 43:
                resolved = downloader.download_video(
                    archive.conversation_id,
                    message.local_id,
                    save_dir=str(destination),
                )
            else:
                resolved = downloader.download_file(
                    archive.conversation_id,
                    message.local_id,
                    save_dir=str(destination),
                )
        except Exception as exc:
            warnings.append(f"{category} extraction failed for message {message.id}: {exc}")
            continue
        if not resolved:
            warnings.append(f"{category} is not available locally for message {message.id}")
            continue
        resolved_path = Path(resolved)
        if not resolved_path.is_file():
            warnings.append(f"{category} extractor returned a missing file for message {message.id}")
            continue
        message.attachment = relative_attachment(resolved_path, output_dir)
        count += 1
    if progress:
        progress(len(targets), len(targets), f"Extracted {count} attachments")
    return count, warnings


def _write_json(path: Path, archive: Archive) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(archive.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_markdown(path: Path, archive: Archive) -> None:
    lines = [
        f"# {archive.conversation_name}",
        "",
        f"- 会话：`{archive.conversation_id}`",
        f"- 导出时间：{archive.exported_at}",
        f"- 消息数：{len(archive.messages)}",
        "",
    ]
    for message in archive.messages:
        lines.append(f"## {message.timestamp} · {message.sender or '未知'}")
        lines.append("")
        if message.transcript:
            lines.append(message.transcript)
        elif message.content:
            lines.append(message.content)
        else:
            lines.append(f"[{message.type}]")
        if message.attachment:
            if selected_media_type(message.type_code) == 3:
                lines.extend(["", f"![图片]({message.attachment})"])
            elif selected_media_type(message.type_code) == 34:
                lines.extend(["", f"[语音文件]({message.attachment})"])
            else:
                lines.extend(["", f"[附件]({message.attachment})"])
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
