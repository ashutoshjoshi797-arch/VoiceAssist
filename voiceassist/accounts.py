"""Local user accounts — work fully offline, nothing is sent anywhere.

* Passwords: salted scrypt (n=2^14, r=8, p=1), compared in constant time. Never stored or logged in clear.
* Sessions: random 256-bit tokens; only their SHA-256 hash is stored. Kept in an HttpOnly,
  SameSite=Strict cookie by the server. "Remember me" = 30 days, otherwise 12 hours.
* Brute force: after 5 wrong passwords an account is locked for 60 s, doubling up to 15 min.
* Forgotten password (no email offline): every account gets a one-time recovery code at sign-up,
  shown once. It can reset the password, and is replaced by a new one when used.

Stored in accounts.sqlite3 in the data folder (always persistent, independent of the
"save history" privacy setting).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
SESSION_REMEMBER_S = 30 * 24 * 3600
SESSION_SHORT_S = 12 * 3600
MAX_FAILS = 5
LOCK_BASE_S, LOCK_MAX_S = 60, 15 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  email TEXT NOT NULL DEFAULT '',
  pw_hash TEXT NOT NULL,
  recovery_hash TEXT NOT NULL,
  created REAL NOT NULL,
  last_login REAL,
  failed INTEGER NOT NULL DEFAULT 0,
  locked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created REAL NOT NULL,
  expires REAL NOT NULL,
  remember INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id);
"""


class AccountError(Exception):
    """User-facing account error. `field` names the form field it relates to (or '')."""

    def __init__(self, message: str, field: str = "", status: int = 400):
        super().__init__(message)
        self.field = field
        self.status = status


# --------------------------------------------------------------------------- hashing
def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        test = hashlib.scrypt(secret.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(dk)))
        return hmac.compare_digest(test, bytes.fromhex(dk))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_secret("timing-equaliser")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


def new_recovery_code() -> str:
    # 20 chars from an unambiguous alphabet, grouped: ABCD-EFGH-JKLM-NPQR-STUV
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(20))
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def _norm_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


# --------------------------------------------------------------------------- validation
def validate_username(username: str) -> str:
    u = (username or "").strip()
    if not USERNAME_RE.match(u):
        raise AccountError("Username must be 3–32 characters: letters, numbers, dot, dash or underscore, "
                           "starting with a letter or number.", "username")
    return u


def validate_password(password: str, username: str = "") -> str:
    pw = password or ""
    if len(pw) < 8:
        raise AccountError("Password must be at least 8 characters.", "password")
    if len(pw) > 256:
        raise AccountError("Password is too long (max 256 characters).", "password")
    if username and pw.lower() == username.lower():
        raise AccountError("Password must not be the same as your username.", "password")
    if not (re.search(r"[A-Za-z]", pw) and re.search(r"[^A-Za-z]", pw)):
        raise AccountError("Use at least one letter and one number or symbol.", "password")
    return pw


def _public(row) -> dict:
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "email": row["email"], "created": row["created"], "last_login": row["last_login"]}


# --------------------------------------------------------------------------- store
class AccountStore:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys=ON")
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            self._db.commit()

    # -- queries ---------------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def get(self, user_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _public(row) if row else None

    # -- sign up / log in --------------------------------------------------------
    def signup(self, username: str, password: str, display_name: str = "", email: str = "") -> tuple[dict, str]:
        """Create an account. Returns (user, recovery_code) — the code is shown to the user once."""
        username = validate_username(username)
        validate_password(password, username)
        display_name = (display_name or "").strip()[:60] or username
        email = (email or "").strip()[:254]
        if email and not EMAIL_RE.match(email):
            raise AccountError("That email address doesn't look right.", "email")
        code = new_recovery_code()
        pw_hash, rc_hash = hash_secret(password), hash_secret(_norm_code(code))
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO users(username, display_name, email, pw_hash, recovery_hash, created) VALUES (?,?,?,?,?,?)",
                    (username, display_name, email, pw_hash, rc_hash, time.time()))
                self._db.commit()
            except sqlite3.IntegrityError:
                raise AccountError("That username is already taken.", "username", 409) from None
            row = self._db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return _public(row), code

    def login(self, username: str, password: str) -> dict:
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
        if row is None:
            verify_secret(password or "", _DUMMY_HASH)  # same work as a real check: no username probing by timing
            raise AccountError("Incorrect username or password.", "password", 401)
        if row["locked_until"] > now:
            wait = int(row["locked_until"] - now) + 1
            raise AccountError(f"Too many wrong attempts. Try again in {wait} seconds.", "password", 429)
        if not verify_secret(password or "", row["pw_hash"]):
            fails = row["failed"] + 1
            locked = 0.0
            if fails >= MAX_FAILS:
                locked = now + min(LOCK_MAX_S, LOCK_BASE_S * 2 ** (fails - MAX_FAILS))
            with self._lock:
                self._db.execute("UPDATE users SET failed=?, locked_until=? WHERE id=?", (fails, locked, row["id"]))
                self._db.commit()
            if locked:
                raise AccountError(f"Too many wrong attempts. Try again in {int(locked - now)} seconds.", "password", 429)
            raise AccountError("Incorrect username or password.", "password", 401)
        with self._lock:
            self._db.execute("UPDATE users SET failed=0, locked_until=0, last_login=? WHERE id=?", (now, row["id"]))
            self._db.commit()
        return _public(row)

    # -- sessions ----------------------------------------------------------------
    def create_session(self, user_id: int, remember: bool) -> tuple[str, int | None]:
        """Returns (token, max_age_seconds or None for a browser-session cookie)."""
        token = secrets.token_urlsafe(32)
        ttl = SESSION_REMEMBER_S if remember else SESSION_SHORT_S
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (_token_hash(token), user_id, now, now + ttl, int(remember)))
            self._db.commit()
        return token, (ttl if remember else None)

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT u.*, s.expires, s.remember, s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                (_token_hash(token),)).fetchone()
            if row is None:
                return None
            if row["expires"] < now:
                self._db.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
                self._db.commit()
                return None
            # sliding expiry for "remember me" sessions
            if row["remember"] and row["expires"] - now < SESSION_REMEMBER_S / 2:
                self._db.execute("UPDATE sessions SET expires=? WHERE token_hash=?", (now + SESSION_REMEMBER_S, row["token_hash"]))
                self._db.commit()
        return _public(row)

    def end_session(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
                self._db.commit()

    def end_all_sessions(self, user_id: int, except_token: str | None = None) -> None:
        with self._lock:
            if except_token:
                self._db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, _token_hash(except_token)))
            else:
                self._db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._db.commit()

    # -- account management --------------------------------------------------------
    def _check_password(self, user_id: int, password: str) -> sqlite3.Row:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise AccountError("Account not found.", "", 404)
        if not verify_secret(password or "", row["pw_hash"]):
            raise AccountError("Current password is incorrect.", "current_password", 401)
        return row

    def update_profile(self, user_id: int, display_name: str | None = None, email: str | None = None) -> dict:
        sets, vals = [], []
        if display_name is not None:
            dn = display_name.strip()[:60]
            if not dn:
                raise AccountError("Name can't be empty.", "display_name")
            sets.append("display_name=?"); vals.append(dn)
        if email is not None:
            em = email.strip()[:254]
            if em and not EMAIL_RE.match(em):
                raise AccountError("That email address doesn't look right.", "email")
            sets.append("email=?"); vals.append(em)
        if sets:
            with self._lock:
                self._db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*vals, user_id))
                self._db.commit()
        return self.get(user_id)

    def change_password(self, user_id: int, current: str, new: str, keep_token: str | None = None) -> None:
        row = self._check_password(user_id, current)
        validate_password(new, row["username"])
        with self._lock:
            self._db.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_secret(new), user_id))
            self._db.commit()
        self.end_all_sessions(user_id, except_token=keep_token)  # sign out other windows

    def regenerate_recovery(self, user_id: int, password: str) -> str:
        self._check_password(user_id, password)
        code = new_recovery_code()
        with self._lock:
            self._db.execute("UPDATE users SET recovery_hash=? WHERE id=?", (hash_secret(_norm_code(code)), user_id))
            self._db.commit()
        return code

    def reset_with_recovery(self, username: str, code: str, new_password: str) -> tuple[dict, str]:
        """Forgot-password flow. Returns (user, NEW recovery code) — the old code stops working."""
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
        if row is None:
            verify_secret(code or "", _DUMMY_HASH)
            raise AccountError("Username or recovery code is incorrect.", "code", 401)
        if row["locked_until"] > now:
            raise AccountError(f"Too many wrong attempts. Try again in {int(row['locked_until'] - now) + 1} seconds.", "code", 429)
        if not verify_secret(_norm_code(code), row["recovery_hash"]):
            fails = row["failed"] + 1
            locked = now + min(LOCK_MAX_S, LOCK_BASE_S * 2 ** (fails - MAX_FAILS)) if fails >= MAX_FAILS else 0.0
            with self._lock:
                self._db.execute("UPDATE users SET failed=?, locked_until=? WHERE id=?", (fails, locked, row["id"]))
                self._db.commit()
            raise AccountError("Username or recovery code is incorrect.", "code", 401)
        validate_password(new_password, row["username"])
        new_code = new_recovery_code()
        with self._lock:
            self._db.execute("UPDATE users SET pw_hash=?, recovery_hash=?, failed=0, locked_until=0 WHERE id=?",
                             (hash_secret(new_password), hash_secret(_norm_code(new_code)), row["id"]))
            self._db.commit()
        self.end_all_sessions(row["id"])
        return _public(row), new_code

    def delete(self, user_id: int, password: str) -> None:
        self._check_password(user_id, password)
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._db.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._db.commit()
