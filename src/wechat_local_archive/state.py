from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import app_data_dir, config_path, secret_path, ui_config_path


@dataclass(frozen=True, slots=True)
class AppConfig:
    db_dir: str
    account: str


@dataclass(frozen=True, slots=True)
class SecretState:
    db_keys: dict[str, str]
    cfg_dword: int | None = None
    master_key: str | None = None


@dataclass(frozen=True, slots=True)
class UiConfig:
    output_root: str
    asr_preset: str = "balanced"


class StateError(RuntimeError):
    pass


def load_config() -> AppConfig | None:
    path = config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    db_dir = str(payload.get("db_dir") or "").strip()
    account = str(payload.get("account") or "").strip()
    if not db_dir or not account:
        return None
    return AppConfig(db_dir=db_dir, account=account)


def save_config(config: AppConfig) -> None:
    _atomic_write_text(config_path(), json.dumps(asdict(config), ensure_ascii=False, indent=2))


def load_ui_config() -> UiConfig | None:
    try:
        payload = json.loads(ui_config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    output_root = str(payload.get("output_root") or "").strip()
    asr_preset = str(payload.get("asr_preset") or "balanced").strip()
    if not output_root or asr_preset not in {"background", "balanced", "fast"}:
        return None
    return UiConfig(output_root=output_root, asr_preset=asr_preset)


def save_ui_config(config: UiConfig) -> None:
    if config.asr_preset not in {"background", "balanced", "fast"}:
        raise ValueError(f"Unknown ASR preset: {config.asr_preset}")
    _atomic_write_text(ui_config_path(), json.dumps(asdict(config), ensure_ascii=False, indent=2))


def load_secret() -> SecretState | None:
    path = secret_path()
    try:
        encrypted = path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    if not encrypted:
        return None
    if os.name != "nt":
        raise StateError("DPAPI secret storage is only supported on Windows")
    try:
        import win32crypt

        _description, clear = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        payload = json.loads(clear.decode("utf-8"))
    except Exception as exc:
        raise StateError(f"Unable to decrypt local secret state: {exc}") from exc
    raw_keys = payload.get("db_keys")
    db_keys = {
        str(name): str(value)
        for name, value in raw_keys.items()
        if isinstance(name, str) and isinstance(value, str) and value
    } if isinstance(raw_keys, dict) else {}
    master_key = str(payload.get("master_key") or "").strip() or None
    if master_key is not None and len(master_key) != 64:
        master_key = None
    if not db_keys and master_key is None:
        raise StateError("Stored database keys are invalid")
    cfg_value = payload.get("cfg_dword")
    cfg_dword = int(cfg_value) if cfg_value is not None else None
    return SecretState(db_keys=db_keys, cfg_dword=cfg_dword, master_key=master_key)


def save_secret(secret: SecretState) -> None:
    if os.name != "nt":
        raise StateError("DPAPI secret storage is only supported on Windows")
    try:
        import win32crypt

        clear = json.dumps(asdict(secret), separators=(",", ":")).encode("utf-8")
        encrypted = win32crypt.CryptProtectData(
            clear,
            "WeChat Local Archive",
            None,
            None,
            None,
            0,
        )
    except Exception as exc:
        raise StateError(f"Unable to protect local secret state: {exc}") from exc
    _atomic_write_bytes(secret_path(), encrypted)


def clear_state() -> None:
    for path in (config_path(), secret_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
