from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable


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
        schema_version=2,
        exported_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        account=str(payload.get("wxid") or ""),
        account_name=str(payload.get("nick_name") or ""),
        conversation_id=conversation_id,
        conversation_name=conversation_name,
        messages=messages,
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
