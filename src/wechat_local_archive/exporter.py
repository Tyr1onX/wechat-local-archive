from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
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
        _new_transcripts, voice_warnings = transcriber.transcribe_messages(
            archive.messages,
            archive_root=output_dir,
            progress=progress,
        )
        transcript_count = sum(1 for message in archive.messages if message.transcript)
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
    if media == "voice":
        targets = [message for message in archive.messages if selected_media_type(message.type_code) == 34]
    else:
        targets = [
            message
            for message in archive.messages
            if selected_media_type(message.type_code) in {3, 34, 43}
            or (selected_media_type(message.type_code) == 49 and _appmsg_type(message.content) == 6)
        ]
    if not targets:
        return 0, []

    assets_dir = output_dir / "assets"
    warnings: list[str] = []
    count = 0

    voice_targets = [message for message in targets if selected_media_type(message.type_code) == 34]
    if voice_targets:
        voice_count, voice_warnings = _materialize_voices_bulk(
            session,
            archive,
            output_dir,
            voice_targets,
            progress=progress,
        )
        count += voice_count
        warnings.extend(voice_warnings)

    image_targets = [message for message in targets if selected_media_type(message.type_code) == 3]
    if image_targets:
        image_count, image_warnings = _materialize_images_bulk(
            session,
            archive,
            output_dir,
            image_targets,
            progress=progress,
        )
        count += image_count
        warnings.extend(image_warnings)

    file_targets = [message for message in targets if selected_media_type(message.type_code) == 49]
    if file_targets:
        file_count, file_warnings = _materialize_files_bulk(
            session,
            archive,
            output_dir,
            file_targets,
            progress=progress,
        )
        count += file_count
        warnings.extend(file_warnings)

    other_targets = [
        message
        for message in targets
        if selected_media_type(message.type_code) not in {3, 34, 49}
    ]
    if not other_targets:
        return count, warnings

    for index, message in enumerate(other_targets, start=1):
        base_type = selected_media_type(message.type_code)
        category = {43: "video"}.get(base_type, "other")
        destination = assets_dir / category
        destination.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(index - 1, len(other_targets), f"Extracting {category} {index}/{len(other_targets)}")
        try:
            resolved_path = session.resolve_video(
                archive.conversation_id,
                message.local_id,
                destination,
            )
        except Exception as exc:
            warnings.append(f"{category} extraction failed for message {message.id}: {exc}")
            continue
        if resolved_path is None:
            warnings.append(f"{category} is not available locally for message {message.id}")
            continue
        message.attachment = relative_attachment(resolved_path, output_dir)
        count += 1
    if progress:
        progress(len(other_targets), len(other_targets), f"Extracted {count} attachments")
    return count, warnings


def _materialize_images_bulk(
    session: SourceSession,
    archive: Archive,
    output_dir: Path,
    targets: list,
    progress=None,
) -> tuple[int, list[str]]:
    destination = output_dir / "assets" / "image"
    destination.mkdir(parents=True, exist_ok=True)
    expected = {message.media_md5.lower(): message for message in targets if message.media_md5}
    if not expected:
        return 0, [f"image metadata is missing for message {message.id}" for message in targets]

    found = session.find_image_sources(archive.conversation_id, set(expected))
    warnings: list[str] = []
    count = 0
    for index, message in enumerate(targets, start=1):
        if progress:
            progress(index - 1, len(targets), f"Extracting image {index}/{len(targets)}")
        digest = (message.media_md5 or "").lower()
        choices = found.get(digest, {})
        source = choices.get("high") or choices.get("normal") or choices.get("thumb")
        if source is None:
            warnings.append(f"image is not available locally for message {message.id}")
            continue
        try:
            data = session.decrypt_image(source)
            suffix = "_thumb" if source == choices.get("thumb") else ""
            if data[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif data[:4] == b"\x89PNG":
                ext = "png"
            elif data[:3] == b"GIF":
                ext = "gif"
            elif data[:4] == b"wxgf":
                jpg = _convert_wxgf_to_jpg(data)
                if jpg is not None:
                    data, ext = jpg, "jpg"
                else:
                    ext = "wxgf"
            else:
                ext = "img"
            target = destination / f"{archive.conversation_id}_{message.local_id}{suffix}.{ext}"
            target.write_bytes(data)
            message.attachment = relative_attachment(target, output_dir)
            count += 1
        except Exception as exc:
            warnings.append(f"image extraction failed for message {message.id}: {exc}")
    if progress:
        progress(len(targets), len(targets), f"Extracted {count}/{len(targets)} images")
    return count, warnings


def _materialize_files_bulk(
    session: SourceSession,
    archive: Archive,
    output_dir: Path,
    targets: list,
    progress=None,
) -> tuple[int, list[str]]:
    destination = output_dir / "assets" / "file"
    destination.mkdir(parents=True, exist_ok=True)
    requested_names = {
        _extract_xml_text(message.content, ("title",)).replace("\\", "/").split("/")[-1].strip()
        for message in targets
    }
    by_name = session.find_file_sources(requested_names)

    warnings: list[str] = []
    count = 0
    for index, message in enumerate(targets, start=1):
        if progress:
            progress(index - 1, len(targets), f"Extracting file {index}/{len(targets)}")
        title = _extract_xml_text(message.content, ("title",))
        name = title.replace("\\", "/").split("/")[-1].strip()
        candidates = by_name.get(name.casefold(), []) if name else []
        if not candidates:
            warnings.append(f"file is not available locally for message {message.id}")
            continue
        month = datetime.fromtimestamp(message.timestamp_unix).strftime("%Y-%m")
        source = next((path for path in candidates if month in path.parts), None)
        if source is None:
            source = max(candidates, key=lambda path: path.stat().st_mtime)
        safe_name = Path(name).name or "attachment.bin"
        target = destination / f"{archive.conversation_id}_{message.local_id}_{safe_name}"
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            warnings.append(f"file extraction failed for message {message.id}: {exc}")
            continue
        message.attachment = relative_attachment(target, output_dir)
        count += 1
    if progress:
        progress(len(targets), len(targets), f"Extracted {count}/{len(targets)} files")
    return count, warnings


def _convert_wxgf_to_jpg(data: bytes) -> bytes | None:
    starts = [
        offset
        for offset in (data.find(b"\x00\x00\x01"), data.find(b"\x00\x00\x00\x01"))
        if offset >= 0
    ]
    if not starts:
        return None
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    with tempfile.TemporaryDirectory(prefix="wechat-local-wxgf-") as temp_name:
        source = Path(temp_name) / "image.hevc"
        target = Path(temp_name) / "image.jpg"
        source.write_bytes(data[min(starts) :])
        try:
            result = subprocess.run(
                [
                    executable,
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "hevc",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    str(target),
                ],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            return None
        if result.returncode != 0 or not target.is_file():
            return None
        output = target.read_bytes()
        return output if output[:3] == b"\xff\xd8\xff" else None


def _materialize_voices_bulk(
    session: SourceSession,
    archive: Archive,
    output_dir: Path,
    targets: list,
    progress=None,
) -> tuple[int, list[str]]:
    destination = output_dir / "assets" / "voice"
    destination.mkdir(parents=True, exist_ok=True)
    by_server = {int(message.server_id): message for message in targets if int(message.server_id) > 0}
    by_local = {int(message.local_id): message for message in targets if int(message.local_id) > 0}
    matched: set[str] = set()
    rows = session.iter_voice_rows(archive.conversation_id)
    for row in rows:
        message = None
        if row.server_id > 0:
            message = by_server.get(row.server_id)
        if message is None and row.local_id > 0:
            message = by_local.get(row.local_id)
        if message is None or message.id in matched:
            continue
        target = destination / f"{archive.conversation_id}_{message.local_id}.silk"
        target.write_bytes(row.data)
        message.attachment = relative_attachment(target, output_dir)
        matched.add(message.id)
    warnings = [
        f"voice is not available locally for message {message.id}"
        for message in targets
        if message.id not in matched
    ]
    if progress:
        progress(len(matched), len(targets), f"Extracted {len(matched)}/{len(targets)} voices")
    return len(matched), warnings


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
        lines.append(f"## {message.timestamp} · {_display_sender(archive, message)}")
        lines.append("")
        lines.append(_render_message_content(message))
        if message.attachment:
            if selected_media_type(message.type_code) == 3:
                lines.extend(["", f"![图片]({message.attachment})"])
            elif selected_media_type(message.type_code) == 34:
                lines.extend(["", f"[语音文件]({message.attachment})"])
            else:
                lines.extend(["", f"[附件]({message.attachment})"])
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _display_sender(archive: Archive, message) -> str:
    if message.type_code == 10000:
        return "系统"
    if archive.account_name and message.sender == archive.account_name:
        return "我"
    if message.sender and not archive.conversation_id.endswith("@chatroom"):
        return archive.conversation_name
    return message.sender or "未知"


def _render_message_content(message) -> str:
    if message.transcript:
        return message.transcript

    base_type = selected_media_type(message.type_code)
    if message.type_code == 10000:
        return _extract_xml_text(message.content, ("content",)) or "[系统消息]"
    if base_type == 1:
        return message.content or "[文本]"
    if base_type == 3:
        return "[图片]"
    if base_type == 34:
        return "[语音]"
    if base_type == 43:
        return "[视频]"
    if base_type == 47:
        return "[表情]"
    if base_type == 48:
        label = _extract_xml_text(message.content, ("label", "poiname"))
        return f"[位置] {label}" if label else "[位置]"
    if base_type == 49:
        title = _extract_xml_text(message.content, ("title", "des"))
        app_type = _appmsg_type(message.content)
        label = {5: "链接", 6: "文件", 19: "聊天记录", 33: "小程序", 57: "引用消息"}.get(app_type, "应用消息")
        return f"[{label}] {title}" if title else f"[{label}]"
    if message.content and not message.content.lstrip().startswith("<"):
        return message.content
    return f"[{message.type}]"


def _appmsg_type(content: str) -> int | None:
    value = _extract_xml_text(content, ("type",))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_xml_text(content: str, tags: tuple[str, ...]) -> str:
    if not content:
        return ""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        root = None
    if root is not None:
        for tag in tags:
            node = root.find(f".//{tag}")
            if node is not None and node.text and node.text.strip():
                return html.unescape(node.text.strip())
    for tag in tags:
        match = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", content, re.I | re.S)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if text:
                return html.unescape(text)
    return ""
