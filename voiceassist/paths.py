"""Filesystem locations. Everything the app writes lives under one per-user data folder.

Windows : %LOCALAPPDATA%\\VoiceAssist
macOS   : ~/Library/Application Support/VoiceAssist
Linux   : $XDG_DATA_HOME/VoiceAssist (default ~/.local/share/VoiceAssist)

Override with the VOICEASSIST_HOME environment variable (used by the tests).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
WEB_DIR = PROJECT_DIR / "web"


def data_dir() -> Path:
    override = os.environ.get("VOICEASSIST_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def models_dir() -> Path:
    override = os.environ.get("VOICEASSIST_MODELS")
    p = Path(override) if override else data_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def stt_dir() -> Path:
    return models_dir() / "stt"


def tts_dir() -> Path:
    return models_dir() / "tts"


def vad_model_path() -> Path:
    return models_dir() / "vad" / "silero_vad.onnx"


def recordings_dir() -> Path:
    p = data_dir() / "recordings"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path() -> Path:
    return data_dir() / "settings.json"


def accounts_db_path() -> Path:
    return data_dir() / "accounts.sqlite3"


def history_db_path() -> Path:
    return data_dir() / "history.sqlite3"


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def browser_profile_dir() -> Path:
    p = data_dir() / "window-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p
