"""End-to-end UI test: a real Chromium window with a FAKE MICROPHONE fed from a WAV file.

It drives the actual desktop UI exactly like a user: presses the mic button (hands-free mode),
speaks (the WAV), and checks that the transcript appears, the AI answers, and the answer is
played back as audio. Scenarios:

  basic      — one question, expect an answer spoken aloud, then a follow-up in the same conversation
  bargein    — ask for a long story, then talk over the assistant; expect the story to be
               interrupted and the new question answered

Usage:
  python tests/e2e_ui.py --url http://127.0.0.1:8765/#token=... --wav q.wav --scenario basic \
         --expect-network OFFLINE --expect-heard "capital of france" --expect-answer paris
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from tests.ui_helpers import ensure_signed_in  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--scenario", default="basic", choices=["basic", "review", "bargein", "ptt", "mute", "denied", "nomic"])
    ap.add_argument("--expect-network", default=None)
    ap.add_argument("--expect-heard", default="")
    ap.add_argument("--expect-answer", default="")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--screenshot", default="")
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()

    results = {"scenario": a.scenario, "checks": []}

    def check(name, ok, detail=""):
        results["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
        return ok

    with sync_playwright() as p:
        args = ["--autoplay-policy=no-user-gesture-required", "--no-sandbox"]
        if a.scenario != "denied":
            args.append("--use-fake-ui-for-media-stream")  # auto-accept the permission prompt
        if a.scenario != "nomic":
            args += ["--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={a.wav}"]
        browser = p.chromium.launch(executable_path=a.chrome, headless=True, args=args)
        ctx = browser.new_context(viewport={"width": 1240, "height": 860}, bypass_csp=True)  # test driver needs eval
        page = ctx.new_page()
        logs = []
        page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: logs.append(f"PAGEERROR: {e}"))
        page.goto(a.url)
        ensure_signed_in(page)
        page.wait_for_function("S.settings && S.settings.ui", timeout=15000)
        if a.scenario != "review":   # the other scenarios test the send-straight-away voice flow
            page.evaluate("saveSettings({ui: {review_voice: false}})")
        page.wait_for_function("document.querySelector('#net-label').textContent !== 'CHECKING'", timeout=15000)
        net = page.inner_text("#net-label")
        if a.expect_network:
            want = "AIRPLANE MODE" if a.expect_network == "AIRPLANE" else a.expect_network
            check("network indicator", net == want, f"shows {net!r}")
        check("route label", True, page.inner_text("#route-label"))
        platform = page.evaluate("S.state && S.state.platform") or "desktop"
        if platform == "desktop" or platform not in ("android", "jvm"):
            check("voice controls hidden by default (desktop)", page.is_hidden("#voice-panel"))
        if a.scenario not in ("ptt",) and page.is_hidden("#voice-panel"):
            page.click("#btn-voice-toggle")  # open the voice controls (button left of Send)
        if a.scenario not in ("ptt",):
            check("voice controls visible", page.is_visible("#btn-mic"))

        t0 = time.time()
        if a.scenario in ("denied", "nomic"):
            page.click("#btn-mic")
            page.wait_for_selector("#banner-mic", timeout=15000)
            msg = page.inner_text("#banner-mic")
            want = "blocked" if a.scenario == "denied" else "No microphone"
            check(f"clear error for {a.scenario} microphone", want in msg, msg.replace("\n", " | "))
            page.fill("#text-in", "Say hello in three words.")
            page.press("#text-in", "Enter")
            page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .meta')].some(m => /voice in|\\ds/.test(m.textContent))", timeout=a.timeout * 1000)
            check("app still usable by typing (answer spoken)", True, page.inner_text(".msg-row.assistant .bubble"))
            if a.screenshot:
                page.screenshot(path=a.screenshot)
            browser.close()
            ok = all(c["ok"] for c in results["checks"])
            return 0 if ok else 1
        if a.scenario == "ptt":
            if page.is_hidden("#voice-panel"):
                page.click("#btn-voice-toggle")
            page.click("#style-seg button[data-style=ptt]")
            page.click("#btn-voice-toggle")  # close it: Space must still work with the panel hidden
            check("voice controls closed again", page.is_hidden("#voice-panel"))
            page.keyboard.down("Space")   # hold to talk (the fake mic starts speaking ~2 s in)
            page.wait_for_function("document.querySelector('#btn-mic').classList.contains('ptt-held')", timeout=10000)
            check("push-to-talk recording while Space is held", True, page.inner_text("#mic-state"))
            time.sleep(5.0)
            page.keyboard.up("Space")     # release to send
            page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .meta')].some(m => /voice in/.test(m.textContent))", timeout=a.timeout * 1000)
            heard = page.inner_text(".msg-row.user .bubble")
            check("push-to-talk speech recognised", any(x in heard.lower() for x in a.expect_heard.lower().split("|")), f"heard {heard!r}")
            check("push-to-talk answered", a.expect_answer.lower() in page.inner_text(".msg-row.assistant .bubble").lower(), page.inner_text(".msg-row.assistant .bubble"))
            time.sleep(6)
            n = page.evaluate("document.querySelectorAll('.msg-row.user').length")
            check("nothing recorded after releasing (no hands-free listening in PTT)", n == 1, f"{n} user message(s)")
            browser.close()
            return 0 if all(c["ok"] for c in results["checks"]) else 1
        page.click("#btn-mic")  # hands-free: start listening
        if a.scenario == "mute":
            page.click("#btn-mute")
            check("mute engaged", "muted" in page.get_attribute("#btn-mute", "class"), page.inner_text("#mic-state"))
            time.sleep(14)  # the fake mic asks its question while muted
            n = page.evaluate("document.querySelectorAll('.msg-row.user').length")
            check("nothing heard while muted", n == 0, f"{n} user message(s)")
            page.click("#btn-mute")
            page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .meta')].some(m => /voice in/.test(m.textContent))", timeout=a.timeout * 1000)
            check("heard and answered after unmuting", a.expect_answer.lower() in page.inner_text(".msg-row.assistant .bubble").lower(), page.inner_text(".msg-row.assistant .bubble"))
            browser.close()
            return 0 if all(c["ok"] for c in results["checks"]) else 1
        page.wait_for_function("document.querySelector('#btn-mic').classList.contains('listening') || document.querySelector('#btn-mic').classList.contains('hearing')", timeout=10000)
        check("microphone started (hands-free)", True, page.inner_text("#mic-state"))

        if a.scenario == "review":
            page.wait_for_function("!document.querySelector('#review-hint').hidden", timeout=a.timeout * 1000)
            box = page.input_value("#text-in")
            n = page.evaluate("document.querySelectorAll('.msg-row.user').length")
            check("check before sending: words wait in the message box, nothing sent", n == 0 and any(x in box.lower() for x in a.expect_heard.lower().split("|")), f"box {box!r}")
            check("listening paused while checking", not page.evaluate("S.session"))
            time.sleep(3)
            check("still not sent after waiting", page.evaluate("document.querySelectorAll('.msg-row.user').length") == 0)
            page.fill("#text-in", box + " Answer in one sentence.")
            page.press("#text-in", "Enter")
            page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .bubble')].some(b => b.textContent.length > 3 && !b.classList.contains('cursor'))", timeout=a.timeout * 1000)
            sent = page.inner_text(".msg-row.user .bubble")
            answer = page.inner_text(".msg-row.assistant .bubble")
            check("the checked (edited) message is what was sent", "one sentence" in sent.lower(), sent)
            check("AI answered", a.expect_answer.lower() in answer.lower(), f"answer {answer!r}")
            check("hint hidden after sending", page.is_hidden("#review-hint"))
        if a.scenario == "basic":
            page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .meta')].some(m => /voice in/.test(m.textContent))", timeout=a.timeout * 1000)
            heard = page.inner_text(".msg-row.user .bubble")
            answer = page.inner_text(".msg-row.assistant .bubble")
            check("speech recognised", any(x in heard.lower() for x in a.expect_heard.lower().split("|")), f"heard {heard!r}")
            check("AI answered", a.expect_answer.lower() in answer.lower(), f"answer {answer!r}")
            check("answer badge", True, page.inner_text(".msg-row.assistant .meta"))
            page.wait_for_function("window.__vaStats.chunksPlayed > 0", timeout=30000)
            st = page.evaluate("window.__vaStats")
            check("answer played as audio", st["chunksPlayed"] > 0, f"{st['chunksPlayed']} chunk(s), {st['secondsPlayed']:.1f}s of speech")
            # The fake mic loops the file, so the same question is asked again: a follow-up turn
            # in the same conversation without restarting anything.
            page.wait_for_function("document.querySelectorAll('.msg-row.assistant .meta').length >= 2 && [...document.querySelectorAll('.msg-row.assistant .meta')].filter(m => /voice in/.test(m.textContent)).length >= 2", timeout=a.timeout * 1000)
            n_conv = page.evaluate("document.querySelectorAll('#conv-list li[data-id]').length")
            check("follow-up answered in the same conversation", n_conv == 1, f"{n_conv} conversation(s) in history")
        else:
            # long answer starts playing ...
            page.wait_for_function("window.__vaStats.chunksPlayed > 0", timeout=a.timeout * 1000)
            check("long answer is being spoken", True)
            # ... then the user talks over it
            page.wait_for_function("[...document.querySelectorAll('.badge.int')].length > 0", timeout=a.timeout * 1000)
            check("assistant was interrupted by voice (barge-in)", True, page.inner_text(".msg-row.assistant .meta"))
            page.wait_for_function("document.querySelectorAll('.msg-row.user').length >= 2 && [...document.querySelectorAll('.msg-row.assistant .meta')].some(m => /voice in/.test(m.textContent))", timeout=a.timeout * 1000)
            users = page.eval_on_selector_all(".msg-row.user .bubble", "els => els.map(e => e.textContent)")
            answers = page.eval_on_selector_all(".msg-row.assistant .bubble", "els => els.map(e => e.textContent)")
            check("new question heard after interruption", any(x in users[-1].lower() for x in a.expect_heard.lower().split("|")), f"heard {users[-1]!r}")
            check("new question answered", a.expect_answer.lower() in answers[-1].lower(), f"answer {answers[-1]!r}")

        if a.screenshot:
            page.screenshot(path=a.screenshot)
        results["elapsed_s"] = round(time.time() - t0, 1)
        errs = [l for l in logs if l.startswith("PAGEERROR") or l.startswith("error")]
        check("no JavaScript errors", not errs, "; ".join(errs)[:400])
        browser.close()
    ok = all(c["ok"] for c in results["checks"])
    print(json.dumps(results, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
