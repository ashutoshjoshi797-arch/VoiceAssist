"""Conversation history in a local SQLite file (never uploaded anywhere).

If privacy.save_history is off, an in-memory database is used: conversations still work, and
everything disappears when the app closes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, user_id INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL, meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id);
"""


FLAGS = ("pinned", "archived", "unread")
TITLE_MAX = 80


class HistoryStore:
    def __init__(self, path: str | None):
        self.persistent = path is not None
        self._db = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys=ON")
            if self.persistent:
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            cols = [r[1] for r in self._db.execute("PRAGMA table_info(conversations)")]
            if "user_id" not in cols:  # databases created before accounts existed
                self._db.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER")
            for flag in FLAGS:  # pinned / archived / unread (added in 1.2.1)
                if flag not in cols:
                    self._db.execute(f"ALTER TABLE conversations ADD COLUMN {flag} INTEGER NOT NULL DEFAULT 0")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated)")
            self._db.commit()

    def create(self, title: str = "New conversation", user_id: int | None = None) -> str:
        cid = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO conversations(id, title, created, updated, user_id) VALUES (?,?,?,?,?)",
                             (cid, title, now, now, user_id))
            self._db.commit()
        return cid

    def exists(self, cid: str, user_id: int | None = None) -> bool:
        """True if the conversation exists (and, when user_id is given, belongs to that user)."""
        with self._lock:
            row = self._db.execute("SELECT user_id FROM conversations WHERE id=?", (cid,)).fetchone()
        return row is not None and (user_id is None or row["user_id"] == user_id)

    def claim_orphans(self, user_id) -> int:
        """Give conversations made on this computer before cloud accounts (no account, or an old local
        account) to the first cloud account that signs in here."""
        with self._lock:
            n = self._db.execute("UPDATE conversations SET user_id=? WHERE user_id IS NULL OR typeof(user_id)='integer'",
                                 (user_id,)).rowcount
            self._db.commit()
        return n

    def add(self, cid: str, role: str, content: str, meta: dict | None = None) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages(conv_id, role, content, created, meta) VALUES (?,?,?,?,?)",
                (cid, role, content, now, json.dumps(meta or {})),
            )
            self._db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, cid))
            if role == "assistant":   # a new answer: unread until the window showing it marks it read
                self._db.execute("UPDATE conversations SET unread=1 WHERE id=?", (cid,))
            elif role == "user":      # talking in an archived conversation brings it back
                self._db.execute("UPDATE conversations SET archived=0 WHERE id=?", (cid,))
            row = self._db.execute("SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()
            if role == "user" and row and row["title"] == "New conversation":
                title = content.strip().replace("\n", " ")
                self._db.execute("UPDATE conversations SET title=? WHERE id=?",
                                 (title[:60] + ("…" if len(title) > 60 else ""), cid))
            self._db.commit()
            return cur.lastrowid

    def update_message(self, mid: int, content: str, meta: dict | None = None) -> None:
        with self._lock:
            if meta is None:
                self._db.execute("UPDATE messages SET content=? WHERE id=?", (content, mid))
            else:
                self._db.execute("UPDATE messages SET content=?, meta=? WHERE id=?", (content, json.dumps(meta), mid))
            self._db.commit()

    def messages(self, cid: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, role, content, created, meta FROM messages WHERE conv_id=? ORDER BY id", (cid,)
            ).fetchall()
        return [{"id": r["id"], "role": r["role"], "content": r["content"], "created": r["created"],
                 "meta": json.loads(r["meta"] or "{}")} for r in rows]

    def list(self, limit: int = 200, user_id: int | None = None) -> list[dict]:
        where, args = ("WHERE c.user_id=? ", (user_id, limit)) if user_id is not None else ("", (limit,))
        with self._lock:
            rows = self._db.execute(
                "SELECT c.id, c.title, c.created, c.updated, c.pinned, c.archived, c.unread, COUNT(m.id) AS n "
                f"FROM conversations c LEFT JOIN messages m ON m.conv_id=c.id {where}"
                "GROUP BY c.id ORDER BY c.pinned DESC, c.updated DESC LIMIT ?", args
            ).fetchall()
        return [{**dict(r), **{f: bool(r[f]) for f in FLAGS}} for r in rows]

    def update(self, cid: str, title: str | None = None, **flags) -> dict:
        """Rename a conversation and/or set its pinned / archived / unread flags. Returns the new values."""
        sets, args = [], []
        if title is not None:
            title = " ".join(str(title).split())[:TITLE_MAX]
            if not title:
                raise ValueError("The name can't be empty.")
            sets.append("title=?"); args.append(title)
        for k, v in flags.items():
            if k in FLAGS and v is not None:
                sets.append(f"{k}=?"); args.append(1 if v else 0)
        with self._lock:
            if sets:
                self._db.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?", (*args, cid))
                self._db.commit()
            r = self._db.execute("SELECT id, title, pinned, archived, unread FROM conversations WHERE id=?", (cid,)).fetchone()
        return {**dict(r), **{f: bool(r[f]) for f in FLAGS}} if r else {}

    def delete(self, cid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE conv_id=?", (cid,))
            self._db.execute("DELETE FROM conversations WHERE id=?", (cid,))
            self._db.commit()

    def clear(self, user_id: int | None = None) -> None:
        """Delete all history (or only one user's history)."""
        with self._lock:
            if user_id is None:
                self._db.execute("DELETE FROM messages")
                self._db.execute("DELETE FROM conversations")
            else:
                self._db.execute("DELETE FROM messages WHERE conv_id IN (SELECT id FROM conversations WHERE user_id=?)", (user_id,))
                self._db.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
            self._db.commit()
            if self.persistent:
                self._db.execute("VACUUM")


def build_context(history: list[dict], max_chars: int, keep_min: int = 2) -> list[dict]:
    """Newest-first window of the conversation that fits in `max_chars`.

    Always keeps at least the last `keep_min` messages (the current question). Older messages that
    don't fit are dropped and replaced by a short marker so the model knows context was trimmed.
    """
    msgs = [{"role": m["role"], "content": m["content"]} for m in history
            if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
    out: list[dict] = []
    used = 0
    for i, m in enumerate(reversed(msgs)):
        size = len(m["content"]) + 16
        if used + size > max_chars and len(out) >= keep_min:
            dropped = len(msgs) - i
            out.append({"role": "user", "content": f"[{dropped} earlier messages omitted to fit the model's memory]"})
            break
        out.append(m)
        used += size
    out.reverse()
    return out
