"""Shared helpers for the browser UI tests."""


def ensure_signed_in(page, email="tester@example.com", password="test-pass-123", display_name="Test User"):
    """If the sign-in page is showing, create (or log into) a VoiceAssist cloud account so the app opens.

    The test cloud (tests/fake_supabase.py) signs new accounts in straight away unless it was started with
    confirm=True; then the signup shows "check your email" and the caller confirms and logs in.
    """
    page.wait_for_function("!document.querySelector('#auth-view').hidden || !document.querySelector('#app').hidden", timeout=15000)
    if page.is_hidden("#auth-view"):
        return
    page.click("#tab-signup")
    f = "#form-signup"
    page.fill(f + " [name=display_name]", display_name)
    page.fill(f + " [name=email]", email)
    page.fill(f + " [name=password]", password)
    page.fill(f + " [name=confirm]", password)
    page.click(f + " button[type=submit]")
    page.wait_for_function("!document.querySelector('#app').hidden || document.querySelector('#form-signup .auth-error').textContent"
                           " || document.querySelector('#form-login .auth-error').textContent", timeout=15000)
    if page.is_visible("#app"):
        return
    log_in(page, email, password)


def log_in(page, email, password):
    page.click("#tab-login")
    page.fill("#form-login [name=email]", email)
    page.fill("#form-login [name=password]", password)
    page.click("#form-login button[type=submit]")
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
