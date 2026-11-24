"""API-level end-to-end test of the VoiceAssist cloud flow against a running engine (desktop or phone core).

Starts the fake VoiceAssist cloud (tests/fake_supabase.py), then the engine command you give with
VOICEASSIST_SUPABASE_URL / _KEY pointing at it, waits for <data>/last_url.txt and drives the HTTP API:
sign-up, typed cloud answer (no AI key anywhere), usage, daily limit -> offline AI fallback, profile, password,
logout / login, forgot password, delete account.

    python tests/e2e_cloud_api.py --data /tmp/va-core -- gradle -q --offline -PcoreOnly :core:devServer -Dva.data=/tmp/va-core
    python tests/e2e_cloud_api.py --data /tmp/va-py -- python -m voiceassist --no-window --port 8799
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fake_supabase import ANON_KEY, FakeSupabase  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


class Api:
    def __init__(self, url):
        self.base = url.split("#")[0].rstrip("/")
        self.token = url.split("token=")[-1]
        self.new_jar()

    def new_jar(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, timeout=120):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"X-App-Token": self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, method, path, body=None):
        st, raw = self.call(method, path, body)
        try:
            return st, json.loads(raw or b"{}")
        except ValueError:
            return st, {"raw": raw[:200]}

    def ask(self, text):
        st, raw = self.call("POST", "/api/turn", {"text": text}, timeout=600)
        return [json.loads(l) for l in raw.decode().splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="the engine's data folder (where it writes last_url.txt)")
    ap.add_argument("--cloud-port", type=int, default=54331)
    ap.add_argument("--keep", action="store_true", help="don't wipe the data folder first")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    data = Path(a.data)
    if not a.keep and data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True, exist_ok=True)
    cloud = FakeSupabase(a.cloud_port, limit=3).start()
    env = {**os.environ, "VOICEASSIST_SUPABASE_URL": cloud.url, "VOICEASSIST_SUPABASE_KEY": ANON_KEY, "VOICEASSIST_HOME": str(data)}
    proc = subprocess.Popen(cmd, env=env, stdout=open(data.parent / (data.name + ".log"), "w"), stderr=subprocess.STDOUT, cwd=os.getcwd())
    try:
        url = ""
        for _ in range(600):
            f = data / "last_url.txt"
            if f.exists() and f.read_text().strip():
                url = f.read_text().strip()
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if not url:
            print((data.parent / (data.name + ".log")).read_text()[-3000:])
            check("engine started", False)
            return 1
        time.sleep(1)
        api = Api(url)
        st, me = api.json("GET", "/api/auth/me")
        check("engine started, not signed in", st == 200 and me.get("user") is None, json.dumps(me)[:120])

        st, r = api.json("POST", "/api/auth/signup", {"email": "bad", "password": "x"})
        check("sign-up validates the email", st == 400 and r.get("field") == "email", r.get("error", ""))
        st, r = api.json("POST", "/api/auth/signup", {"email": "e2e@example.com", "password": "e2e-pass-123", "display_name": "E2E"})
        check("cloud account created and signed in", st == 201 and r.get("user", {}).get("email") == "e2e@example.com", str(st))
        st, s = api.json("GET", "/api/state")
        check("state: connected to the VoiceAssist cloud, no API keys", s.get("cloud", {}).get("configured") is True and "keys" not in s)

        evs = api.ask("What is the capital of France?")
        ans = "".join(e["text"] for e in evs if e["type"] == "token")
        start = next((e for e in evs if e["type"] == "llm_start"), {})
        done = next((e for e in evs if e["type"] == "done"), {})
        check("typed question answered by the shared cloud AI", "paris" in ans.lower() and start.get("kind") == "cloud", f"{start.get('label')}: {ans[:60]}")
        check("cloud request has only the conversation (no key)", cloud.chat_bodies and set(cloud.chat_bodies[-1]) <= {"system", "messages", "max_tokens"})
        check("questions left reported", done.get("provider", {}).get("questions_left") == 2, str(done.get("provider")))
        st, u = api.json("GET", "/api/cloud/usage")
        check("usage endpoint", st == 200 and u.get("left") == 2 and u.get("limit") == 3, json.dumps(u))

        api.ask("What is the capital of Germany?")
        api.ask("What is 2 plus 2?")
        evs = api.ask("One more question?")
        kinds = [e["type"] for e in evs]
        fail = next((e for e in evs if e["type"] in ("failover", "error")), {})
        check("daily limit reached: cloud refuses, the app falls back or explains",
              ("failover" in kinds or "error" in kinds) and "today" in (fail.get("message", "") + fail.get("hint", "")).lower(), fail.get("message", "")[:120])

        st, r = api.json("POST", "/api/account/profile", {"display_name": "Renamed"})
        check("profile name saved in the cloud", st == 200 and r["user"]["display_name"] == "Renamed"
              and cloud.users["e2e@example.com"]["meta"].get("display_name") == "Renamed")
        st, r = api.json("POST", "/api/account/password", {"current_password": "nope-123", "new_password": "e2e-pass-456"})
        check("change password: wrong current password refused", st == 401 and r.get("field") == "current_password")
        st, r = api.json("POST", "/api/account/password", {"current_password": "e2e-pass-123", "new_password": "e2e-pass-456"})
        check("change password", st == 200 and cloud.users["e2e@example.com"]["password"] == "e2e-pass-456", str(r))

        st, _ = api.json("POST", "/api/auth/logout", {})
        st2, me = api.json("GET", "/api/auth/me")
        check("logout", st == 200 and me.get("user") is None)
        st, r = api.json("POST", "/api/auth/login", {"email": "e2e@example.com", "password": "e2e-pass-123"})
        check("old password no longer works", st == 401 and "Incorrect" in r.get("error", ""), r.get("error", ""))
        st, r = api.json("POST", "/api/auth/login", {"email": "E2E@example.com", "password": "e2e-pass-456"})
        check("login with the new password", st == 200 and r["user"]["email"] == "e2e@example.com", str(st))
        st, r = api.json("POST", "/api/auth/recover", {"email": "e2e@example.com"})
        check("forgot password sends the reset email", st == 200 and cloud.recover_emails[-1:] == ["e2e@example.com"])
        st, r = api.json("POST", "/api/account/delete", {"password": "e2e-pass-456"})
        check("delete account (in the cloud)", st == 200 and "e2e@example.com" not in cloud.users, str(r))
        st, me = api.json("GET", "/api/auth/me")
        check("signed out after deleting", me.get("user") is None)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        cloud.stop()
    failed = [n for n, ok in results if not ok]
    print("\nALL CLOUD API CHECKS PASSED" if not failed else f"\nFAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
