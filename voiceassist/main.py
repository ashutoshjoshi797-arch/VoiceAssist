"""Entry point:  python -m voiceassist  [--no-window] [--port 8765] [--window app|webview|browser]"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time

from . import __version__, netutil, paths
from .config import Settings
from .connectivity import ONLINE, ConnectionManager
from .engine import Engine, EventBus
from .history import HistoryStore
from .server import App, start_server

WINDOW_GONE_S = 30 * 60
log = logging.getLogger("voiceassist")


def setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir() / "voiceassist.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:  # pythonw.exe (no console) has no stdout
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def existing_instance_url() -> str | None:
    """If the SAME version of VoiceAssist is already running, return its URL so we just open another window.

    An older/different version still running (e.g. after an update) is asked to quit, so the new code starts.
    """
    import json
    import urllib.request

    f = paths.data_dir() / "last_url.txt"
    if not f.exists():
        return None
    url = f.read_text(encoding="utf-8").strip()
    base, token = url.split("#")[0].rstrip("/"), url.split("token=")[-1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(method, path, timeout=2.0):
        req = urllib.request.Request(base + path, method=method, headers={"X-App-Token": token},
                                     data=b"{}" if method == "POST" else None)
        if method == "POST":
            req.add_header("Content-Type", "application/json")
        with opener.open(req, timeout=timeout) as r:
            return r.read()

    try:
        if b"VoiceAssist" not in call("GET", "/")[:4000]:
            return None
    except Exception:
        return None  # nothing running
    try:
        running = json.loads(call("GET", "/api/version")).get("version")
    except Exception:
        running = None  # versions before 1.1 have no /api/version
    if running == __version__:
        return url
    log.info("an older VoiceAssist engine (%s) is still running — stopping it", running or "1.0.x")
    try:
        call("POST", "/api/quit")
    except Exception as e:
        log.warning("could not ask the old engine to quit: %s", e)
    for _ in range(40):  # wait up to 10 s for it to release the port
        time.sleep(0.25)
        try:
            call("GET", "/", timeout=0.5)
        except Exception:
            time.sleep(0.5)
            return None
    log.warning("old engine did not stop; starting on another port")
    return None


_instance_lock = None


def acquire_instance_lock() -> bool:
    """One engine per user: an OS-level lock on a file in the data folder, held while the engine runs.

    Clicking the shortcut several times quickly used to start several engines at once (each checked for a running
    one before any had started); the extra ones then shut down and left windows showing "Failed to fetch"."""
    global _instance_lock
    f = open(paths.data_dir() / "engine.lock", "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _instance_lock = f
    return True


def wait_for_running_instance(timeout: float = 30) -> str | None:
    """Another launch holds the lock and is starting up: wait until it answers, then use it."""
    end = time.time() + timeout
    while time.time() < end:
        url = existing_instance_url()
        if url:
            return url
        if acquire_instance_lock():   # the other launch went away without starting
            return None
        time.sleep(0.5)
    return None


def build_app(settings: Settings | None = None) -> App:
    settings = settings or Settings()
    netutil.set_socket_guard(bool(settings.get("strict_offline")))
    bus = EventBus()
    conn = ConnectionManager(settings)
    history = HistoryStore(str(paths.history_db_path()) if settings.get("privacy.save_history", True) else None)
    engine = Engine(settings, conn, history, bus)
    app = App(settings, conn, history, engine, bus)

    def on_network(kind: str, snap: dict) -> None:
        route, reason = conn.route()
        bus.publish({"type": "network", "kind": kind, **snap, "route": route, "route_reason": reason})
        if kind == "switch_back_offer":
            bus.publish({"type": "switch_back_offer", "provider": settings.get("online.provider")}, sticky=False)
        if snap["state"] != ONLINE and engine.local_status.get("state") not in ("ready", "loading"):
            threading.Thread(target=engine.preload_local, daemon=True).start()

    conn.on_change(on_network)

    def on_settings(changed: set, new: dict) -> None:
        if "strict_offline" in changed:
            netutil.set_socket_guard(bool(new.get("strict_offline")))
        if changed & {"online", "mode", "strict_offline"}:
            conn.poke()
            on_network("state", conn.snapshot())
        if "local" in changed:
            threading.Thread(target=engine.preload_local, daemon=True).start()
        if changed & {"stt", "tts"}:
            engine.warmup()

    settings.on_change(on_settings)
    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="voiceassist", description="Desktop AI voice assistant (online + offline)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-window", action="store_true", help="only start the server (open the URL yourself)")
    ap.add_argument("--window", choices=["auto", "app", "webview", "browser"], default="auto")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    setup_logging(a.verbose)
    running = None
    if not acquire_instance_lock():
        running = wait_for_running_instance()
        if not running and _instance_lock is None:
            log.error("another VoiceAssist engine is starting but did not answer — try again in a few seconds")
            return 1
    else:
        running = existing_instance_url()   # a pre-1.2 engine (no lock) may still be running
    if running and a.no_window:
        log.info("VoiceAssist is already running: %s", running.split("#")[0])
        return 0
    if running:
        log.info("VoiceAssist is already running — opening a window for it")
        from .window import open_window
        open_window(running, a.window)
        return 0
    log.info("VoiceAssist %s starting; data folder %s", __version__, paths.data_dir())
    app = build_app()
    app.conn.check_now()  # know ONLINE/OFFLINE/AIRPLANE before the window opens
    app.conn.start()
    app.engine.warmup()
    srv = start_server(app, a.port)
    url = f"http://127.0.0.1:{app.port}/#token={app.token}"
    if sys.stdout is not None:
        print(f"\nVoiceAssist is running: {url}\n", flush=True)
    (paths.data_dir() / "last_url.txt").write_text(url, encoding="utf-8")

    proc = None
    started = time.time()
    if not a.no_window:
        from .window import open_window
        proc = open_window(url, {"auto": "auto", "app": "app", "webview": "webview", "browser": "browser"}[a.window])

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # Time this computer has been awake since the window last pinged. Counted in small steps so a sleep / lid-close
    # (when Windows pauses both the engine and the window) never looks like a missing window on wake-up.
    awake_without_ping, seen_ping, tick = 0.0, app.last_ping, time.time()
    try:
        while not stop.is_set() and not app.shutdown_event.is_set():
            time.sleep(0.5)
            now = time.time()
            step, tick = now - tick, now
            if app.last_ping != seen_ping:
                seen_ping, awake_without_ping = app.last_ping, 0.0
            elif 0 < step < 5:
                awake_without_ping += step
            if proc is not None and proc.poll() is not None:
                if time.time() - started < 8:
                    # the browser handed the window to an already-running browser process and exited at once:
                    # its exit says nothing about the window, so rely on the window's beacons/pings instead
                    proc = None
                else:
                    log.info("window closed")
                    break
            # Fallback for browsers that hand the window to an existing process:
            if app.bye_at and time.time() - app.bye_at > 8 and app.last_ping < app.bye_at:   # other windows answer within 8 s
                log.info("window closed (beacon)")
                break
            # Last resort (a crashed window): no ping for WINDOW_GONE_S of awake time. Pings come every 20 s, but a
            # minimised or background window may be throttled or frozen for minutes, so this is deliberately long.
            if not a.no_window and now - started > 180 and awake_without_ping > WINDOW_GONE_S:
                log.info("no window connected for %d minutes — shutting down", WINDOW_GONE_S // 60)
                break
    finally:
        app.shutdown_event.set()
        app.conn.stop()
        app.engine.cancel()
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
