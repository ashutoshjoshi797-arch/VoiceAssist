"""Opens the desktop window.

Order of preference:
  1. Microsoft Edge in app mode (built into Windows 10/11, native on ARM64) — a frameless app
     window with its own private profile, so mic/speaker permissions are remembered.
  2. Google Chrome / Chromium / Brave in app mode.
  3. pywebview, if installed (`pip install pywebview`).
  4. The default browser.

The page itself never loads anything from the internet (strict Content-Security-Policy).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)


def _candidates() -> list[str]:
    c: list[str] = []
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        for rel in rels:
            for root in roots:
                if root:
                    p = Path(root) / rel
                    if p.exists():
                        c.append(str(p))
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome", "Chromium", "Brave Browser"):
            p = Path("/Applications") / f"{app}.app" / "Contents" / "MacOS" / app
            if p.exists():
                c.append(str(p))
    for name in ("microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser"):
        p = shutil.which(name)
        if p:
            c.append(p)
    return c


APP_FLAGS = [
    "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-background-networking",
    "--disable-component-update", "--disable-domain-reliability", "--disable-client-side-phishing-detection",
    "--disable-features=Translate,OptimizationHints,MediaRouter,msEdgeShoppingUI,msImplicitSignin",
    "--autoplay-policy=no-user-gesture-required", "--window-size=1180,820",
]


def open_window(url: str, prefer: str = "auto"):
    """Returns a Popen for the window process (or None if we fell back to a normal browser tab)."""
    if prefer in ("auto", "app"):
        for exe in _candidates():
            try:
                args = [exe, f"--app={url}", f"--user-data-dir={paths.browser_profile_dir()}", *APP_FLAGS]
                log.info("opening window with %s", Path(exe).name)
                return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                log.warning("could not start %s: %s", exe, e)
    if prefer in ("auto", "webview"):
        try:
            import webview  # type: ignore  # noqa: F401
            log.info("opening window with pywebview")
            code = f"import webview; webview.create_window('VoiceAssist', {url!r}, width=1180, height=820); webview.start()"
            return subprocess.Popen([sys.executable, "-c", code])
        except ImportError:
            pass
    log.info("opening default browser")
    webbrowser.open(url)
    return None
