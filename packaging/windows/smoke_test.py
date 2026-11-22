"""End-to-end test of an INSTALLED VoiceAssist on a real Windows machine (GitHub Actions runner).

Run with the installed embedded Python:
    %LOCALAPPDATA%\\Programs\\VoiceAssist\\python\\python.exe packaging\\windows\\smoke_test.py

1. starts the installed engine        2. signs up an account through the API
3. one-click offline setup: downloads + silently installs Ollama, pulls a small model (real Windows path)
4. typed question -> local answer + spoken audio
5. spoken question (WAV) -> Windows Whisper -> local answer
6. the offline self-test with all network access blocked
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "VoiceAssist"
DATA = Path(os.environ["LOCALAPPDATA"]) / "VoiceAssist"
MODEL = os.environ.get("SMOKE_MODEL", "qwen2.5:0.5b")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    return ok


class Api:
    def __init__(self, url: str):
        self.base = url.split("#")[0].rstrip("/")
        self.token = url.split("token=")[-1]
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
        st, b = self.call("POST", "/api/turn", timeout=600, **kw)
        return [json.loads(l) for l in b.decode("utf-8").splitlines() if l.strip()]


def main() -> int:
    py = APP / "python" / "python.exe"
    check("installed files present", py.exists() and (APP / "app" / "voiceassist").is_dir() and (APP / "voiceassist.ico").exists(), str(APP))
    check("speech models installed", (DATA / "models" / "stt").is_dir() and (DATA / "models" / "tts").is_dir())
    imp = subprocess.run([str(py), "-c", "import sherpa_onnx, numpy, keyring, psutil, voiceassist, platform; "
                          "print(sherpa_onnx.__version__, platform.machine(), voiceassist.__version__)"],
                         capture_output=True, text=True, cwd=APP / "app")
    check("embedded Python loads all packages", imp.returncode == 0, (imp.stdout or imp.stderr).strip()[-300:])

    (DATA / "last_url.txt").unlink(missing_ok=True)
    proc = subprocess.Popen([str(py), "-m", "voiceassist", "--no-window", "--port", "8765"], cwd=APP / "app",
                            stdout=open(DATA.parent / "va-smoke.log", "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            if (DATA / "last_url.txt").exists():
                break
            time.sleep(0.5)
        time.sleep(3)
        api = Api((DATA / "last_url.txt").read_text().strip())
        st, v = api.json("GET", "/api/version")
        check("engine started", st == 200, json.dumps(v))
        st, r = api.json("POST", "/api/auth/signup", {"username": "smoketest", "password": "smoke-test-123"})
        check("account sign-up", st == 201 and "recovery_code" in r, str(st))
        st, s = api.json("GET", "/api/state")
        check("network detected", st == 200, f"{s['network']['state']} — {s['network']['detail']}")

        st, job = api.json("POST", "/api/setup/start", {"model": MODEL})
        check("one-click offline setup started", st == 200, job.get("step", ""))
        t0, last = time.time(), ""
        while time.time() - t0 < 30 * 60:
            st, s = api.json("GET", "/api/setup/status")
            job = s["job"]
            if job.get("step") != last:
                last = job.get("step", "")
                print(f"   [{job.get('state')}] {last}", flush=True)
            if job.get("state") in ("done", "error"):
                break
            time.sleep(3)
        check("offline setup installed Ollama + model", job.get("state") == "done" and s["ready"], job.get("step", ""))

        evs = api.turn(body={"text": "What is the capital of France? Answer in one short sentence."})
        answer = "".join(e["text"] for e in evs if e["type"] == "token")
        start = next((e for e in evs if e["type"] == "llm_start"), {})
        check("typed question answered by the local model", "paris" in answer.lower() and start.get("kind") == "local", answer[:120])
        check("answer spoken (audio produced)", any(e["type"] == "audio" for e in evs))

        st, wav = api.call("POST", "/api/tts/test", {"text": "What is the capital of Germany?"})
        evs = api.turn(raw=wav, ctype="audio/wav")
        heard = next((e["text"] for e in evs if e["type"] == "transcript"), "")
        answer = "".join(e["text"] for e in evs if e["type"] == "token")
        check("spoken question recognised by Whisper on Windows", "germany" in heard.lower(), heard)
        check("spoken question answered", bool(answer.strip()), answer[:120])
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()

    diag = subprocess.run([str(py), "-m", "voiceassist.diagnostics"], capture_output=True, text=True, cwd=APP / "app", timeout=600)
    check("offline self-test with network blocked", diag.returncode == 0, diag.stdout.strip().splitlines()[-1] if diag.stdout.strip() else diag.stderr[-300:])

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Windows install test ({os.environ.get('PROCESSOR_ARCHITECTURE', '?')})\n\n| Check | Result | Detail |\n|---|---|---|\n")
            for name, ok, detail in results:
                f.write(f"| {name} | {'✅' if ok else '❌'} | {detail.replace('|', '/')[:150]} |\n")
    failed = [n for n, ok, _ in results if not ok]
    print("\nALL WINDOWS CHECKS PASSED" if not failed else f"\nFAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
