"""VoiceAssist cloud: one Supabase backend for the whole product.

* Accounts are Supabase Auth users (email + password). One login works on the laptop, the phone and the website.
* Online answers come from the product's "chat" Edge Function, which holds the product's single AI key (Gemini,
  free tier) and enforces the per-user daily limit. Users never need an AI key.
* The app keeps working offline: once signed in, the session is cached on this computer, so the account opens
  without internet and the on-device AI answers.

The project URL and its public (anon / publishable) key live in voiceassist/cloud.json — both are safe to ship.
Override with VOICEASSIST_SUPABASE_URL / VOICEASSIST_SUPABASE_KEY (the tests point them at a fake server).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Iterator

from . import netutil, paths
from .accounts import AccountError
from .llm.base import AuthError, ChatProvider, LLMError

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_REMEMBER_S = 30 * 24 * 3600
SESSION_SHORT_S = 12 * 3600
CLOUD_LABEL = "Cloud AI"


def load_config() -> dict:
    cfg = {}
    try:
        cfg = json.loads((Path(__file__).with_name("cloud.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    url = os.environ.get("VOICEASSIST_SUPABASE_URL") or cfg.get("supabase_url") or ""
    key = os.environ.get("VOICEASSIST_SUPABASE_KEY") or cfg.get("supabase_anon_key") or ""
    return {"url": url.rstrip("/"), "key": key, "site_url": cfg.get("site_url", "")}


def _err_message(e: netutil.HTTPStatusError) -> tuple[str, str]:
    try:
        j = json.loads(e.body)
    except ValueError:
        return e.body[:200], ""
    if not isinstance(j, dict):
        return str(j)[:200], ""
    err = j.get("error")
    if isinstance(err, dict):   # {"error": {"message": ...}}
        err = err.get("message") or err.get("type")
    msg = j.get("msg") or j.get("error_description") or j.get("message") or err or ""
    return str(msg).strip(), str(j.get("error_code") or j.get("code") or "")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class CloudAccounts:
    """Same interface as the old local AccountStore, backed by Supabase Auth.

    Local sessions (the window's cookie) map to a cached Supabase session per user, kept in cloud_session.json
    in the data folder (readable only by this Windows user)."""

    def __init__(self, path: str | None = None, config: dict | None = None):
        self.cfg = config or load_config()
        self.path = Path(path or (paths.data_dir() / "cloud_session.json"))
        self.lock = threading.RLock()
        self.db = {"users": {}, "sessions": {}}
        try:
            self.db.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    # ----------------------------------------------------------------- plumbing
    @property
    def configured(self) -> bool:
        return bool(self.cfg["url"] and self.cfg["key"])

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.db), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def _call(self, method: str, path: str, body=None, token: str | None = None, timeout: float = 20) -> dict:
        if not self.configured:
            raise AccountError("This copy of VoiceAssist isn't connected to the VoiceAssist cloud yet.", "", 503)
        headers = {"apikey": self.cfg["key"], "Authorization": f"Bearer {token or self.cfg['key']}"}
        try:
            s = netutil.open_request(method, self.cfg["url"] + path, headers=headers, json_body=body, timeout=timeout)
            raw = s.read_all()
            return json.loads(raw) if raw.strip() else {}
        except netutil.HTTPStatusError as e:
            msg, code = _err_message(e)
            raise self._map(e.status, msg, code) from e
        except (netutil.NetworkError, netutil.OfflineViolation) as e:
            raise AccountError("You're offline. Connect to the internet to sign in or change your account — "
                               "once signed in, VoiceAssist also works offline.", "", 503) from e

    @staticmethod
    def _map(status: int, msg: str, code: str) -> AccountError:
        m = msg.lower()
        if code == "invalid_credentials" or "invalid login" in m:
            return AccountError("Incorrect email or password.", "password", 401)
        if code == "email_not_confirmed" or "not confirmed" in m:
            return AccountError("Please confirm your email first — open the link we sent you, then log in.", "email", 401)
        if code in ("user_already_exists", "email_exists") or "already registered" in m:
            return AccountError("An account with this email already exists. Log in instead.", "email", 409)
        if code == "weak_password" or "password" in m and ("least" in m or "weak" in m or "should" in m):
            return AccountError(msg or "Choose a stronger password.", "password", 400)
        if code in ("over_request_rate_limit", "over_email_send_rate_limit") or status == 429:
            return AccountError("Too many attempts — please wait a minute and try again.", "", 429)
        if "email" in m and ("invalid" in m or "valid" in m):
            return AccountError("That email address doesn't look right.", "email", 400)
        return AccountError(msg or f"Account service error ({status}).", "", status if 400 <= status < 600 else 400)

    @staticmethod
    def _public(u: dict) -> dict:
        meta = u.get("user_metadata") or {}
        email = u.get("email", "")
        return {"id": u["id"], "username": email, "email": email,
                "display_name": meta.get("display_name") or email.split("@")[0],
                "created": u.get("created_at"), "last_login": u.get("last_sign_in_at")}

    def _store(self, sess: dict) -> dict:
        user = self._public(sess["user"])
        with self.lock:
            self.db["users"][user["id"]] = {
                "access": sess["access_token"], "refresh": sess["refresh_token"],
                "expires_at": time.time() + int(sess.get("expires_in", 3600)) - 60, "user": user}
            self._save()
        return user

    @staticmethod
    def _validate(email: str, password: str) -> str:
        email = (email or "").strip()
        if not EMAIL_RE.match(email):
            raise AccountError("Enter a valid email address.", "email")
        if len(password or "") < 8:
            raise AccountError("Password must be at least 8 characters.", "password")
        if not (re.search(r"[A-Za-z]", password) and re.search(r"[^A-Za-z]", password)):
            raise AccountError("Use at least one letter and one number or symbol.", "password")
        return email

    # ----------------------------------------------------------------- AccountStore interface
    def count(self) -> int:
        return len(self.db["users"])

    def signup(self, email: str, password: str, display_name: str = "") -> tuple[dict | None, bool]:
        """Returns (user, False) when signed in straight away, or (None, True) when the email must be confirmed."""
        email = self._validate(email, password)
        r = self._call("POST", "/auth/v1/signup", {"email": email, "password": password,
                                                  "data": {"display_name": (display_name or "").strip()[:60]}})
        if r.get("access_token"):
            return self._store(r), False
        if r.get("identities") == []:   # Supabase answers this way for an email that is already registered
            raise AccountError("An account with this email already exists. Log in instead.", "email", 409)
        return None, True

    def login(self, email: str, password: str) -> dict:
        email = (email or "").strip()
        if not email:
            raise AccountError("Enter your email.", "email")
        r = self._call("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password})
        return self._store(r)

    def create_session(self, user_id: str, remember: bool) -> tuple[str, int | None]:
        token = secrets.token_urlsafe(32)
        ttl = SESSION_REMEMBER_S if remember else SESSION_SHORT_S
        with self.lock:
            self.db["sessions"][_hash(token)] = {"user_id": user_id, "expires": time.time() + ttl, "remember": remember}
            self._save()
        return token, (ttl if remember else None)

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.lock:
            s = self.db["sessions"].get(_hash(token))
            if not s:
                return None
            if s["expires"] < time.time():
                self.db["sessions"].pop(_hash(token), None)
                self._save()
                return None
            u = self.db["users"].get(s["user_id"])
            return dict(u["user"]) if u else None

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self.lock:
            s = self.db["sessions"].pop(_hash(token), None)
            if s and not any(x["user_id"] == s["user_id"] for x in self.db["sessions"].values()):
                u = self.db["users"].pop(s["user_id"], None)   # last window signed out: forget the cloud session
                if u:
                    threading.Thread(target=self._remote_logout, args=(u["access"],), daemon=True).start()
            self._save()

    def _remote_logout(self, access: str) -> None:
        try:
            self._call("POST", "/auth/v1/logout", {}, token=access, timeout=8)
        except AccountError:
            pass

    def access_token(self, user_id: str) -> str:
        """A valid access token for this user (refreshed when it has expired). Needs internet."""
        with self.lock:
            u = self.db["users"].get(user_id)
            if not u:
                raise AuthError("Please sign in again to use the cloud AI.")
            if u["expires_at"] > time.time():
                return u["access"]
            refresh = u["refresh"]
        try:
            r = self._call("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": refresh})
        except AccountError as e:
            if e.status == 503:
                raise netutil.NetworkError(str(e)) from e
            raise AuthError("Your sign-in has expired — please log out and log in again.") from e
        self._store(r)
        return r["access_token"]

    def update_profile(self, user_id: str, display_name: str | None = None, email: str | None = None) -> dict:
        if display_name is None:
            return dict(self.db["users"][user_id]["user"])
        name = display_name.strip()[:60]
        if not name:
            raise AccountError("Name can't be empty.", "display_name")
        u = self._call("PUT", "/auth/v1/user", {"data": {"display_name": name}}, token=self.access_token(user_id))
        with self.lock:
            self.db["users"][user_id]["user"] = self._public(u)
            self._save()
        return dict(self.db["users"][user_id]["user"])

    def _verify_password(self, user_id: str, password: str) -> None:
        email = self.db["users"][user_id]["user"]["email"]
        try:
            self._call("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password})
        except AccountError as e:
            if e.status == 401:
                raise AccountError("Current password is incorrect.", "current_password", 401) from e
            raise

    def change_password(self, user_id: str, current: str, new: str, keep_token: str | None = None) -> None:
        self._validate(self.db["users"][user_id]["user"]["email"], new)
        self._verify_password(user_id, current)
        self._call("PUT", "/auth/v1/user", {"password": new}, token=self.access_token(user_id))

    def request_password_reset(self, email: str) -> None:
        email = (email or "").strip()
        if not EMAIL_RE.match(email):
            raise AccountError("Enter the email address of your account.", "email")
        q = f"?redirect_to={self.cfg['site_url']}" if self.cfg.get("site_url") else ""
        self._call("POST", "/auth/v1/recover" + q, {"email": email})

    def delete(self, user_id: str, password: str) -> None:
        self._verify_password(user_id, password)
        self._call("POST", "/functions/v1/delete-account", {}, token=self.access_token(user_id))
        with self.lock:
            self.db["users"].pop(user_id, None)
            for k in [k for k, s in self.db["sessions"].items() if s["user_id"] == user_id]:
                self.db["sessions"].pop(k)
            self._save()

    # ----------------------------------------------------------------- cloud AI
    def transcribe(self, user_id: str, wav: bytes, timeout: float = 20) -> str:
        """Speech -> text with the cloud AI (better with names and accents than the small offline model)."""
        import base64
        r = self._call("POST", "/functions/v1/chat", {"transcribe": base64.b64encode(wav).decode("ascii")},
                       token=self.access_token(user_id), timeout=timeout)
        return str(r.get("text") or "").strip()

    def usage(self, user_id: str) -> dict:
        return self._call("GET", "/functions/v1/chat", token=self.access_token(user_id), timeout=10)


class VoiceAssistCloudProvider(ChatProvider):
    """The product's cloud AI via the Supabase "chat" function (no user key needed)."""
    kind, name, label = "cloud", "voiceassist", CLOUD_LABEL

    def __init__(self, accounts: CloudAccounts, user_id: str | None, max_tokens: int = 1024, timeout: float = 30):
        super().__init__("gemini")
        self.accounts, self.user_id, self.max_tokens, self.timeout = accounts, user_id, max_tokens, timeout
        self.questions_left: int | None = None

    def stream_chat(self, system, messages, cancel_event: threading.Event) -> Iterator[str]:
        from .llm.cloud import merge_roles
        if not self.user_id:
            raise AuthError("Please sign in to use the cloud AI.")
        token = self.accounts.access_token(self.user_id)
        cfg = self.accounts.cfg
        try:
            s = netutil.open_request(
                "POST", cfg["url"] + "/functions/v1/chat",
                headers={"apikey": cfg["key"], "Authorization": f"Bearer {token}"},
                json_body={"system": system, "messages": merge_roles(messages), "max_tokens": self.max_tokens},
                timeout=self.timeout, cancel_event=cancel_event)
        except netutil.HTTPStatusError as e:
            msg, code = _err_message(e)
            if e.status == 401:
                raise AuthError("Your sign-in has expired — please log out and log in again.") from e
            if e.status == 429 or code == "limit":
                raise LLMError(msg or "Daily question limit reached.", hint="Answers continue with the offline AI on this device.") from e
            raise LLMError(msg or f"VoiceAssist cloud error {e.status}") from e
        try:
            left = s.headers.get("x-questions-left")
            self.questions_left = int(left) if left is not None else None
        except ValueError:
            pass
        completed = False
        for event, data in s.iter_sse():
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if event == "error" or j.get("type") == "error":
                raise LLMError((j.get("error") or {}).get("message") or "The cloud AI returned an error.")
            if j.get("type") == "content_block_delta":
                d = j.get("delta", {})
                if d.get("type") == "text_delta" and d.get("text"):
                    yield d["text"]
            elif j.get("type") == "message_stop":
                completed = True
                break
        if not completed:
            raise netutil.NetworkError("the answer stopped before it was complete")
