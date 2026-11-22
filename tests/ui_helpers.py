"""Shared helpers for the browser UI tests."""


def ensure_signed_in(page, username="tester", password="test-pass-123", display_name="Test User"):
    """If the sign-in page is showing, create (or log into) an account so the app opens."""
    page.wait_for_function("!document.querySelector('#auth-view').hidden || !document.querySelector('#app').hidden", timeout=15000)
    if page.is_hidden("#auth-view"):
        return
    page.click("#tab-signup")
    f = "#form-signup"
    page.fill(f + " [name=display_name]", display_name)
    page.fill(f + " [name=username]", username)
    page.fill(f + " [name=password]", password)
    page.fill(f + " [name=confirm]", password)
    page.click(f + " button[type=submit]")
    page.wait_for_function("document.querySelector('#recovery-dialog').open || document.querySelector('#form-signup .auth-error').textContent", timeout=15000)
    if page.evaluate("document.querySelector('#recovery-dialog').open"):
        page.check("#rc-saved")
        page.click("#rc-continue")
    else:  # account already exists: log in instead
        page.click("#tab-login")
        page.fill("#form-login [name=username]", username)
        page.fill("#form-login [name=password]", password)
        page.click("#form-login button[type=submit]")
    page.wait_for_selector("#app:not([hidden])", timeout=15000)
