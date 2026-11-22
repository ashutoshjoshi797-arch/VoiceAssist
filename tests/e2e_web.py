"""End-to-end test of the WEBSITE edition (online only), in a real Chromium with a fake microphone.

The cloud providers are faked at the network layer (Playwright routes api.anthropic.com, api.openai.com and
generativelanguage.googleapis.com), using their real streaming formats, so no API key or credit is needed.

    python packaging/web/build_site.py /tmp/site
    python tests/e2e_web.py --site /tmp/site --wav /tmp/mic_france.wav
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import struct
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from ui_helpers import ensure_signed_in  # noqa: E402


def sse(events):
    return "".join(f"event: {e.get('type', 'message')}\ndata: {json.dumps(e)}\n\n" for e in events)


def anthropic_stream(text):
    ev = [{"type": "message_start", "message": {"id": "m1", "role": "assistant", "content": []}},
          {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
    for w in text.split(" "):
        ev.append({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w + " "}})
    ev += [{"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
    return sse(ev)


def tone_wav(seconds=0.6, rate=16000):
    import math
    n = int(seconds * rate)
    data = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(n))
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--shots", default="/tmp")
    a = ap.parse_args()

    # serve the site under a sub-path, like GitHub Pages does (https://<user>.github.io/voiceassist/)
    root = Path("/tmp/web-root"); (root).mkdir(exist_ok=True)
    link = root / "voiceassist"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path(a.site).resolve())
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/voiceassist/"

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)

    seen = {"anthropic": [], "openai_stt": 0, "openai_tts": 0, "gemini": 0}

    def on_anthropic(route):
        req = route.request
        seen["anthropic"].append({"headers": req.headers, "body": json.loads(req.post_data or "{}")})
        if req.headers.get("x-api-key") != "sk-ant-test":
            return route.fulfill(status=401, content_type="application/json", body=json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
        last = seen["anthropic"][-1]["body"]["messages"][-1]["content"].lower()
        text = "The capital of France is Paris." if "france" in last else "Hello there, friend!"
        route.fulfill(status=200, headers={"content-type": "text/event-stream", "access-control-allow-origin": "*"}, body=anthropic_stream(text))

    def on_openai(route):
        req = route.request
        if req.url.endswith("/audio/transcriptions"):
            seen["openai_stt"] += 1
            return route.fulfill(status=200, content_type="application/json", headers={"access-control-allow-origin": "*"}, body=json.dumps({"text": "What is the capital of France?"}))
        if req.url.endswith("/audio/speech"):
            seen["openai_tts"] += 1
            return route.fulfill(status=200, headers={"content-type": "audio/mpeg", "access-control-allow-origin": "*"}, body=tone_wav())
        route.fulfill(status=404, body="{}")

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=a.chrome, headless=True, args=[
            "--autoplay-policy=no-user-gesture-required", "--no-sandbox", "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={a.wav}"])
        ctx = browser.new_context(viewport={"width": 1240, "height": 860})
        ctx.route("https://api.anthropic.com/**", on_anthropic)
        ctx.route("https://api.openai.com/**", on_openai)
        ctx.route("https://generativelanguage.googleapis.com/**", lambda r: (seen.__setitem__("gemini", seen["gemini"] + 1), r.fulfill(status=500, body="{}")))
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)

        page.wait_for_selector("#auth-view:not([hidden])", timeout=15000)
        check("website opens on the sign-in page", True, page.inner_text("#auth-view h2") if page.query_selector("#auth-view h2") else "")
        intro = page.inner_text("#auth-view")
        check("wording says 'this browser', not 'this computer'", "this browser" in intro and "this computer" not in intro)
        ensure_signed_in(page)
        page.wait_for_function("document.querySelector('#net-label').textContent !== 'CHECKING'", timeout=15000)
        check("signed up and the app opened", page.is_visible("#app"))
        check("network indicator ONLINE", page.inner_text("#net-label") == "ONLINE", page.inner_text("#net-label"))
        check("no offline setup offered", not page.evaluate("document.querySelector('#setup-dialog').open"))
        check("no Auto / Local-only switch", page.is_hidden("#mode-seg"))
        models = page.evaluate("[...document.querySelectorAll('#model-select optgroup')].map(g => g.label)")
        check("model list has only cloud models", models and all(m.startswith("☁") for m in models), str(models))
        page.click("#btn-settings")
        tabs = page.evaluate("[...document.querySelectorAll('[data-tab]')].filter(b => b.offsetParent).map(b => b.textContent)")
        check("settings have no Local AI / offline self-test", "Local AI" not in tabs and "Diagnostics" not in tabs, str(tabs))
        page.click("#btn-close-settings") if page.query_selector("#btn-close-settings") else page.keyboard.press("Escape")

        # without a key: a clear message
        page.fill("#text-in", "Hello")
        page.press("#text-in", "Enter")
        page.wait_for_selector(".msg-row.assistant .bubble.error", timeout=15000)
        check("without an API key: clear message", "API key" in page.inner_text(".msg-row.assistant .bubble.error"), page.inner_text(".msg-row.assistant .bubble.error").replace("\n", " | "))

        # add keys through Settings -> Online AI
        page.evaluate("openSettings('online')")
        page.fill(".key-row[data-provider=anthropic] input", "sk-ant-test")
        page.click(".key-row[data-provider=anthropic] button")
        page.wait_for_function("document.querySelector('.key-row[data-provider=anthropic] em').classList.contains('ok')", timeout=10000)
        check("Claude key saved (in this browser)", True, page.inner_text(".key-row[data-provider=anthropic] em"))
        page.fill(".key-row[data-provider=openai] input", "sk-openai-test")
        page.click(".key-row[data-provider=openai] button")
        page.wait_for_function("document.querySelector('.key-row[data-provider=openai] em').classList.contains('ok')", timeout=10000)
        page.evaluate("openSettings('speech')")
        page.select_option("select[data-setting='web.tts']", "openai")
        page.wait_for_timeout(500)
        page.keyboard.press("Escape")
        page.evaluate("document.querySelector('.drawer')?.classList.remove('open')")

        # typed question
        page.fill("#text-in", "What is the capital of France?")
        page.press("#text-in", "Enter")
        page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .bubble')].some(b => /Paris/.test(b.textContent) && !b.classList.contains('cursor'))", timeout=30000)
        check("typed question answered by Claude (streamed)", True, page.evaluate("[...document.querySelectorAll('.msg-row.assistant .bubble')].at(-1).textContent"))
        h = seen["anthropic"][-1]["headers"] if seen["anthropic"] else {}
        check("browser calls Anthropic directly with the right headers", h.get("anthropic-dangerous-direct-browser-access") == "true" and h.get("anthropic-version") == "2023-06-01")
        page.wait_for_function("window.__vaStats.chunksPlayed > 0", timeout=20000)
        check("answer spoken (OpenAI voice)", seen["openai_tts"] > 0, f"{seen['openai_tts']} speech request(s)")

        # spoken question (fake microphone) -> OpenAI transcription -> Claude
        n0 = page.evaluate("document.querySelectorAll('.msg-row.assistant').length")
        if page.is_hidden("#voice-panel"):
            page.click("#btn-voice-toggle")
        page.click("#btn-mic")
        page.wait_for_function(f"document.querySelectorAll('.msg-row.user').length >= 3 && [...document.querySelectorAll('.msg-row.assistant .bubble')].length > {n0} && /Paris/.test([...document.querySelectorAll('.msg-row.assistant .bubble')].at(-1).textContent)", timeout=60000)
        heard = page.evaluate("[...document.querySelectorAll('.msg-row.user .bubble')].at(-1).textContent")
        check("spoken question recognised and answered", "france" in heard.lower() and seen["openai_stt"] > 0, heard)
        page.click("#btn-mic")
        page.screenshot(path=f"{a.shots}/web_online.png")

        # internet off: the website does NOT answer offline, it says to reconnect
        ctx.set_offline(True)
        page.wait_for_function("document.querySelector('#net-label').textContent === 'OFFLINE'", timeout=10000)
        check("internet off -> indicator OFFLINE", True, page.inner_text("#route-label"))
        before = len(seen["anthropic"])
        page.fill("#text-in", "Are you there?")
        page.press("#text-in", "Enter")
        page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .bubble.error')].some(b => /offline/i.test(b.textContent))", timeout=10000)
        check("offline: no answer attempted, asks to reconnect", len(seen["anthropic"]) == before, page.evaluate("[...document.querySelectorAll('.msg-row.assistant .bubble.error')].at(-1).textContent"))
        page.screenshot(path=f"{a.shots}/web_offline.png")
        ctx.set_offline(False)
        page.wait_for_function("document.querySelector('#net-label').textContent === 'ONLINE'", timeout=10000)
        check("back online -> ONLINE", True)

        # reload: still signed in, history kept in this browser
        page.reload()
        page.wait_for_selector("#app:not([hidden])", timeout=15000)
        n = page.evaluate("document.querySelectorAll('#conv-list li[data-id]').length")
        check("after reload: still signed in, history kept", n >= 1, f"{n} conversation(s)")
        check("no JavaScript errors", not errors, "; ".join(errors)[:300])
        browser.close()
    srv.shutdown()
    failed = [n for n, ok in results if not ok]
    print("\nALL WEBSITE CHECKS PASSED" if not failed else f"\nFAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
