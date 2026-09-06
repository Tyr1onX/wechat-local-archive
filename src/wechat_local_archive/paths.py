from __future__ import annotations

import os
import tempfile
from pathlib import Path


APP_DIR_NAME = "WeChatLocalArchive"


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / APP_DIR_NAME


def config_path() -> Path:
    return app_data_dir() / "config.json"


def secret_path() -> Path:
    return app_data_dir() / "secrets.dpapi"


def ui_config_path() -> Path:
    return app_data_dir() / "ui.json"


def transcript_cache_dir() -> Path:
    return app_data_dir() / "transcripts"


def model_cache_dir() -> Path:
    return app_data_dir() / "models"


def temporary_workdir(account: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in account) or "default"
    return Path(tempfile.gettempdir()) / "wechat-local-archive" / safe
