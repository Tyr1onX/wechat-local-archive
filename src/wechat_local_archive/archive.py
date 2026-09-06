from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable

SUPPORTED_SCHEMA_VERSIONS = {2, 3}
CURRENT_SCHEMA_VERSION = 3


@dataclass(slots=True)
class ArchiveMessage:
    id: str
    local_id: int
    server_id: int
    sort_seq: int
    timestamp: str
    timestamp_unix: int
    sender: str
    type: str
    type_code: int
    content: str
    media_md5: str | None = None
    attachment: str | None = None
    transcript: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Archive:
    schema_version: int
    exported_at: str
    account: str
    account_name: str
    conversation_id: str
    conversation_name: str
    messages: list[ArchiveMessage]
    range_start: str | None = None
    range_end: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "exported_at": self.exported_at,
            "account": self.account,
            "account_name": self.account_name,
            "conversation": {
                "id": self.conversation_id,
                "name": self.conversation_name,
            },
            "range": {
                "start": self.range_start,
                "end": self.range_end,
            },
            "messages": [message.to_dict() for message in self.messages],
        }


def normalize_payload(
    payload: dict,
    conversation_id: str,
    conversation_name: str,
    start: date | None = None,
    end: date | None = None,
) -> Archive:
    lower = datetime.combine(start, time.min).astimezone().timestamp() if start else None
    upper = datetime.combine(end, time.max).astimezone().timestamp() if end else None
    messages: list[ArchiveMessage] = []
    for raw in payload.get("messages", []):
        if str(raw.get("chat") or "") != conversation_id:
            continue
        timestamp_unix = _coerce_int(raw.get("create_time"))
        if lower is not None and timestamp_unix < lower:
            continue
        if upper is not None and timestamp_unix > upper:
            continue
        type_code = _coerce_int(raw.get("type_code"))
        content = raw.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        local_id = _coerce_int(raw.get("local_id"))
        server_id = _coerce_int(raw.get("server_id"))
        sort_seq = _coerce_int(raw.get("sort_seq"))
        message_id = str(server_id) if server_id else f"{conversation_id}:{sort_seq}:{local_id}"
        messages.append(
            ArchiveMessage(
                id=message_id,
                local_id=local_id,
                server_id=server_id,
                sort_seq=sort_seq,
                timestamp=datetime.fromtimestamp(timestamp_unix).astimezone().isoformat(timespec="seconds"),
                timestamp_unix=timestamp_unix,
                sender=str(raw.get("sender_name") or ""),
                type=str(raw.get("type") or _base_type(type_code)),
                type_code=type_code,
                content=content,
                media_md5=str(raw.get("md5") or "") or None,
            )
        )
    messages.sort(key=lambda item: (item.timestamp_unix, item.sort_seq, item.local_id))
    return Archive(
        schema_version=CURRENT_SCHEMA_VERSION,
        exported_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        account=str(payload.get("wxid") or ""),
        account_name=str(payload.get("nick_name") or ""),
        conversation_id=conversation_id,
        conversation_name=conversation_name,
        messages=messages,
        range_start=start.isoformat() if start else None,
        range_end=end.isoformat() if end else None,
    )


def archive_from_dict(payload: dict) -> Archive:
    schema_version = _coerce_int(payload.get("schema_version"))
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported archive schema: {schema_version}; supported versions are {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        raise ValueError("Archive conversation metadata is missing")
    range_payload = payload.get("range") if schema_version >= 3 else None
    if not isinstance(range_payload, dict):
        range_payload = {}
    messages: list[ArchiveMessage] = []
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("Archive messages must be a list")
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise ValueError("Archive contains an invalid message entry")
        messages.append(
            ArchiveMessage(
                id=str(raw.get("id") or ""),
                local_id=_coerce_int(raw.get("local_id")),
                server_id=_coerce_int(raw.get("server_id")),
                sort_seq=_coerce_int(raw.get("sort_seq")),
                timestamp=str(raw.get("timestamp") or ""),
                timestamp_unix=_coerce_int(raw.get("timestamp_unix")),
                sender=str(raw.get("sender") or ""),
                type=str(raw.get("type") or ""),
                type_code=_coerce_int(raw.get("type_code")),
                content=str(raw.get("content") or ""),
                media_md5=str(raw.get("media_md5") or "") or None,
                attachment=str(raw.get("attachment") or "") or None,
                transcript=str(raw.get("transcript") or "") or None,
            )
        )
    messages.sort(key=lambda item: (item.timestamp_unix, item.sort_seq, item.local_id))
    return Archive(
        schema_version=schema_version,
        exported_at=str(payload.get("exported_at") or ""),
        account=str(payload.get("account") or ""),
        account_name=str(payload.get("account_name") or ""),
        conversation_id=str(conversation.get("id") or ""),
        conversation_name=str(conversation.get("name") or ""),
        messages=messages,
        range_start=str(range_payload.get("start") or "") or None,
        range_end=str(range_payload.get("end") or "") or None,
    )


def merge_archives(existing: Archive, fresh: Archive) -> Archive:
    if existing.account != fresh.account:
        raise ValueError("Existing archive belongs to a different WeChat account")
    if existing.conversation_id != fresh.conversation_id:
        raise ValueError("Existing archive belongs to a different conversation")
    if (existing.range_start, existing.range_end) != (fresh.range_start, fresh.range_end):
        raise ValueError("Existing archive uses a different date range; use --refresh to rebuild it")

    old_by_id = {message.id: message for message in existing.messages}
    merged: dict[str, ArchiveMessage] = {message.id: message for message in existing.messages}
    for message in fresh.messages:
        old = old_by_id.get(message.id)
        if old is not None:
            message.attachment = old.attachment
            message.transcript = old.transcript
        merged[message.id] = message
    messages = sorted(merged.values(), key=lambda item: (item.timestamp_unix, item.sort_seq, item.local_id))
    return Archive(
        schema_version=CURRENT_SCHEMA_VERSION,
        exported_at=fresh.exported_at,
        account=fresh.account,
        account_name=fresh.account_name or existing.account_name,
        conversation_id=fresh.conversation_id,
        conversation_name=fresh.conversation_name,
        messages=messages,
        range_start=fresh.range_start,
        range_end=fresh.range_end,
    )


def selected_media_type(type_code: int) -> int:
    return _base_type(type_code)


def relative_attachment(path: str | Path, root: Path) -> str:
    source = Path(path).resolve()
    try:
        return source.relative_to(root.resolve()).as_posix()
    except ValueError:
        return source.as_posix()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid date: {value}. Expected YYYY-MM-DD") from exc


def _base_type(value: int) -> int:
    if value > 0xFFFF:
        return value & 0xFF
    return value & 0xFFFF


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
