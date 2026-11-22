"""UI test of the account system through the real sign-in page (no network needed).

First run -> Sign up tab -> validation -> account created -> recovery code dialog -> app opens ->
chat -> log out -> wrong password rejected -> log in -> history is back -> second account sees
nothing of the first -> forgot password with the recovery code -> account settings.

    python tests/e2e_accounts.py --url http://127.0.0.1:8765/#token=...
"""
from __future__ import annotations

import argparse
import sys
import time

from playwright.sync_api import sync_playwright

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--shots", default="/tmp")
    a = ap.parse_args()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=a.chrome, headless=True, args=["--no-sandbox"])
        ctx = b.new_context(bypass_csp=True, viewport={"width": 1240, "height": 820})
        page = ctx.new_page()
        trail = []
        page.on("console", lambda m: trail.append("console: " + m.text))
        page.on("response", lambda r: trail.append(f"{r.status} {r.request.method} {r.url.split('/api/')[-1][:60]}") if "/api/" in r.url else None)
        page.on("framenavigated", lambda f: trail.append("NAVIGATED " + f.url.split('#')[0]) if f == page.main_frame else None)
        try:
            return run(page, a, b)
        except Exception:
            page.screenshot(path=f"{a.shots}/acct_FAILED.png")
            print("---- last browser events ----\n" + "\n".join(trail[-25:]))
            raise


def run(page, a, b):
    if True:
        page.goto(a.url)
        page.wait_for_selector("#auth-view:not([hidden])", timeout=15000)
        check("sign-in page shown before the app", page.is_hidden("#app"))
        check("first run opens on the Sign up section", page.get_attribute("#tab-signup", "aria-selected") == "true",
              page.inner_text("#signup-note"))
        page.screenshot(path=f"{a.shots}/acct_signup.png")

        # --- validation
        f = "#form-signup"
        page.fill(f + " [name=username]", "mohit")
        page.fill(f + " [name=password]", "secret-pass-1")
        page.fill(f + " [name=confirm]", "different-pass-1")
        page.click(f + " button[type=submit]")
        check("password mismatch caught", "don't match" in page.inner_text(f + " .auth-error"))
        page.fill(f + " [name=password]", "short")
        page.fill(f + " [name=confirm]", "short")
        page.click(f + " button[type=submit]")
        page.wait_for_function("document.querySelector('#form-signup .auth-error').textContent.includes('8 characters')", timeout=5000)
        check("weak password rejected by the server", True, page.inner_text(f + " .auth-error"))

        # --- sign up
        page.fill(f + " [name=display_name]", "Mohit Sharma")
        page.fill(f + " [name=password]", "secret-pass-1")
        page.fill(f + " [name=confirm]", "secret-pass-1")
        check("password strength meter reacts", page.get_attribute(f + " .pw-meter", "data-score") in ("2", "3", "4"))
        page.click(f + " button[type=submit]")
        page.wait_for_selector("#recovery-dialog[open]", timeout=10000)
        code = page.inner_text("#rc-code").strip()
        check("recovery code shown once after sign-up", len(code) == 24, code)
        check("can't continue before confirming it's saved", page.is_disabled("#rc-continue"))
        page.screenshot(path=f"{a.shots}/acct_recovery.png")
        page.check("#rc-saved")
        page.click("#rc-continue")
        page.wait_for_selector("#app:not([hidden])", timeout=10000)
        check("app opens after sign-up", page.inner_text("#user-name") == "Mohit Sharma", page.inner_text("#user-handle"))

        # --- use the app, then log out
        page.fill("#text-in", "Say hello in three words.")
        page.press("#text-in", "Enter")
        page.wait_for_function("[...document.querySelectorAll('.msg-row.assistant .meta')].some(m => /voice in|\\ds/.test(m.textContent))", timeout=90000)
        check("chat works while signed in", True, page.inner_text(".msg-row.assistant .bubble"))
        page.click("#user-chip")
        page.screenshot(path=f"{a.shots}/acct_menu.png")
        page.click("#menu-logout")
        page.wait_for_selector("#auth-view:not([hidden])", timeout=10000)
        check("log out returns to the sign-in page (Log in section)", page.get_attribute("#tab-login", "aria-selected") == "true")
        page.screenshot(path=f"{a.shots}/acct_login.png")

        # --- log in
        page.fill("#form-login [name=username]", "mohit")
        page.fill("#form-login [name=password]", "wrong-pass-9")
        page.click("#form-login button[type=submit]")
        page.wait_for_function("document.querySelector('#form-login .auth-error').textContent.length > 0", timeout=5000)
        check("wrong password rejected", "Incorrect" in page.inner_text("#form-login .auth-error"))
        page.fill("#form-login [name=password]", "secret-pass-1")
        page.click("#form-login button[type=submit]")
        page.wait_for_selector("#app:not([hidden])", timeout=10000)
        page.wait_for_function("document.querySelectorAll('#conv-list li[data-id]').length > 0", timeout=10000)
        check("history is back after logging in", "Say hello" in page.inner_text("#conv-list"))

        # --- stays signed in after a restart of the window (remember me)
        page.reload()
        page.wait_for_selector("#app:not([hidden])", timeout=10000)
        check("still signed in after reopening the window", page.inner_text("#user-name") == "Mohit Sharma")

        # --- second account sees nothing of the first
        page.click("#user-chip"); page.click("#menu-logout")
        page.wait_for_selector("#auth-view:not([hidden])", timeout=10000)
        page.click("#tab-signup")
        page.fill(f + " [name=username]", "guest")
        page.fill(f + " [name=password]", "guest-pass-22")
        page.fill(f + " [name=confirm]", "guest-pass-22")
        page.click(f + " button[type=submit]")
        page.wait_for_selector("#recovery-dialog[open]"); page.check("#rc-saved"); page.click("#rc-continue")
        page.wait_for_selector("#app:not([hidden])", timeout=10000)
        time.sleep(1)
        n = page.evaluate("document.querySelectorAll('#conv-list li[data-id]').length")
        check("second account has its own empty history", n == 0, f"{n} conversation(s)")

        # --- forgot password with recovery code
        page.click("#user-chip"); page.click("#menu-logout")
        page.wait_for_selector("#auth-view:not([hidden])", timeout=10000)
        page.click("#link-forgot")
        r = "#form-recover"
        page.fill(r + " [name=username]", "mohit")
        page.fill(r + " [name=code]", code.lower())
        page.fill(r + " [name=new_password]", "fresh-pass-77")
        page.fill(r + " [name=confirm]", "fresh-pass-77")
        page.click(r + " button[type=submit]")
        page.wait_for_selector("#recovery-dialog[open]", timeout=10000)
        new_code = page.inner_text("#rc-code").strip()
        check("password reset with recovery code issues a NEW code", new_code != code, new_code)
        page.check("#rc-saved"); page.click("#rc-continue")
        page.wait_for_selector("#app:not([hidden])", timeout=10000)
        check("signed in after reset", page.inner_text("#user-handle") == "@mohit")

        # --- account settings tab
        page.click("#user-chip"); page.click("#menu-account")
        page.wait_for_selector("[data-panel=account].active", timeout=5000)
        page.fill("#form-profile [name=display_name]", "Mohit S.")
        page.click("#form-profile button[type=submit]")
        page.wait_for_function("document.querySelector('#user-name').textContent === 'Mohit S.'", timeout=5000)
        check("profile name updated", True)
        page.fill("#form-password [name=current_password]", "fresh-pass-77")
        page.fill("#form-password [name=new_password]", "fresher-pass-88")
        page.fill("#form-password [name=confirm]", "fresher-pass-88")
        page.click("#form-password button[type=submit]")
        page.wait_for_function("document.querySelector('#form-password .acct-msg').textContent.length > 0", timeout=5000)
        check("password changed from settings", "changed" in page.inner_text("#form-password .acct-msg"))
        page.screenshot(path=f"{a.shots}/acct_settings.png")
        b.close()
    print("\nALL ACCOUNT UI CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
