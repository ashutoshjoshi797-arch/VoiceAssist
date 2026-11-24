"""Account tests: VoiceAssist cloud accounts (Supabase Auth, simulated by tests/fake_supabase.py) and the local
HTTP API (session cookies, access control, per-account history isolation, offline sign-in).

    python -m unittest tests.test_accounts -v      (no models, Ollama or internet needed)
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

from tests.fake_supabase import ANON_KEY, FakeSupabase  # noqa: E402
from voiceassist.accounts import AccountError  # noqa: E402
from voiceassist.cloud import CloudAccounts  # noqa: E402
from voiceassist.config import Settings  # noqa: E402
from voiceassist.connectivity import ConnectionManager  # noqa: E402
from voiceassist.engine import Engine, EventBus  # noqa: E402
from voiceassist.history import HistoryStore  # noqa: E402
from voiceassist.server import App, start_server  # noqa: E402

PW = "correct-horse-7"


def accounts_for(fake, name="s"):
    return CloudAccounts(str(Path(os.environ["VOICEASSIST_HOME"]) / f"{name}-{time.time()}.json"),
                         {"url": fake.url, "key": ANON_KEY, "site_url": "https://example.github.io/VoiceAssist/"})


class TestCloudAccounts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeSupabase().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def test_validation_happens_before_any_network_call(self):
        s = accounts_for(self.fake)
        n = len(self.fake.requests)
        for email, pw, field in [("not-an-email", PW, "email"), ("a@b.co", "short1", "password"), ("a@b.co", "lettersonly", "password")]:
            with self.assertRaises(AccountError) as cm:
                s.signup(email, pw)
            self.assertEqual(cm.exception.field, field)
        self.assertEqual(len(self.fake.requests), n)

    def test_signup_login_errors_and_session_survive_restart(self):
        s = accounts_for(self.fake, "persist")
        u, confirm = s.signup("Mohit@Example.com ", PW, "Mohit")
        self.assertFalse(confirm)
        self.assertEqual((u["email"], u["display_name"]), ("mohit@example.com", "Mohit"))
        with self.assertRaises(AccountError) as cm:
            s.signup("mohit@example.com", PW)
        self.assertEqual((cm.exception.status, cm.exception.field), (409, "email"))
        with self.assertRaises(AccountError) as cm:
            s.login("mohit@example.com", "wrong-pass-1")
        self.assertEqual((cm.exception.status, cm.exception.field), (401, "password"))
        tok, max_age = s.create_session(u["id"], remember=True)
        self.assertEqual(max_age, 30 * 24 * 3600)
        # restart the app with the internet gone: still signed in (cached), so the offline AI is usable
        again = CloudAccounts(str(s.path), {"url": "http://127.0.0.1:9", "key": ANON_KEY, "site_url": ""})
        self.assertEqual(again.session_user(tok)["email"], "mohit@example.com")
        self.assertIsNone(again.session_user("forged"))
        with self.assertRaises(AccountError) as cm:   # but signing in a new account needs internet, with a clear message
            again.login("x@y.co", PW)
        self.assertIn("offline", str(cm.exception))
        self.assertNotIn(PW, s.path.read_text(), "passwords are never stored")

    def test_expired_access_token_is_refreshed(self):
        s = accounts_for(self.fake)
        u, _ = s.signup("refresh@example.com", PW)
        s.db["users"][u["id"]]["expires_at"] = time.time() - 1
        old = s.db["users"][u["id"]]["access"]
        new = s.access_token(u["id"])
        self.assertNotEqual(old, new)
        self.assertEqual(s.usage(u["id"])["limit"], 50)

    def test_email_confirmation_flow(self):
        fake = FakeSupabase(confirm=True).start()
        try:
            s = accounts_for(fake)
            self.assertEqual(s.signup("new@example.com", PW, "New"), (None, True))
            with self.assertRaises(AccountError) as cm:
                s.login("new@example.com", PW)
            self.assertIn("confirm", str(cm.exception))
            fake.confirm_all()
            self.assertEqual(s.login("new@example.com", PW)["display_name"], "New")
        finally:
            fake.stop()

    def test_password_reset_email_and_not_configured(self):
        s = accounts_for(self.fake)
        s.request_password_reset("someone@example.com")
        self.assertIn("someone@example.com", self.fake.recover_emails)
        blank = CloudAccounts(str(Path(os.environ["VOICEASSIST_HOME"]) / "blank.json"), {"url": "", "key": "", "site_url": ""})
        self.assertFalse(blank.configured)
        with self.assertRaises(AccountError):
            blank.login("a@b.co", PW)


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
        cls.fake = FakeSupabase().start()
        s = Settings(Path(os.environ["VOICEASSIST_HOME"]) / "settings.json")
        s.update({"local": {"preload": False}})
        hist = HistoryStore(None)
        cls.hist = hist
        cls.legacy = hist.create()  # a conversation from before cloud accounts existed
        hist.add(cls.legacy, "user", "old question")
        engine = Engine(s, ConnectionManager(s), hist, EventBus())
        cls.app = App(s, engine.conn, hist, engine, engine.bus, accounts=accounts_for(cls.fake, "api"))
        cls.srv = start_server(cls.app, 0)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.fake.stop()

    def test_full_account_flow(self):
        a, b = Client(self.app), Client(self.app)
        # not logged in: app API is closed, sign-in endpoints open; no AI key settings exist any more
        self.assertEqual(a.call("GET", "/api/conversations")[0], 401)
        self.assertEqual(a.call("GET", "/api/auth/me")[1]["user"], None)
        # first cloud account on this computer gets the old conversations, and a secure session cookie
        st, r = a.call("POST", "/api/auth/signup", {"email": "mohit@example.com", "password": PW, "display_name": "Mohit"})
        self.assertEqual(st, 201)
        self.assertEqual(r["claimed_conversations"], 1)
        uid = r["user"]["id"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, a.last_set_cookie)
        self.assertEqual([c["id"] for c in a.call("GET", "/api/conversations")[1]["conversations"]], [self.legacy])
        st, state = a.call("GET", "/api/state")
        self.assertTrue(state["cloud"]["configured"])
        self.assertNotIn("keys", state)
        self.assertEqual(a.call("POST", "/api/keys", {"provider": "anthropic", "key": "x"})[0], 404)   # no AI keys any more
        self.assertEqual(a.call("GET", "/api/cloud/usage")[1]["left"], 50)
        # second user: separate history, cannot read or delete the first user's conversation
        st, r2 = b.call("POST", "/api/auth/signup", {"email": "guest@example.com", "password": PW})
        self.assertEqual(r2["claimed_conversations"], 0)
        self.assertEqual(b.call("GET", "/api/conversations")[1]["conversations"], [])
        self.assertEqual(b.call("GET", f"/api/conversations/{self.legacy}")[0], 404)
        self.assertEqual(b.call("DELETE", f"/api/conversations/{self.legacy}")[0], 404)
        b.call("POST", "/api/history/clear")
        self.assertTrue(self.hist.exists(self.legacy), "clearing guest history must not touch mohit's")
        st, _ = b.call("POST", "/api/turn", {"text": "hello", "conversation_id": self.legacy})
        self.assertEqual(len(self.hist.messages(self.legacy)), 1, "guest's message must not land in mohit's conversation")
        # logout closes the API again; login works; wrong password is rejected
        self.assertEqual(a.call("POST", "/api/auth/logout")[0], 200)
        self.assertEqual(a.call("GET", "/api/state")[0], 401)
        st, r = a.call("POST", "/api/auth/login", {"email": "mohit@example.com", "password": "wrong-pass-1"})
        self.assertEqual((st, r["field"]), (401, "password"))
        st, r = a.call("POST", "/api/auth/login", {"email": "mohit@example.com", "password": PW})
        self.assertEqual((st, r["user"]["email"]), (200, "mohit@example.com"))
        # profile + password change (checks the current password with the cloud)
        st, r = a.call("POST", "/api/account/profile", {"display_name": "Mohit S"})
        self.assertEqual(r["user"]["display_name"], "Mohit S")
        st, r = a.call("POST", "/api/account/password", {"current_password": "nope-nope-1", "new_password": "new-pass-22"})
        self.assertEqual((st, r["field"]), (401, "current_password"))
        self.assertEqual(a.call("POST", "/api/account/password", {"current_password": PW, "new_password": "new-pass-22"})[0], 200)
        # forgot password: the cloud emails a reset link
        self.assertEqual(a.call("POST", "/api/auth/recover", {"email": "mohit@example.com"})[0], 200)
        # delete guest account (needs password) -> account gone from the cloud, history gone, session ends
        self.assertEqual(b.call("POST", "/api/account/delete", {"password": "wrong-pass-1"})[0], 401)
        self.assertEqual(b.call("POST", "/api/account/delete", {"password": PW})[0], 200)
        self.assertEqual(b.call("GET", "/api/state")[0], 401)
        self.assertNotIn("guest@example.com", self.fake.users)
        self.assertIn(uid, [u["id"] for u in self.fake.users.values()])

    def test_needs_confirmation_is_reported(self):
        fake = FakeSupabase(confirm=True).start()
        try:
            old = self.app.accounts
            self.app.accounts = accounts_for(fake)
            st, r = Client(self.app).call("POST", "/api/auth/signup", {"email": "c@example.com", "password": PW})
            self.assertEqual((st, r.get("needs_confirmation")), (202, True))
        finally:
            self.app.accounts = old
            fake.stop()

    def test_token_still_required(self):
        c = Client(self.app)
        self.assertEqual(c.call("GET", "/api/auth/me", token=False)[0], 401)

    def test_forged_cookie_rejected(self):
        c = Client(self.app)
        c.cookie = "va_session=forged"
        self.assertEqual(c.call("GET", "/api/state")[0], 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
