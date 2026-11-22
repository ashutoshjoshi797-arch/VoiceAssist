"""API key handling. Keys are never written to settings.json or logs.

Lookup order for each provider:
  1. Environment variable (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY or GOOGLE_API_KEY)
  2. The OS credential store via `keyring` (Windows Credential Manager, macOS Keychain,
     Secret Service on Linux) — this is where keys entered in the Settings panel are saved.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SERVICE = "VoiceAssist"
ENV_VARS = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
}

try:  # optional dependency
    import keyring  # type: ignore
    from keyring.errors import KeyringError  # type: ignore
except Exception:  # pragma: no cover
    keyring = None
    KeyringError = Exception


def _keyring_ok() -> bool:
    if keyring is None:
        return False
    try:
        kr = keyring.get_keyring()
        # The "fail" backend means no usable store (e.g. headless Linux without Secret Service)
        return "fail" not in type(kr).__module__.lower()
    except Exception:
        return False


def get_key(provider: str) -> str | None:
    for var in ENV_VARS.get(provider, []):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    if _keyring_ok():
        try:
            val = keyring.get_password(SERVICE, provider)
            return val.strip() if val else None
        except KeyringError as e:
            log.warning("credential store read failed for %s: %s", provider, e)
    return None


def key_source(provider: str) -> str | None:
    for var in ENV_VARS.get(provider, []):
        if os.environ.get(var, "").strip():
            return f"env:{var}"
    if _keyring_ok():
        try:
            if keyring.get_password(SERVICE, provider):
                return "credential-store"
        except KeyringError:
            pass
    return None


def set_key(provider: str, key: str) -> None:
    if provider not in ENV_VARS:
        raise ValueError(f"unknown provider {provider}")
    if not _keyring_ok():
        raise RuntimeError(
            "No OS credential store is available. Set the "
            f"{ENV_VARS[provider][0]} environment variable instead."
        )
    key = (key or "").strip()
    if not key:
        delete_key(provider)
        return
    keyring.set_password(SERVICE, provider, key)


def delete_key(provider: str) -> None:
    if _keyring_ok():
        try:
            keyring.delete_password(SERVICE, provider)
        except Exception:
            pass


def status() -> dict:
    return {p: {"configured": bool(get_key(p)), "source": key_source(p)} for p in ENV_VARS}


def credential_store_available() -> bool:
    return _keyring_ok()
