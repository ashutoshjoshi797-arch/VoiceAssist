"""End-to-end test of the VoiceAssist APK on an Android emulator (GitHub Actions).

    python packaging/android/smoke_test.py path/to/app.apk

1. installs the APK and opens it like a user would      2. checks the sign-in screen is shown
3. signs up an account through the app's API             4. one-click offline setup (downloads speech + AI models)
5. typed and spoken questions answered on the phone      6. AIRPLANE MODE ON: proves the internet is gone, then
   asks again (typed + spoken) and checks the on-device AI answers and the app shows AIRPLANE MODE
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PKG = "com.voiceassist.app"
MODEL = os.environ.get("SMOKE_MODEL", "qwen2.5:0.5b")
OUT = Path(os.environ.get("SMOKE_OUT", "android-test-output"))
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    return ok


def adb(*args: str, timeout: int = 120, check_rc: bool = False) -> str:
    r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: {r.stderr or r.stdout}")
    return (r.stdout or "") + (r.stderr or "")


def screenshot(name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{name}.png", "wb") as f:
        subprocess.run(["adb", "exec-out", "screencap", "-p"], stdout=f, timeout=60)


def screen_text() -> str:
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=60)
    xml = adb("shell", "cat", "/sdcard/ui.xml")
    return " ".join(re.findall(r'(?:text|content-desc)="([^"]+)"', xml))


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, raw=None, ctype="application/json", timeout=120):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-App-Token": self.token, "Content-Type": ctype})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, *a, **kw):
        st, b = self.call(*a, **kw)
        return st, json.loads(b or b"{}")

    def turn(self, **kw):
        st, b = self.call("POST", "/api/turn", timeout=900, **kw)
        return [json.loads(l) for l in b.decode("utf-8").splitlines() if l.strip()]


def ask(api: Api, label: str, expect_kind: str = "local") -> None:
    evs = api.turn(body={"text": "What is the capital of France? Answer in one short sentence."})
    answer = "".join(e["text"] for e in evs if e["type"] == "token")
    start = next((e for e in evs if e["type"] == "llm_start"), {})
    check(f"{label}: typed question answered by the on-device AI", "paris" in answer.lower() and start.get("kind") == expect_kind,
          f"{start.get('kind')}: {answer[:100]}")
    check(f"{label}: answer spoken (audio produced on the phone)", any(e["type"] == "audio" for e in evs))
    st, wav = api.call("POST", "/api/tts/test", {"text": "What is the capital of Germany?"})
    evs = api.turn(raw=wav, ctype="audio/wav")
    heard = next((e["text"] for e in evs if e["type"] == "transcript"), "")
    answer = "".join(e["text"] for e in evs if e["type"] == "token")
    check(f"{label}: spoken question recognised by Whisper on the phone", "germany" in heard.lower(), heard)
    check(f"{label}: spoken question answered", bool(answer.strip()), answer[:100])


def main(apk: str) -> int:
    print(adb("install", "-r", "-g", apk, timeout=600, check_rc=True).strip())
    adb("shell", "pm", "grant", PKG, "android.permission.RECORD_AUDIO")
    adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity", timeout=120)

    url = ""
    for _ in range(120):
        url = adb("shell", "run-as", PKG, "cat", "files/voiceassist/last_url.txt").strip()
        if url.startswith("http://127.0.0.1"):
            break
        time.sleep(1)
    if not check("app opened and on-device engine started", url.startswith("http://127.0.0.1"), url[:80]):
        print(adb("logcat", "-d", "-t", "300"))
        return 1
    port = re.search(r":(\d+)/", url).group(1)
    token = url.split("token=")[-1]
    adb("forward", "tcp:18765", f"tcp:{port}", check_rc=True)
    api = Api("http://127.0.0.1:18765", token)

    seen = ""
    for _ in range(30):
        seen = screen_text()
        if "Log in" in seen or "Sign up" in seen:
            break
        time.sleep(2)
    screenshot("1-sign-in")
    check("sign-in screen shown in the app", "Log in" in seen and "Sign up" in seen, seen[:150])

    st, v = api.json("GET", "/api/version")
    check("engine answers", st == 200, json.dumps(v))
    st, r = api.json("POST", "/api/auth/signup", {"username": "phonetest", "password": "phone-test-123"})
    check("account sign-up", st == 201 and "recovery_code" in r, str(st))
    st, s = api.json("GET", "/api/state")
    check("phone reports ONLINE with internet", str(s.get("network", {}).get("state")).upper() == "ONLINE" and s.get("platform") == "android",
          f"{s.get('network', {}).get('state')} / {s.get('platform')}")

    st, job = api.json("POST", "/api/setup/start", {"model": MODEL})
    check("one-click offline setup started", st == 200, job.get("step", ""))
    t0, last, s = time.time(), "", {}
    while time.time() - t0 < 40 * 60:
        st, s = api.json("GET", "/api/setup/status")
        job = s.get("job", {})
        if job.get("step") != last:
            last = job.get("step", "")
            print(f"   [{job.get('state')}] {last}", flush=True)
        if job.get("state") in ("done", "error"):
            break
        time.sleep(5)
    check("offline setup downloaded speech + AI models", job.get("state") == "done" and s.get("ready"), job.get("step", ""))
    ask(api, "online")

    # ---- airplane mode: the real offline test ----
    adb("shell", "cmd", "connectivity", "airplane-mode", "enable")
    time.sleep(8)
    ping = adb("shell", "ping", "-c", "2", "-W", "3", "8.8.8.8", timeout=30)
    dns = adb("shell", "ping", "-c", "1", "-W", "3", "huggingface.co", timeout=30)
    check("airplane mode really cut the internet", " 0% packet loss" not in ping and " 0% packet loss" not in dns,
          (ping.strip().splitlines() or ["?"])[-1][:100])
    for _ in range(20):
        st, s = api.json("GET", "/api/state")
        if str(s.get("network", {}).get("state")).upper() == "AIRPLANE":
            break
        time.sleep(1)
    check("app shows AIRPLANE MODE", str(s.get("network", {}).get("state")).upper() == "AIRPLANE", s.get("network", {}).get("detail", ""))
    ask(api, "AIRPLANE MODE")
    for _ in range(10):
        seen = screen_text()
        if "AIRPLANE" in seen.upper():
            break
        time.sleep(1)
    screenshot("2-airplane-mode")
    adb("shell", "cmd", "connectivity", "airplane-mode", "disable")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Android emulator test\n\n| Check | Result | Detail |\n|---|---|---|\n")
            for name, ok, detail in results:
                f.write(f"| {name} | {'✅' if ok else '❌'} | {detail.replace('|', '/')[:150]} |\n")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "logcat.txt").write_text(adb("logcat", "-d", "-t", "2000"), encoding="utf-8")
    print("\nALL ANDROID CHECKS PASSED" if not failed else f"\nFAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
