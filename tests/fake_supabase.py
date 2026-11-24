"""A fake VoiceAssist cloud (Supabase Auth + the "chat" and "delete-account" Edge Functions) for tests.

Speaks the same HTTP API the real project does, so the desktop, phone and website apps can be tested without
a Supabase project or an AI key.

    python tests/fake_supabase.py [port] [--confirm] [--limit N]
    from tests.fake_supabase import FakeSupabase; fs = FakeSupabase(limit=3).start(); fs.url
"""
from __future__ import annotations

import base64
import json
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ANON_KEY = "test-anon-key"
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
        "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS", "Access-Control-Expose-Headers": "x-questions-left, x-daily-limit"}


def answer_for(question: str) -> str:
    q = question.lower()
    if "france" in q:
        return "The capital of France is Paris."
    if "germany" in q:
        return "The capital of Germany is Berlin."
    if "japan" in q:
        return "The capital of Japan is Tokyo."
    if "2+2" in q or "2 + 2" in q:
        return "2 plus 2 equals 4."
    return "Hello! I'm the VoiceAssist cloud AI. How can I help?"


class FakeSupabase:
    def __init__(self, port: int = 0, confirm: bool = False, limit: int = 50, delay: float = 0.03):
        self.confirm, self.limit, self.delay = confirm, limit, delay
        self.users: dict[str, dict] = {}         # email -> user
        self.tokens: dict[str, str] = {}         # access token -> email
        self.refresh: dict[str, str] = {}        # refresh token -> email
        self.usage: dict[str, int] = {}          # user id -> questions today
        self.requests: list[tuple[str, str]] = []
        self.recover_emails: list[str] = []
        self.chat_bodies: list[dict] = []
        self.ai_down = False                     # simulate the AI failing (refund)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj=None, headers=None, raw=None, ctype="application/json"):
                self.send_response(status)
                for k, v in {**CORS, **(headers or {})}.items():
                    self.send_header(k, v)
                body = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return {}

            def _user(self):
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                email = fake.tokens.get(tok)
                return fake.users.get(email) if email else None

            def do_OPTIONS(self):
                self._send(200, raw=b"ok", ctype="text/plain")

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

            def _route(self, method):
                u = urlsplit(self.path)
                fake.requests.append((method, u.path))
                if self.headers.get("apikey") != ANON_KEY:
                    return self._send(401, {"message": "Invalid API key"})
                q = parse_qs(u.query)
                if method == "POST" and u.path == "/auth/v1/signup":
                    return fake.signup(self, self._body())
                if method == "POST" and u.path == "/auth/v1/token":
                    return fake.token(self, q.get("grant_type", [""])[0], self._body())
                if method == "POST" and u.path == "/auth/v1/recover":
                    fake.recover_emails.append(self._body().get("email", ""))
                    return self._send(200, {})
                user = self._user()
                if u.path.startswith("/auth/v1/") and not user:
                    return self._send(401, {"code": 401, "error_code": "bad_jwt", "msg": "invalid JWT"})
                if u.path.startswith("/functions/v1/") and not user:
                    return self._send(401, {"error": "Please sign in to VoiceAssist again.", "code": "auth"})
                if u.path == "/auth/v1/user" and method == "GET":
                    return self._send(200, fake.public(user))
                if u.path == "/auth/v1/user" and method == "PUT":
                    b = self._body()
                    if "password" in b:
                        user["password"] = b["password"]
                    if "data" in b:
                        user["meta"].update(b["data"])
                    return self._send(200, fake.public(user))
                if u.path == "/auth/v1/logout":
                    return self._send(204, raw=b"")
                if u.path == "/functions/v1/delete-account":
                    fake.users.pop(user["email"], None)
                    return self._send(200, {"ok": True})
                if u.path == "/functions/v1/chat":
                    return fake.chat(self, method, user)
                self._send(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()

    # ------------------------------------------------------------------ auth
    def public(self, u):
        return {"id": u["id"], "email": u["email"], "user_metadata": dict(u["meta"]), "created_at": "2026-11-22T00:00:00Z",
                "last_sign_in_at": "2026-11-22T00:00:00Z"}

    def session(self, u):
        at, rt = secrets.token_hex(16), secrets.token_hex(16)
        self.tokens[at] = u["email"]
        self.refresh[rt] = u["email"]
        return {"access_token": at, "refresh_token": rt, "expires_in": 3600, "token_type": "bearer", "user": self.public(u)}

    def signup(self, h, b):
        email, pw = (b.get("email") or "").strip().lower(), b.get("password") or ""
        if "@" not in email:
            return h._send(400, {"code": 400, "error_code": "validation_failed", "msg": "Unable to validate email address: invalid format"})
        if len(pw) < 6:
            return h._send(422, {"code": 422, "error_code": "weak_password", "msg": "Password should be at least 6 characters."})
        if email in self.users:
            if self.confirm:   # real Supabase hides existing accounts when confirmations are on
                return h._send(200, {"id": str(uuid.uuid4()), "email": email, "identities": []})
            return h._send(422, {"code": 422, "error_code": "user_already_exists", "msg": "User already registered"})
        u = {"id": str(uuid.uuid4()), "email": email, "password": pw, "meta": dict(b.get("data") or {}), "confirmed": not self.confirm}
        self.users[email] = u
        if self.confirm:
            return h._send(200, {**self.public(u), "identities": [{"id": u["id"]}], "confirmation_sent_at": "now"})
        h._send(200, self.session(u))

    def confirm_all(self):
        for u in self.users.values():
            u["confirmed"] = True

    def token(self, h, grant, b):
        if grant == "password":
            u = self.users.get((b.get("email") or "").strip().lower())
            if not u or u["password"] != b.get("password"):
                return h._send(400, {"code": 400, "error_code": "invalid_credentials", "msg": "Invalid login credentials"})
            if not u["confirmed"]:
                return h._send(400, {"code": 400, "error_code": "email_not_confirmed", "msg": "Email not confirmed"})
            return h._send(200, self.session(u))
        if grant == "refresh_token":
            email = self.refresh.pop(b.get("refresh_token", ""), None)
            if not email or email not in self.users:
                return h._send(400, {"code": 400, "error_code": "refresh_token_not_found", "msg": "Invalid Refresh Token"})
            return h._send(200, self.session(self.users[email]))
        h._send(400, {"msg": "unsupported grant"})

    # ------------------------------------------------------------------ chat function
    def chat(self, h, method, user):
        used = self.usage.get(user["id"], 0)
        if method == "GET":
            return h._send(200, {"used": used, "limit": self.limit, "left": max(0, self.limit - used), "model": "fake-gemini"})
        b = h._body()
        if "transcribe" in b:
            return h._send(200, {"text": "What is the capital of France?"})
        self.chat_bodies.append(b)
        msgs = b.get("messages") or []
        if not msgs or msgs[-1].get("role") != "user":
            return h._send(400, {"error": "No question to answer."})
        if used >= self.limit:
            return h._send(429, {"error": f"You've used all {self.limit} cloud questions for today.", "code": "limit"},
                           headers={"x-questions-left": "0", "x-daily-limit": str(self.limit)})
        if self.ai_down:
            return h._send(503, {"error": "The free AI has reached its limit for the moment — please try again in a minute.", "code": "busy"})
        self.usage[user["id"]] = used + 1
        text = answer_for(msgs[-1].get("content", ""))
        h.send_response(200)
        for k, v in {**CORS, "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                     "x-questions-left": str(self.limit - used - 1), "x-daily-limit": str(self.limit)}.items():
            h.send_header(k, v)
        h.end_headers()
        for word in text.split(" "):
            ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": word + " "}}
            h.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            h.wfile.flush()
            time.sleep(self.delay)
        h.wfile.write(b'data: {"type":"message_stop"}\n\n')
        h.wfile.flush()
        h.close_connection = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 54321
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 50
    fs = FakeSupabase(port, confirm="--confirm" in sys.argv, limit=limit).start()
    print(f"fake VoiceAssist cloud on {fs.url} (anon key {ANON_KEY})", flush=True)
    threading.Event().wait()
