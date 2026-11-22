"""Account system tests: hashing, sign-up/log-in rules, lockout, sessions, recovery, and the HTTP API
(cookies, access control, per-account history isolation).

    python -m unittest tests.test_accounts -v      (no models or Ollama needed)
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["VOICEASSIST_HOME"] = tempfile.mkdtemp(prefix="va-acct-")

from voiceassist import accounts as A  # noqa: E402
from voiceassist.accounts import AccountError, AccountStore  # noqa: E402
from voiceassist.config import Settings  # noqa: E402
from voiceassist.connectivity import ConnectionManager  # noqa: E402
from voiceassist.engine import Engine, EventBus  # noqa: E402
from voiceassist.history import HistoryStore  # noqa: E402
from voiceassist.server import App, start_server  # noqa: E402

PW = "correct-horse-7"


class TestAccountStore(unittest.TestCase):
    def setUp(self):
        self.s = AccountStore(":memory:")

    def tearDown(self):
        self.s._db.close()

    def test_password_is_hashed_not_stored(self):
        u, code = self.s.signup("mohit", PW, "Mohit")
        row = self.s._db.execute("SELECT pw_hash, recovery_hash FROM users").fetchone()
        self.assertTrue(row["pw_hash"].startswith("scrypt$"))
        self.assertNotIn(PW, row["pw_hash"])
        self.assertNotIn(code.replace("-", ""), row["recovery_hash"])
        self.assertEqual(u["display_name"], "Mohit")

    def test_signup_validation(self):
        for bad_user in ["ab", "-bad", "has space", "x" * 40]:
            with self.assertRaises(AccountError) as cm:
                self.s.signup(bad_user, PW)
            self.assertEqual(cm.exception.field, "username")
        for bad_pw in ["short1", "lettersonly", "12345678"]:
            with self.assertRaises(AccountError) as cm:
                self.s.signup("validuser", bad_pw)
            self.assertEqual(cm.exception.field, "password")
        with self.assertRaises(AccountError):
            self.s.signup("validuser", PW, email="not-an-email")

    def test_username_unique_case_insensitive(self):
        self.s.signup("Mohit", PW)
        with self.assertRaises(AccountError) as cm:
            self.s.signup("mohit", PW)
        self.assertEqual(cm.exception.status, 409)

    def test_login_and_generic_errors(self):
        self.s.signup("mohit", PW)
        self.assertEqual(self.s.login("MOHIT", PW)["username"], "mohit")
        with self.assertRaises(AccountError) as wrong_pw:
            self.s.login("mohit", "wrong-pass-1")
        with self.assertRaises(AccountError) as no_user:
            self.s.login("nobody", "wrong-pass-1")
        self.assertEqual(str(wrong_pw.exception), str(no_user.exception))  # doesn't reveal which usernames exist

    def test_lockout_after_repeated_failures(self):
        self.s.signup("mohit", PW)
        for _ in range(A.MAX_FAILS - 1):
            with self.assertRaises(AccountError):
                self.s.login("mohit", "wrong-pass-1")
        with self.assertRaises(AccountError) as cm:
            self.s.login("mohit", "wrong-pass-1")
        self.assertEqual(cm.exception.status, 429)
        with self.assertRaises(AccountError) as cm:
            self.s.login("mohit", PW)  # even the right password waits out the lock
        self.assertEqual(cm.exception.status, 429)
        self.s._db.execute("UPDATE users SET locked_until=0")
        self.assertTrue(self.s.login("mohit", PW))

    def test_sessions_expire_and_end(self):
        u, _ = self.s.signup("mohit", PW)
        tok, max_age = self.s.create_session(u["id"], remember=True)
        self.assertEqual(max_age, A.SESSION_REMEMBER_S)
        self.assertEqual(self.s.session_user(tok)["id"], u["id"])
        self.assertIsNone(self.s.session_user("forged-token"))
        tok2, max_age2 = self.s.create_session(u["id"], remember=False)
        self.assertIsNone(max_age2)  # browser-session cookie
        self.s._db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
        self.assertIsNone(self.s.session_user(tok2))
        tok3, _ = self.s.create_session(u["id"], True)
        self.s.end_session(tok3)
        self.assertIsNone(self.s.session_user(tok3))

    def test_recovery_code_resets_password_once(self):
        self.s.signup("mohit", PW)
        code = self.s.signup("other", PW)[1]
        u, code = self.s.signup("third", PW)
        user, new_code = self.s.reset_with_recovery("third", code.lower(), "brand-new-pass9")
        self.assertNotEqual(code, new_code)
        self.assertTrue(self.s.login("third", "brand-new-pass9"))
        with self.assertRaises(AccountError):
            self.s.reset_with_recovery("third", code, "another-pass-9")  # old code no longer works
        with self.assertRaises(AccountError):
            self.s.reset_with_recovery("third", "AAAA-BBBB-CCCC-DDDD-EEEE", "another-pass-9")

    def test_change_password_signs_out_other_sessions(self):
        u, _ = self.s.signup("mohit", PW)
        keep, _ = self.s.create_session(u["id"], True)
        other, _ = self.s.create_session(u["id"], True)
        with self.assertRaises(AccountError):
            self.s.change_password(u["id"], "wrong-pass-1", "new-pass-word-2")
        self.s.change_password(u["id"], PW, "new-pass-word-2", keep_token=keep)
        self.assertIsNotNone(self.s.session_user(keep))
        self.assertIsNone(self.s.session_user(other))
        self.assertTrue(self.s.login("mohit", "new-pass-word-2"))

    def test_delete_requires_password(self):
        u, _ = self.s.signup("mohit", PW)
        with self.assertRaises(AccountError):
            self.s.delete(u["id"], "wrong-pass-1")
        self.s.delete(u["id"], PW)
        self.assertEqual(self.s.count(), 0)


class Client:
    """Minimal HTTP client that keeps the session cookie, like the app window does."""

    def __init__(self, app):
        self.app, self.cookie = app, None

    def call(self, method, path, body=None, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.app.port}"}
        if token:
            h["X-App-Token"] = self.app.token
        if self.cookie:
            h["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        raw = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0] if "Max-Age=0" not in sc else None
            self.last_set_cookie = sc
        try:
            return r.status, json.loads(raw or b"{}")
        except ValueError:
            return r.status, raw


class TestAccountAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = Settings(Path(os.environ["VOICEASSIST_HOME"]) / "settings.json")
        s.update({"strict_offline": True, "local": {"preload": False}})
        hist = HistoryStore(None)
        cls.hist = hist
        cls.legacy = hist.create()  # a conversation from before accounts existed
        hist.add(cls.legacy, "user", "old question")
        engine = Engine(s, ConnectionManager(s), hist, EventBus())
        cls.app = App(s, engine.conn, hist, engine, engine.bus, accounts=AccountStore(":memory:"))
        cls.srv = start_server(cls.app, 0)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_full_account_flow(self):
        a, b = Client(self.app), Client(self.app)
        # not logged in: app API is closed, sign-in endpoints open
        self.assertEqual(a.call("GET", "/api/conversations")[0], 401)
        st, me = a.call("GET", "/api/auth/me")
        self.assertEqual((st, me["user"], me["has_accounts"]), (200, None, False))
        # first sign-up claims the old conversations, sets a secure session cookie
        st, r = a.call("POST", "/api/auth/signup", {"username": "mohit", "password": PW, "display_name": "Mohit"})
        self.assertEqual(st, 201)
        self.assertRegex(r["recovery_code"], r"^[A-Z0-9]{4}(-[A-Z0-9]{4}){4}$")
        self.assertEqual(r["claimed_conversations"], 1)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, a.last_set_cookie)
        st, convs = a.call("GET", "/api/conversations")
        self.assertEqual([c["id"] for c in convs["conversations"]], [self.legacy])
        # second user: separate history, cannot read or delete the first user's conversation
        st, r2 = b.call("POST", "/api/auth/signup", {"username": "guest", "password": PW})
        self.assertEqual(r2["claimed_conversations"], 0)
        self.assertEqual(b.call("GET", "/api/conversations")[1]["conversations"], [])
        self.assertEqual(b.call("GET", f"/api/conversations/{self.legacy}")[0], 404)
        self.assertEqual(b.call("DELETE", f"/api/conversations/{self.legacy}")[0], 404)
        b.call("POST", "/api/history/clear")
        self.assertTrue(self.hist.exists(self.legacy), "clearing guest history must not touch mohit's")
        # typed turn by guest goes into guest's own conversation even if it names mohit's conversation
        st, _ = b.call("POST", "/api/turn", {"text": "hello", "conversation_id": self.legacy})
        self.assertEqual(len(self.hist.messages(self.legacy)), 1, "guest's message must not land in mohit's conversation")
        # logout closes the API again; login works; wrong password is rejected
        self.assertEqual(a.call("POST", "/api/auth/logout")[0], 200)
        self.assertEqual(a.call("GET", "/api/state")[0], 401)
        self.assertEqual(a.call("POST", "/api/auth/login", {"username": "mohit", "password": "wrong-pass-1"})[0], 401)
        st, r = a.call("POST", "/api/auth/login", {"username": "mohit", "password": PW})
        self.assertEqual((st, r["user"]["username"]), (200, "mohit"))
        self.assertEqual(a.call("GET", "/api/state")[1]["user"]["username"], "mohit")
        # profile + password change
        st, r = a.call("POST", "/api/account/profile", {"display_name": "Mohit S"})
        self.assertEqual(r["user"]["display_name"], "Mohit S")
        st, r = a.call("POST", "/api/account/password", {"current_password": "nope-nope-1", "new_password": "x"})
        self.assertEqual((st, r["field"]), (401, "current_password"))
        # delete guest account (needs password) -> its history is gone, session ends
        self.assertEqual(b.call("POST", "/api/account/delete", {"password": "wrong-pass-1"})[0], 401)
        self.assertEqual(b.call("POST", "/api/account/delete", {"password": PW})[0], 200)
        self.assertEqual(b.call("GET", "/api/state")[0], 401)
        self.assertEqual(self.hist.list(user_id=2), [])

    def test_token_still_required(self):
        c = Client(self.app)
        self.assertEqual(c.call("GET", "/api/auth/me", token=False)[0], 401)

    def test_forged_cookie_rejected(self):
        c = Client(self.app)
        c.cookie = "va_session=forged"
        self.assertEqual(c.call("GET", "/api/state")[0], 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
