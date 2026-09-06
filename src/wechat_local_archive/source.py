from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from wechatauto import MediaDownloader, WeChatDB, auto_detect_db_dir, list_accounts

from .paths import temporary_workdir
from .state import AppConfig, SecretState, load_config, load_secret, save_config, save_secret


class SourceError(RuntimeError):
    pass


class _OfflineWeChatDB(WeChatDB):
    """WeChatDB variant that is forbidden from reading Weixin.exe process memory."""

    def __init__(self, *args, cached_cfg_dword: int | None = None, **kwargs) -> None:
        self._cached_cfg_dword = cached_cfg_dword
        super().__init__(*args, **kwargs)

    def extract_keys(self) -> dict:
        return {}

    def extract_master_key(self):
        if self._cached_cfg_dword is None:
            return None
        # The constructor only needs this tuple to populate cfg_dword when cached DB keys are already valid.
        return ("0" * 64, self._cached_cfg_dword, "offline-cache")


@dataclass(slots=True)
class SourceSession:
    db: WeChatDB
    cfg_dword: int | None
    workdir: Path

    def list_chats(self) -> list[dict]:
        return self.db.list_message_chats()

    def export_chat_payload(self, username: str) -> dict:
        target = self.workdir / "history.json"
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        result = self.db.export_history(str(target), fmt="json", users=[username])
        if not result.get("messages"):
            raise SourceError(f"No messages were found for {username}")
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(f"Unable to read temporary history export: {exc}") from exc

    def media_downloader(self, save_dir: Path) -> MediaDownloader:
        return MediaDownloader(
            self.db,
            save_dir=str(save_dir),
            cfg_dword=self.cfg_dword,
        )


def discover_accounts(db_dir: str | None = None) -> list[dict]:
    root = db_dir or auto_detect_db_dir()
    if not root:
        return []
    return list_accounts(root)


def bootstrap(db_dir: str | None = None, account: str | None = None) -> AppConfig:
    root = db_dir or auto_detect_db_dir()
    if not root:
        raise SourceError("Unable to find the local WeChat xwechat_files data root")
    accounts = list_accounts(root)
    if not accounts:
        raise SourceError("No local WeChat 4.x account directories were found")
    selected = account or str(accounts[0]["account"])
    known = {str(item["account"]) for item in accounts}
    if selected not in known:
        raise SourceError(f"Unknown account directory: {selected}")

    workdir = temporary_workdir(selected)
    _reset_workdir(workdir)
    keys_path = workdir / "keys.json"
    try:
        db = WeChatDB(
            db_dir=root,
            account=selected,
            workdir=str(workdir),
            keys_file=str(keys_path),
        )
        try:
            db_keys = json.loads(keys_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise SourceError("WeChat database keys were not captured") from exc
        if not isinstance(db_keys, dict) or _essential_keys_missing(db_keys):
            raise SourceError(
                "Essential WeChat database keys could not be captured. Keep the already logged-in WeChat client running and retry."
            )
        cfg_dword = getattr(db, "cfg_dword", None)
        if cfg_dword is None:
            try:
                extracted = db.extract_master_key()
            except Exception:
                extracted = None
            if extracted:
                _master_key, cfg_dword, _account_info = extracted
        config = AppConfig(db_dir=str(root), account=selected)
        save_config(config)
        save_secret(
            SecretState(
                db_keys={str(name): str(value) for name, value in db_keys.items()},
                cfg_dword=cfg_dword,
            )
        )
        return config
    finally:
        _reset_workdir(workdir)


@contextmanager
def open_offline() -> Iterator[SourceSession]:
    config = load_config()
    secret = load_secret()
    if config is None or secret is None:
        raise SourceError("Not initialized. Run `wechat-archive bootstrap` once while WeChat is already logged in.")
    workdir = temporary_workdir(config.account)
    _reset_workdir(workdir)
    keys_path = workdir / "keys.json"
    keys_path.write_text(json.dumps(secret.db_keys, ensure_ascii=False), encoding="utf-8")
    try:
        db = _OfflineWeChatDB(
            db_dir=config.db_dir,
            account=config.account,
            workdir=str(workdir),
            keys_file=str(keys_path),
            cached_cfg_dword=secret.cfg_dword,
        )
        if _essential_keys_missing(secret.db_keys) or _essential_unkeyed(getattr(db, "unkeyed", [])):
            raise SourceError(
                "The cached database keys no longer match the local WeChat databases. Run `wechat-archive bootstrap` again."
            )
        yield SourceSession(db=db, cfg_dword=secret.cfg_dword, workdir=workdir)
    finally:
        _reset_workdir(workdir)


def resolve_chat(chats: list[dict], query: str) -> dict:
    needle = query.strip().casefold()
    if not needle:
        raise SourceError("Chat selector must not be empty")
    exact = [
        item
        for item in chats
        if str(item.get("username", "")).casefold() == needle
        or str(item.get("name", "")).casefold() == needle
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        item
        for item in chats
        if needle in str(item.get("username", "")).casefold()
        or needle in str(item.get("name", "")).casefold()
    ]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SourceError(f"No chat matched: {query}")
    names = ", ".join(str(item.get("name") or item.get("username")) for item in partial[:8])
    raise SourceError(f"Chat selector is ambiguous: {query}. Matches: {names}")


def _essential_keys_missing(keys: dict[str, str]) -> bool:
    names = {str(name).replace("/", "\\").casefold() for name in keys}
    has_contact = "contact\\contact.db" in names
    has_session = "session\\session.db" in names
    has_message = any(name.startswith("message\\message_") and name.endswith(".db") for name in names)
    return not (has_contact and has_session and has_message)


def _essential_unkeyed(unkeyed: list[str]) -> bool:
    names = {str(name).replace("/", "\\").casefold() for name in unkeyed}
    return (
        "contact\\contact.db" in names
        or "session\\session.db" in names
        or any(name.startswith("message\\message_") and name.endswith(".db") for name in names)
    )


def _reset_workdir(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        # A previous Python process may still be releasing a SQLite handle. Leaving a temp cache is safer than deleting user data.
        return
    path.mkdir(parents=True, exist_ok=True)
