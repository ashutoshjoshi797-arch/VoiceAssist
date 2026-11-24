"""End-to-end test of the WEBSITE edition (online only), in a real Chromium with a fake microphone.

The VoiceAssist cloud (Supabase Auth + the shared "chat" function) is faked by tests/fake_supabase.py, which
speaks the same REST and streaming formats, so no Supabase project, AI key or credit is needed. The site is built
pointing at it:

    VOICEASSIST_SUPABASE_URL=http://127.0.0.1:54329 VOICEASSIST_SUPABASE_KEY=test-anon-key \
        python packaging/web/build_site.py /tmp/site
    python tests/e2e_web.py --site /tmp/site --wav /tmp/mic_france.wav --cloud-port 54329
"""
from __future__ import annotations

import argparse
import functools
import http.server
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from fake_supabase import FakeSupabase  # noqa: E402
from ui_helpers import ensure_signed_in, log_in  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--shots", default="/tmp")
    ap.add_argument("--cloud-port", type=int, default=54329)
    a = ap.parse_args()
    cloud = FakeSupabase(a.cloud_port, limit=4).start()

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

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=a.chrome, headless=True, args=[
            "--autoplay-policy=no-user-gesture-required", "--no-sandbox", "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={a.wav}"])
        ctx = browser.new_context(viewport={"width": 1240, "height": 860})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)

        page.wait_for_selector("#auth-view:not([hidden])", timeout=15000)
        check("website opens on the sign-in page", True, page.inner_text("#auth-view h2") if page.query_selector("#auth-view h2") else "")
        intro = page.inner_text("#auth-view")
        check("sign-in page: one cloud account, no 'this computer' wording", "this computer" not in intro and "website" in intro, intro[:120].replace("\n", " | "))
        ensure_signed_in(page)
        page.wait_for_function("document.querySelector('#net-label').textContent !== 'CHECKING'", timeout=15000)
        check("signed up and the app opened", page.is_visible("#app"))
        check("network indicator ONLINE", page.inner_text("#net-label") == "ONLINE", page.inner_text("#net-label"))
        check("no offline setup offered", not page.evaluate("document.querySelector('#setup-dialog').open"))
        check("no Auto / Local-only switch", page.is_hidden("#mode-seg"))
        models = page.evaluate("[...document.querySelectorAll('#model-select option')].map(o => o.textContent)")
        check("one shared cloud AI, nothing to choose or pay for", len(models) == 1, str(models))
        page.click("#btn-settings")
        tabs = page.evaluate("[...document.querySelectorAll('[data-tab]')].filter(b => b.offsetParent).map(b => b.textContent)")
        check("settings have no Local AI / offline self-test", "Local AI" not in tabs and "Diagnostics" not in tabs, str(tabs))
        page.evaluate("openSettings('online')")
        page.wait_for_function("/of 4/.test(document.querySelector('#cloud-info').textContent)", timeout=10000)
        info = page.inner_text("#cloud-info")
        check("Cloud AI tab: no API key fields, shows today's questions", not page.query_selector(".key-row") and "4" in info, info.replace("\n", " | "))
        page.keyboard.press("Escape")
        page.evaluate("document.querySelector('.drawer')?.classList.remove('open')")

        # typed question -> the shared VoiceAssist cloud function (streamed), spoken with the browser voice
        page.fill("#text-in", "What is the capital of France?")
        page.press("#text-in", "Enter")
        page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .bubble')].some(b => /Paris/.test(b.textContent) && !b.classList.contains('cursor'))", timeout=30000)
        check("typed question answered by the VoiceAssist cloud AI (streamed)", True, page.evaluate("[...document.querySelectorAll('.msg-row.assistant .bubble')].at(-1).textContent"))
        body = cloud.chat_bodies[-1] if cloud.chat_bodies else {}
        check("request carries only the conversation — no AI key", set(body) <= {"system", "messages", "max_tokens"} and body.get("messages"), str(sorted(body)))
        check("cloud calls are signed with the user's session", any(p == "/functions/v1/chat" for _, p in cloud.requests))

        # spoken question (fake microphone) -> cloud transcription -> words wait in the box -> user sends
        n0 = page.evaluate("document.querySelectorAll('.msg-row.assistant').length")
        if page.is_hidden("#voice-panel"):
            page.click("#btn-voice-toggle")
        page.click("#btn-mic")
        page.wait_for_function("!document.querySelector('#review-hint').hidden", timeout=60000)
        box = page.input_value("#text-in")
        page.screenshot(path=f"{a.shots}/web_review.png")
        check("check before sending: what was heard waits in the message box", "france" in box.lower()
              and page.evaluate("document.querySelectorAll('.msg-row.assistant').length") == n0, box)
        page.press("#text-in", "Enter")
        page.wait_for_function(f"[...document.querySelectorAll('.msg-row.assistant .bubble')].length > {n0} && /Paris/.test([...document.querySelectorAll('.msg-row.assistant .bubble')].at(-1).textContent)", timeout=60000)
        heard = page.evaluate("[...document.querySelectorAll('.msg-row.user .bubble')].at(-1).textContent")
        check("spoken question recognised by the cloud, checked, sent and answered", "france" in heard.lower(), heard)
        page.screenshot(path=f"{a.shots}/web_online.png")

        # the daily limit (4 in this test): two more questions, then a friendly "limit reached"
        for q in ("What is the capital of Germany?", "What is 2 plus 2?"):
            page.fill("#text-in", q); page.press("#text-in", "Enter")
            page.wait_for_function("!document.querySelector('.bubble.cursor')", timeout=30000)
            page.wait_for_timeout(300)
        page.fill("#text-in", "One more?"); page.press("#text-in", "Enter")
        page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .bubble.error')].some(b => /questions for today/i.test(b.textContent))", timeout=15000)
        check("daily question limit enforced by the cloud", True, page.evaluate("[...document.querySelectorAll('.msg-row.assistant .bubble.error')].at(-1).textContent").replace("\n", " | "))
        cloud.limit = 50

        # internet off: the website does NOT answer offline, it says to reconnect
        ctx.set_offline(True)
        page.wait_for_function("document.querySelector('#net-label').textContent === 'OFFLINE'", timeout=10000)
        check("internet off -> indicator OFFLINE", True, page.inner_text("#route-label"))
        before = len(cloud.chat_bodies)
        page.fill("#text-in", "Are you there?")
        page.press("#text-in", "Enter")
        page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .bubble.error')].some(b => /offline/i.test(b.textContent))", timeout=10000)
        check("offline: no answer attempted, asks to reconnect", len(cloud.chat_bodies) == before, page.evaluate("[...document.querySelectorAll('.msg-row.assistant .bubble.error')].at(-1).textContent"))
        page.screenshot(path=f"{a.shots}/web_offline.png")
        ctx.set_offline(False)
        page.wait_for_function("document.querySelector('#net-label').textContent === 'ONLINE'", timeout=10000)
        check("back online -> ONLINE", True)

        # reload: still signed in, history kept in this browser
        page.reload()
        page.wait_for_selector("#app:not([hidden])", timeout=15000)
        n = page.evaluate("document.querySelectorAll('#conv-list li[data-id]').length")
        check("after reload: still signed in, history kept", n >= 1, f"{n} conversation(s)")
        # log out and back in: the account lives in the VoiceAssist cloud
        page.evaluate("document.querySelector('#btn-logout')?.click()")
        page.wait_for_selector("#auth-view:not([hidden])", timeout=15000)
        page.fill("#form-login [name=email]", "tester@example.com")
        page.fill("#form-login [name=password]", "wrong-pass-1")
        page.click("#form-login button[type=submit]")
        page.wait_for_function("document.querySelector('#form-login .auth-error').textContent", timeout=10000)
        check("wrong password: clear message", "Incorrect" in page.inner_text("#form-login .auth-error"), page.inner_text("#form-login .auth-error"))
        log_in(page, "tester@example.com", "test-pass-123")
        check("logged back in with the cloud account", page.is_visible("#app"))

        # forgot password: email sent, the link from the email opens "choose a new password"
        page.evaluate("document.querySelector('#btn-logout')?.click()")
        page.wait_for_selector("#auth-view:not([hidden])", timeout=15000)
        page.click("#link-forgot")
        page.fill("#form-recover [name=email]", "tester@example.com")
        page.click("#form-recover button[type=submit]")
        page.wait_for_function("document.querySelector('#form-recover .auth-ok').textContent", timeout=10000)
        check("forgot password: reset email requested", cloud.recover_emails[-1:] == ["tester@example.com"])
        sess = cloud.session(cloud.users["tester@example.com"])
        page.goto("about:blank")
        page.goto(url + f"#access_token={sess['access_token']}&refresh_token={sess['refresh_token']}&expires_in=3600&type=recovery")
        page.wait_for_selector("#form-newpw:not([hidden])", timeout=15000)
        page.fill("#form-newpw [name=new_password]", "new-pass-456")
        page.fill("#form-newpw [name=confirm]", "new-pass-456")
        page.click("#form-newpw button[type=submit]")
        page.wait_for_selector("#app:not([hidden])", timeout=15000)
        check("reset link: new password saved and signed in", cloud.users["tester@example.com"]["password"] == "new-pass-456" and "#" not in page.url)
        check("no JavaScript errors", not errors, "; ".join(errors)[:300])
        browser.close()
    srv.shutdown()
    cloud.stop()
    failed = [n for n, ok in results if not ok]
    print("\nALL WEBSITE CHECKS PASSED" if not failed else f"\nFAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
