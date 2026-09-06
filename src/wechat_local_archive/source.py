from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
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


@dataclass(frozen=True, slots=True)
class VoiceRow:
    local_id: int
    server_id: int
    data: bytes


class WeChatSourceAdapter:
    """Compatibility boundary around wechatauto-replica.

    Any use of upstream private attributes belongs here so archive/export code stays
    stable when the dependency changes internally.
    """

    def __init__(self, db: WeChatDB, cfg_dword: int | None, installed_version: str | None = None) -> None:
        self._db = db
        self._cfg_dword = cfg_dword
        self._downloader: MediaDownloader | None = None
        self._validate_version(installed_version)
        if not hasattr(db, "_db_files") or not callable(getattr(db, "_open", None)):
            raise SourceError(
                "wechatauto-replica compatibility check failed: expected database shard APIs are unavailable"
            )

    @property
    def account_dir(self) -> Path:
        return Path(self._db.account_dir)

    @property
    def account_id(self) -> str:
        return str(getattr(self._db, "wxid", "") or "")

    def list_chats(self) -> list[dict]:
        return self._db.list_message_chats()

    def chat_message_count(self, conversation_id: str) -> int | None:
        for chat in self.list_chats():
            if str(chat.get("username") or "") == conversation_id:
                try:
                    return int(chat.get("message_count"))
                except (TypeError, ValueError):
                    return None
        return None

    def has_new_messages(self, conversation_id: str, since_seq: int) -> bool:
        return bool(self._db.get_new_messages(conversation_id, since_seq=since_seq, limit=1))

    def export_messages(self, username: str, workdir: Path) -> dict:
        target = workdir / "history.json"
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        result = self._db.export_history(str(target), fmt="json", users=[username])
        if not result.get("messages"):
            raise SourceError(f"No messages were found for {username}")
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(f"Unable to read temporary history export: {exc}") from exc

    def iter_voice_rows(self, conversation_id: str) -> Iterator[VoiceRow]:
        for rel, path, _size in self._db._db_files:
            if not _basename(path).startswith("media_"):
                continue
            conn = self._db._open(rel)
            try:
                cid = conn.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name=? LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if not cid:
                    continue
                rows = conn.execute(
                    "SELECT local_id, svr_id, voice_data FROM VoiceInfo "
                    "WHERE chat_name_id=? AND voice_data IS NOT NULL",
                    (cid[0],),
                )
                for row in rows:
                    data = row["voice_data"]
                    if data:
                        yield VoiceRow(
                            local_id=int(row["local_id"] or 0),
                            server_id=int(row["svr_id"] or 0),
                            data=bytes(data),
                        )
            finally:
                conn.close()

    def find_image_sources(
        self,
        conversation_id: str,
        media_md5s: set[str],
    ) -> dict[str, dict[str, Path]]:
        expected = {value.casefold() for value in media_md5s if value}
        found: dict[str, dict[str, Path]] = {key: {} for key in expected}
        if not expected:
            return found
        chat_hash = hashlib.md5(conversation_id.encode("utf-8")).hexdigest()
        attach_root = self.account_dir / "msg" / "attach" / chat_hash
        if not attach_root.is_dir():
            return found
        for root, _dirs, files in os.walk(attach_root):
            for name in files:
                lowered = name.casefold()
                digest = ""
                kind = ""
                if lowered.endswith("_h.dat"):
                    digest, kind = lowered[:-6], "high"
                elif lowered.endswith("_t.dat"):
                    digest, kind = lowered[:-6], "thumb"
                elif lowered.endswith(".dat"):
                    digest, kind = lowered[:-4], "normal"
                if digest in expected and kind:
                    found[digest][kind] = Path(root) / name
        return found

    def find_file_sources(self, names: set[str]) -> dict[str, list[Path]]:
        expected = {Path(name).name.casefold() for name in names if name}
        found: dict[str, list[Path]] = {key: [] for key in expected}
        if not expected:
            return found
        base = self.account_dir / "msg" / "file"
        if not base.is_dir():
            return found
        for root, _dirs, files in os.walk(base):
            for name in files:
                key = name.casefold()
                if key in expected:
                    found[key].append(Path(root) / name)
        return found

    def decrypt_image(self, source: Path) -> bytes:
        return self._media_downloader().decrypt_image(str(source))

    def resolve_video(self, conversation_id: str, local_id: int, destination: Path) -> Path | None:
        resolved = self._media_downloader().download_video(
            conversation_id,
            local_id,
            save_dir=str(destination),
        )
        if not resolved:
            return None
        path = Path(resolved)
        return path if path.is_file() else None

    def _media_downloader(self) -> MediaDownloader:
        if self._downloader is None:
            self._downloader = MediaDownloader(self._db, cfg_dword=self._cfg_dword)
        return self._downloader

    @staticmethod
    def _validate_version(installed_version: str | None) -> None:
        version = installed_version
        if version is None:
            try:
                version = importlib.metadata.version("wechatauto-replica")
            except importlib.metadata.PackageNotFoundError as exc:
                raise SourceError("wechatauto-replica is not installed") from exc
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", version)
        if not match:
            raise SourceError(f"Unsupported wechatauto-replica version format: {version}")
        parts = match.groups()
        parsed = tuple(int(part or 0) for part in parts)
        if parsed < (1, 2, 0, 3) or parsed >= (1, 3, 0, 0):
            raise SourceError(
                f"Unsupported wechatauto-replica version: {version}; supported range is >=1.2.0.3,<1.3"
            )


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
    adapter: WeChatSourceAdapter
    workdir: Path

    @property
    def account_id(self) -> str:
        return self.adapter.account_id

    def list_chats(self) -> list[dict]:
        return self.adapter.list_chats()

    def chat_message_count(self, conversation_id: str) -> int | None:
        return self.adapter.chat_message_count(conversation_id)

    def has_new_messages(self, conversation_id: str, since_seq: int) -> bool:
        return self.adapter.has_new_messages(conversation_id, since_seq)

    def export_chat_payload(self, username: str) -> dict:
        return self.adapter.export_messages(username, self.workdir)

    def iter_voice_rows(self, conversation_id: str) -> Iterator[VoiceRow]:
        return self.adapter.iter_voice_rows(conversation_id)

    def find_image_sources(self, conversation_id: str, media_md5s: set[str]) -> dict[str, dict[str, Path]]:
        return self.adapter.find_image_sources(conversation_id, media_md5s)

    def find_file_sources(self, names: set[str]) -> dict[str, list[Path]]:
        return self.adapter.find_file_sources(names)

    def decrypt_image(self, source: Path) -> bytes:
        return self.adapter.decrypt_image(source)

    def resolve_video(self, conversation_id: str, local_id: int, destination: Path) -> Path | None:
        return self.adapter.resolve_video(conversation_id, local_id, destination)


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
        yield SourceSession(
            adapter=WeChatSourceAdapter(db, cfg_dword=secret.cfg_dword),
            workdir=workdir,
        )
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


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _reset_workdir(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        # A previous Python process may still be releasing a SQLite handle. Leaving a temp cache is safer than deleting user data.
        return
    path.mkdir(parents=True, exist_ok=True)
