from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable
import json
import re

from .exporter import ExportSummary, export_chat
from .source import bootstrap, discover_accounts, open_offline, resolve_chat
from .state import load_config, load_secret
from .verify import VerifyResult, verify_archive


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    ready: bool
    account: str = ""
    data_root: str = ""


class ArchiveService:
    """Small application service shared by CLI/GUI-facing workflows."""

    def status(self) -> ServiceStatus:
        config = load_config()
        secret = load_secret()
        if config is None or secret is None:
            return ServiceStatus(
                ready=False,
                account=config.account if config else "",
                data_root=config.db_dir if config else "",
            )
        account_dir = Path(config.db_dir) / config.account / "db_storage"
        return ServiceStatus(
            ready=account_dir.is_dir(),
            account=config.account,
            data_root=config.db_dir,
        )

    def accounts(self) -> list[dict]:
        config = load_config()
        return discover_accounts(config.db_dir if config else None)

    def initialize(self, account: str | None = None) -> ServiceStatus:
        config = load_config()
        selected = account or (config.account if config else None)
        if selected is None and len(self.accounts()) > 1:
            raise ValueError("Multiple local WeChat accounts were found; select an account first")
        bootstrap(db_dir=config.db_dir if config else None, account=selected)
        return self.status()

    def list_chats(self) -> list[dict]:
        with open_offline() as session:
            chats = session.list_chats()
            account_id = session.account_id
        return [{**chat, "account_id": account_id} for chat in chats]

    def destination(self, output_root: Path, chat: dict) -> Path:
        """Use a stable chat ID; reuse an existing legacy display-name archive."""
        root = output_root.expanduser().resolve()
        username = str(chat["username"])
        config = load_config()
        # The account directory may have a suffix that is not part of the
        # canonical wxid stored in archive.json. Prefer the source identity.
        account_id = str(chat.get("account_id") or (config.account if config else ""))

        def matches(directory: Path) -> bool:
            try:
                data = json.loads((directory / "archive.json").read_text(encoding="utf-8"))
                return (data["conversation"]["id"] == username
                        and bool(account_id) and data.get("account") == account_id)
            except (OSError, ValueError, KeyError, TypeError):
                return False

        if matches(root):
            return root
        legacy_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(chat.get("name") or "")).strip(" .")
        if legacy_name and legacy_name.upper() not in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            legacy = root / legacy_name
            if matches(legacy):
                return legacy
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", username).strip(" ._")
        if not safe_id or safe_id.upper() in {"CON", "PRN", "AUX", "NUL"}:
            raise ValueError("Invalid conversation ID")
        return root / safe_id

    def export(
        self,
        *,
        chat_selector: str,
        output_dir: Path,
        start: date | None = None,
        end: date | None = None,
        include_media: bool = True,
        media_types: frozenset[int] | None = None,
        transcribe: bool = True,
        asr_preset: str = "balanced",
        progress: ProgressCallback | None = None,
        cancel: Callable[[], None] | None = None,
    ) -> ExportSummary:
        if cancel is not None:
            cancel()
        with open_offline() as session:
            if cancel is not None:
                cancel()
            chat = resolve_chat(session.list_chats(), chat_selector)
            return export_chat(
                session,
                conversation_id=str(chat["username"]),
                conversation_name=str(chat.get("name") or chat["username"]),
                output_dir=output_dir,
                start=start,
                end=end,
                media="all" if include_media else ("voice" if transcribe else "none"),
                media_types=media_types,
                transcribe=transcribe,
                asr_preset=asr_preset,
                progress=progress,
                cancel=cancel,
            )

    def verify(self, archive_dir: Path) -> VerifyResult:
        return verify_archive(archive_dir)
