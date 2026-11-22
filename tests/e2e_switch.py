"""UI test of automatic online/offline switching, using a fake cloud provider that we can 'unplug'.

ONLINE (cloud answers) -> cloud unreachable (UI flips to OFFLINE, local model answers, same
conversation) -> cloud back (UI OFFERS to switch back; user accepts) -> cloud answers again.

    python tests/e2e_switch.py            (needs Ollama + speech models, like test_backend.py)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.fake_cloud import FakeCloud  # noqa: E402
from tests.ui_helpers import ensure_signed_in  # noqa: E402
from voiceassist import paths  # noqa: E402

CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")


def main() -> int:
    fake = FakeCloud().start()
    port = fake.port
    home = tempfile.mkdtemp(prefix="va-switch-")
    model = os.environ.get("VOICEASSIST_TEST_LOCAL_MODEL", "qwen2.5:0.5b")
    Path(home, "settings.json").write_text('{"local": {"model": "%s"}}' % model)
    env = {**os.environ, "VOICEASSIST_HOME": home, "VOICEASSIST_MODELS": os.environ.get("VOICEASSIST_MODELS", str(paths.models_dir())),
           "VOICEASSIST_ANTHROPIC_BASE": f"http://127.0.0.1:{port}", "VOICEASSIST_PROBE_HOSTS": f"127.0.0.1:{port}",
           "ANTHROPIC_API_KEY": "test-key"}
    app = subprocess.Popen([sys.executable, "-m", "voiceassist", "--no-window", "--port", "8791"], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)

    try:
        url_file = Path(home, "last_url.txt")
        for _ in range(60):
            if url_file.exists():
                break
            time.sleep(0.5)
        url = url_file.read_text()
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
            page = b.new_context(bypass_csp=True, viewport={"width": 1240, "height": 860}).new_page()
            page.goto(url)
            ensure_signed_in(page)

            def ask(text):
                n = page.evaluate("document.querySelectorAll('.msg-row.assistant .meta').length")
                page.fill("#text-in", text)
                page.press("#text-in", "Enter")
                page.wait_for_function(f"[...document.querySelectorAll('.msg-row.assistant .meta')].length > {n} && /voice in|\\ds/.test([...document.querySelectorAll('.msg-row.assistant .meta')].at(-1).textContent)", timeout=90000)
                return page.eval_on_selector_all(".msg-row.assistant", "els => [els.at(-1).querySelector('.bubble').textContent, els.at(-1).querySelector('.meta').textContent]")

            page.wait_for_function("document.querySelector('#net-label').textContent === 'ONLINE'", timeout=15000)
            check("starts ONLINE", True, page.inner_text("#route-label"))
            text, meta = ask("Hello, who are you?")
            check("online answer comes from the cloud provider", "cloud model" in text and "☁" in meta, f"{text!r} [{meta}]")

            fake.stop()  # unplug the internet
            page.wait_for_function("document.querySelector('#net-label').textContent === 'OFFLINE'", timeout=20000)
            check("indicator switches to OFFLINE automatically", True, page.inner_text("#route-label"))
            text, meta = ask("What is two plus two?")
            check("offline answer comes from the local model", "on this PC" in meta, f"{text!r} [{meta}]")

            fake2 = FakeCloud(port).start()  # internet returns
            page.wait_for_selector("#banner-switchback", timeout=25000)
            check("offers to switch back when the internet returns", True, page.inner_text("#banner-switchback .msg"))
            check("does not switch back without asking", "on this PC" in page.inner_text("#route-label"), page.inner_text("#route-label"))
            page.screenshot(path="/tmp/e2e-switchback.png")
            page.click("#banner-switchback button.primary")
            page.wait_for_function("/cloud/.test(document.querySelector('#route-label').textContent)", timeout=10000)
            text, meta = ask("Thanks!")
            check("cloud answers again after switching back", "cloud model" in text and "☁" in meta, f"{text!r} [{meta}]")
            sent = fake2.requests[-1]["body"]["messages"]
            joined = " ".join(m["content"] for m in sent)
            check("conversation kept across all switches", "Hello, who are you?" in joined and "two plus two" in joined,
                  f"{len(sent)} messages sent to cloud")
            n = page.evaluate("document.querySelectorAll('#conv-list li[data-id]').length")
            check("still one conversation", n == 1, f"{n}")
            fake2.stop()
            b.close()
    finally:
        app.terminate()
        fake.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
