from __future__ import annotations

import filecmp
import html
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

from .archive import Archive, ArchiveMessage, archive_from_dict, selected_media_type
from .exporter import _appmsg_type, _display_sender, _render_message_content


AI_FILENAME = "ai.jsonl"
_MARKUP = re.compile(r"^\s*<(?:[A-Za-z_][\w:.-]*\b[^>]*>|[!?])")
_SEMANTIC_TAGS = ("content", "text", "title", "des", "label", "poiname")
_SIMPLE_TAGS = {"b", "i", "u", "strong", "em", "span", "font", "br", "p"}


def _message_type(message: ArchiveMessage) -> str:
    if message.type_code == 10000:
        return "system"
    kind = selected_media_type(message.type_code)
    if kind == 49:
        return {5: "link", 6: "file", 19: "chat_history", 33: "mini_program", 57: "reply"}.get(
            _appmsg_type(message.content), "app"
        )
    return {1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker", 48: "location"}.get(
        kind, "unknown"
    )


def _plain_text(value: str, fallback: str) -> str:
    """Keep readable content, never copy structured message XML into AI text."""
    text = value.strip()
    for _ in range(4):
        if not _MARKUP.match(text):
            return text or fallback
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return fallback
        candidate = ""
        for tag in _SEMANTIC_TAGS:
            node = root if root.tag == tag else root.find(f".//{tag}")
            if node is not None:
                candidate = "".join(node.itertext()).strip()
                if candidate:
                    break
        if not candidate and root.tag in _SIMPLE_TAGS:
            candidate = "".join(root.itertext()).strip()
        if not candidate:
            return fallback
        text = html.unescape(candidate).strip()
    return fallback if _MARKUP.match(text) else text or fallback


def _asset_path(root: Path, attachment: str | None) -> str | None:
    if not attachment:
        return None
    candidate = Path(attachment)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return relative.as_posix()


def iter_ai_rows(archive: Archive, root: Path) -> Iterator[dict[str, str]]:
    """One compact, ordered record per source message; no archive metadata is copied."""
    for message in archive.messages:
        kind = _message_type(message)
        if kind == "voice":
            text = message.transcript or "[语音未转写]"
        else:
            text = _render_message_content(message)
        fallback = "[语音未转写]" if kind == "voice" else "[未解析的消息]"
        row = {
            "time": message.timestamp,
            "sender": _display_sender(archive, message),
            "type": kind,
            "text": _plain_text(text, fallback),
        }
        asset = _asset_path(root, message.attachment)
        if asset is not None:
            row["asset"] = asset
        yield row


def write_ai_jsonl(archive: Archive, root: Path) -> Path:
    """Atomically rebuild the derived file without rewriting an unchanged result."""
    root = root.expanduser().resolve()
    target = root / AI_FILENAME
    root.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=AI_FILENAME + ".", suffix=".tmp", dir=str(root))
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for row in iter_ai_rows(archive, root):
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_file() and filecmp.cmp(temporary, target, shallow=False):
            return target
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def rebuild_ai_jsonl(root: Path) -> Path:
    """Regenerate from the committed archive alone; no WeChat or ASR is required."""
    root = root.expanduser().resolve()
    try:
        payload = json.loads((root / "archive.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read archive.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("archive.json root must be an object")
    return write_ai_jsonl(archive_from_dict(payload), root)
