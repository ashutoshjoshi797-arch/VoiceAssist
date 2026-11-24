"""Settings: defaults + user overrides persisted to settings.json.

API keys are NEVER stored here — see secrets_store.py.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from typing import Any

from . import paths

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful voice assistant running on the user's desktop. Your replies are read aloud, "
    "so answer conversationally in short, clear sentences. Do not use markdown, tables, bullet symbols, "
    "emoji or code blocks unless the user explicitly asks for them. Keep most answers under 80 words "
    "unless the user asks for detail."
)

DEFAULTS: dict[str, Any] = {
    # auto  = use the online provider when the internet is reachable, otherwise the local model
    # local = never use cloud services (everything stays on this computer)
    "mode": "auto",
    # When the internet comes back after a fallback, switch back automatically instead of asking.
    "auto_switch_back": False,
    # Strict offline: never open any non-loopback network connection (not even connectivity probes).
    "strict_offline": False,
    "online": {
        "provider": "anthropic",  # anthropic | openai | gemini
        "models": {
            "anthropic": "claude-sonnet-5-5",
            "openai": "gpt-5.5",
            "gemini": "gemini-3.8-flash",
        },
        "model_choices": {
            "anthropic": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
            "openai": ["gpt-5.5", "gpt-5.6-sol", "chat-latest"],
            "gemini": ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash"],
        },
        "max_output_tokens": 1024,
        "request_timeout_s": 30,
        # Cloud speech is opt-in. Default: speech is always processed locally, even when online.
        "cloud_stt": False,
        "cloud_tts": False,
        "openai_stt_model": "gpt-transcribe",
        "openai_tts_model": "gpt-4o-mini-tts",
        "openai_tts_voice": "alloy",
    },
    "local": {
        "engine": "ollama",  # ollama | openai_compat (llama.cpp server, LM Studio, ...)
        "ollama_url": "http://127.0.0.1:11434",
        "openai_compat_url": "http://127.0.0.1:8080/v1",
        "model": "llama3.2:3b",
        "num_ctx": 4096,
        "temperature": 0.7,
        "keep_alive": "30m",
        "disable_thinking": True,
        # Load the local model into RAM at startup so the first offline answer is fast.
        "preload": True,
        # Local engines must be on this computer. Set true only if you run Ollama on your LAN.
        "allow_lan": False,
    },
    "stt": {
        "model": "whisper-base.en",
        "language": "en",
        "threads": 0,  # 0 = auto
        "provider": "cpu",  # cpu | cuda | directml | coreml
        "use_vad_trim": True,
    },
    "tts": {
        "engine": "piper",  # piper | system | none
        "voice": "en_US-amy-low",
        "speaker_id": 0,
        "speed": 1.0,
        "threads": 0,
        "provider": "cpu",
        "system_voice": "",
    },
    "assistant": {
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "max_context_chars_online": 48000,
        "max_context_chars_local": 9000,
    },
    "privacy": {
        "save_history": True,
        "save_recordings": False,
    },
    # UI-only preferences (persisted here so they survive browser-profile resets).
    "ui": {
        "conversation_style": "handsfree",  # handsfree | ptt
        "review_voice": True,               # show what was heard in the text box; the user checks it and sends
        "mic_device": "",
        "speaker_device": "",
        "vad_sensitivity": 0.5,
        "silence_ms": 800,
        "barge_in": True,
        "theme": "dark",
        "voice_panel_open": False,  # voice controls collapsed behind the mic button next to Send
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _cpu_threads(requested: int, cap: int) -> int:
    if requested and requested > 0:
        return int(requested)
    return max(1, min(cap, (os.cpu_count() or 2) - 1))


class Settings:
    """Thread-safe settings container."""

    def __init__(self, path=None):
        self._path = path or paths.settings_path()
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self._listeners = []
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                if self._path.exists():
                    user = json.loads(self._path.read_text(encoding="utf-8"))
                    self._data = _deep_merge(DEFAULTS, user)
            except Exception as e:  # corrupt file -> keep defaults, keep a backup
                log.warning("settings.json unreadable (%s); using defaults", e)
                try:
                    self._path.rename(self._path.with_suffix(".bad.json"))
                except OSError:
                    pass

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted: str, default=None):
        with self._lock:
            cur: Any = self._data
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return copy.deepcopy(cur)

    def update(self, patch: dict) -> dict:
        """Deep-merge a partial dict, persist, notify listeners with the changed top-level keys."""
        if not isinstance(patch, dict):
            raise ValueError("settings patch must be an object")
        _validate(patch)
        with self._lock:
            before = copy.deepcopy(self._data)
            self._data = _deep_merge(self._data, patch)
            self.save()
            after = copy.deepcopy(self._data)
            listeners = list(self._listeners)
        changed = {k for k in after if before.get(k) != after.get(k)}
        for fn in listeners:
            try:
                fn(changed, after)
            except Exception:
                log.exception("settings listener failed")
        return after

    def on_change(self, fn) -> None:
        with self._lock:
            self._listeners.append(fn)

    # convenience
    def stt_threads(self) -> int:
        return _cpu_threads(self.get("stt.threads", 0), 4)

    def tts_threads(self) -> int:
        return _cpu_threads(self.get("tts.threads", 0), 2)


_ALLOWED_ENUMS = {
    "mode": {"auto", "local"},
    ("online", "provider"): {"anthropic", "openai", "gemini"},
    ("local", "engine"): {"ollama", "openai_compat"},
    ("tts", "engine"): {"piper", "system", "none"},
    ("ui", "conversation_style"): {"handsfree", "ptt"},
}


def _validate(patch: dict) -> None:
    for key, allowed in _ALLOWED_ENUMS.items():
        if isinstance(key, str):
            if key in patch and patch[key] not in allowed:
                raise ValueError(f"{key} must be one of {sorted(allowed)}")
        else:
            sect, name = key
            if isinstance(patch.get(sect), dict) and name in patch[sect] and patch[sect][name] not in allowed:
                raise ValueError(f"{sect}.{name} must be one of {sorted(allowed)}")
    for sect in ("online", "local", "stt", "tts", "assistant", "privacy", "ui"):
        if sect in patch and not isinstance(patch[sect], dict):
            raise ValueError(f"{sect} must be an object")
    if "speed" in patch.get("tts", {}):
        sp = float(patch["tts"]["speed"])
        if not 0.5 <= sp <= 2.0:
            raise ValueError("tts.speed must be between 0.5 and 2.0")
