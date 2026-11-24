"""Local HTTP server that the desktop window talks to. Binds to 127.0.0.1 only.

Security: every /api call must carry the per-launch random token (X-App-Token header, or ?token=
for EventSource/beacon), and the Host header must be the loopback address — this blocks other
websites in your browser from talking to the assistant (CSRF / DNS-rebinding).
"""
from __future__ import annotations

import json
import logging
import mimetypes
import queue
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from http.cookies import SimpleCookie

from . import __version__, models_catalog, paths
from .accounts import AccountError
from .cloud import CloudAccounts
from .llm import RECOMMENDED_MODELS, LLMError, build_local
from .setup_wizard import LocalAISetup, SetupError
from .stt import STTError

log = logging.getLogger(__name__)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; font-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


class App:
    """Glue object holding all services; passed to the request handler."""

    def __init__(self, settings, conn, history, engine, bus, accounts: CloudAccounts | None = None):
        self.settings, self.conn, self.history, self.engine, self.bus = settings, conn, history, engine, bus
        self.accounts = accounts or CloudAccounts()
        engine.cloud_accounts = self.accounts
        conn.cloud_ready = lambda: self.accounts.configured
        self.setup = LocalAISetup(settings, engine, bus)
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.last_ping = time.time()
        self.bye_at: float | None = None
        self.shutdown_event = threading.Event()

    def state(self, user: dict | None = None) -> dict:
        route, reason = self.conn.route()
        try:
            local_models = build_local(self.settings).list_models()
            local_running = True
        except Exception:
            local_models, local_running = [], False
        return {
            "version": __version__,
            "settings": self.settings.snapshot(),
            "network": self.conn.snapshot(),
            "route": {"route": route, "reason": reason},
            "cloud": {"configured": self.accounts.configured, "label": "VoiceAssist cloud AI"},
            "stt": self.engine.stt.status(),
            "tts": self.engine.tts.status(),
            "local": {**self.engine.local_status, "running": local_running, "installed": local_models,
                      "recommended": RECOMMENDED_MODELS},
            "catalog": {"stt": models_catalog.STT_MODELS, "voices": models_catalog.TTS_VOICES},
            "data_dir": str(paths.data_dir()),
            "models_dir": str(paths.models_dir()),
            "user": user,
            "platform": "desktop",
        }


SESSION_COOKIE = "va_session"
# Reachable without being logged in (the sign-in page and window lifecycle only).
PUBLIC_API = {"/api/version", "/api/quit", "/api/auth/me", "/api/auth/signup", "/api/auth/login", "/api/auth/logout", "/api/auth/recover",
              "/api/ping", "/api/bye"}


class Handler(BaseHTTPRequestHandler):
    server_version = "VoiceAssist"
    protocol_version = "HTTP/1.0"  # streamed responses end when the connection closes
    app: App = None  # type: ignore[assignment]
    user: dict | None = None
    _set_cookie: str | None = None

    def log_message(self, fmt, *args):  # quiet; never log request bodies
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}")

    def _token_ok(self, query: dict) -> bool:
        tok = self.headers.get("X-App-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(tok, self.app.token)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self._set_cookie:
            self.send_header("Set-Cookie", self._set_cookie)
            self._set_cookie = None
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _err(self, code: int, message: str, hint: str = "") -> None:
        self._json({"error": message, "hint": hint}, code)

    # -- sessions -----------------------------------------------------------
    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
            m = c.get(SESSION_COOKIE)
            return m.value if m else None
        except Exception:
            return None

    def _start_session(self, user: dict, remember: bool) -> None:
        token, max_age = self.app.accounts.create_session(user["id"], remember)
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        if max_age:
            cookie += f"; Max-Age={max_age}"
        self._set_cookie = cookie

    def _clear_session_cookie(self) -> None:
        self._set_cookie = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    def _body(self, limit: int = 50 * 1024 * 1024) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("request too large")
        return self.rfile.read(n) if n else b""

    def _json_body(self) -> dict:
        raw = self._body(2 * 1024 * 1024)
        return json.loads(raw.decode("utf-8") or "{}") if raw else {}

    # ------------------------------------------------------------------ dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._err(403, "forbidden host")
        u = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(u.query)
        path = u.path
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else self._err(405, "method not allowed")
        if not self._token_ok(query):
            return self._err(401, "missing or invalid app token")
        self._set_cookie = None
        self.user = self.app.accounts.session_user(self._session_token())
        if self.user is None and path not in PUBLIC_API:
            return self._json({"error": "Please log in.", "auth": "required"}, 401)
        try:
            route = ROUTES_EXACT.get((method, path))
            if route:
                return route(self, query)
            for (m, rx), fn in ROUTES_RE:
                mt = rx.fullmatch(path)
                if m == method and mt:
                    return fn(self, query, *mt.groups())
            return self._err(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except AccountError as e:
            self._json({"error": str(e), "field": e.field}, e.status)
        except SetupError as e:
            self._err(400, str(e))
        except LLMError as e:
            self._err(502, str(e), e.hint)
        except ValueError as e:
            self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("API error on %s %s", method, path)
            try:
                self._err(500, f"internal error: {e}")
            except Exception:
                pass

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (paths.WEB_DIR / rel).resolve()
        if paths.WEB_DIR.resolve() not in target.parents and target != paths.WEB_DIR.resolve() or not target.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        extra = {"Content-Security-Policy": CSP} if target.suffix == ".html" else {}
        self._send(200, target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""), extra)

    # ------------------------------------------------------------------ API handlers
    def api_state(self, q):
        self._json(self.app.state(self.user))

    def api_setup_status(self, q):
        self._json(self.app.setup.status())

    def api_setup_start(self, q):
        self._json(self.app.setup.start(self._json_body().get("model")))

    def api_setup_cancel(self, q):
        self.app.setup.cancel()
        self._json({"ok": True})

    def api_version(self, q):
        self._json({"version": __version__})

    # ------------------------------------------------------------------ accounts
    def api_auth_me(self, q):
        self._json({"user": self.user, "has_accounts": True, "platform": "desktop", "cloud": self.app.accounts.configured})

    def _signed_in(self, user: dict, remember: bool, status: int = 200) -> None:
        claimed = 0
        if not self.app.accounts.db.get("legacy_claimed"):   # first cloud account on this computer gets its old chats
            claimed = self.app.history.claim_orphans(user["id"])
            self.app.accounts.db["legacy_claimed"] = user["id"]
            self.app.accounts._save()
        self._start_session(user, remember)
        self._json({"user": user, "claimed_conversations": claimed}, status)

    def api_auth_signup(self, q):
        b = self._json_body()
        user, confirm = self.app.accounts.signup(b.get("email", ""), b.get("password", ""), b.get("display_name", ""))
        if confirm:
            return self._json({"needs_confirmation": True, "email": b.get("email", "").strip()}, 202)
        log.info("cloud account created")
        self._signed_in(user, bool(b.get("remember", True)), 201)

    def api_auth_login(self, q):
        b = self._json_body()
        user = self.app.accounts.login(b.get("email", ""), b.get("password", ""))
        self._signed_in(user, bool(b.get("remember", True)))

    def api_auth_logout(self, q):
        self.app.accounts.end_session(self._session_token())
        self.app.engine.cancel()
        self._clear_session_cookie()
        self._json({"ok": True})

    def api_auth_recover(self, q):
        self.app.accounts.request_password_reset(self._json_body().get("email", ""))
        self._json({"ok": True})

    def api_cloud_usage(self, q):
        try:
            self._json(self.app.accounts.usage(self.user["id"]))
        except Exception as e:  # noqa: BLE001
            self._err(503, str(e))

    def api_account_profile(self, q):
        b = self._json_body()
        self._json({"user": self.app.accounts.update_profile(self.user["id"], b.get("display_name"), b.get("email"))})

    def api_account_password(self, q):
        b = self._json_body()
        self.app.accounts.change_password(self.user["id"], b.get("current_password", ""), b.get("new_password", ""),
                                          keep_token=self._session_token())
        self._json({"ok": True})

    def api_account_delete(self, q):
        b = self._json_body()
        uid = self.user["id"]
        self.app.accounts.delete(uid, b.get("password", ""))
        self.app.history.clear(uid)
        self.app.engine.cancel()
        self._clear_session_cookie()
        log.info("account deleted (id %s)", uid)
        self._json({"ok": True})

    def api_events(self, q):
        sub = self.app.bus.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.app.shutdown_event.is_set():
                try:
                    ev = sub.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(sub)

    def api_turn(self, q):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        conv = (q.get("conversation_id") or [None])[0]
        if ctype == "application/json":
            body = self._json_body()
            turn = self.app.engine.start_turn(body.get("conversation_id") or conv, text=str(body.get("text", "")),
                                              user_id=self.user["id"])
        else:
            turn = self.app.engine.start_turn(conv, wav=self._body(), user_id=self.user["id"])
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Turn-Id", turn.id)
        self.end_headers()
        try:
            self.wfile.write((json.dumps({"type": "accepted", "turn_id": turn.id}) + "\n").encode())
            self.wfile.flush()
            for ev in turn.iter_events():
                self.wfile.write((json.dumps(ev) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            turn.cancel.set()  # the window went away: stop working on it

    def api_transcribe(self, q):
        try:
            self._json(self.app.engine.transcribe_only(self._body(), self.user["id"]))
        except STTError as e:
            self._err(400, str(e), e.hint)
        except Exception as e:
            self._err(400, f"Could not read the recorded audio: {e}")

    def api_cancel(self, q):
        body = self._json_body()
        self._json({"cancelled": self.app.engine.cancel(body.get("turn_id"))})

    def api_audio(self, q, aid):
        wav = self.app.engine.audio.pop(aid)
        if wav is None:
            return self._err(404, "audio expired")
        self._send(200, wav, "audio/wav")

    def api_settings_get(self, q):
        self._json(self.app.settings.snapshot())

    def api_settings_post(self, q):
        patch = self._json_body()
        new = self.app.settings.update(patch)
        self.app.bus.publish({"type": "settings", "settings": new}, sticky=False)
        self._json({"ok": True, "settings": new})

    def api_switch_back(self, q):
        self.app.conn.accept_switch_back(bool(self._json_body().get("accept")))
        self._json({"ok": True, "route": dict(zip(("route", "reason"), self.app.conn.route()))})

    def api_recheck(self, q):
        self._json(self.app.conn.check_now())

    def api_conversations(self, q):
        self._json({"conversations": self.app.history.list(user_id=self.user["id"]),
                    "persistent": self.app.history.persistent})

    def api_conversation_new(self, q):
        self._json({"id": self.app.history.create(user_id=self.user["id"])})

    def api_conversation_get(self, q, cid):
        if not self.app.history.exists(cid, self.user["id"]):
            return self._err(404, "conversation not found")
        self._json({"id": cid, "messages": self.app.history.messages(cid)})

    def api_conversation_update(self, q, cid):
        if not self.app.history.exists(cid, self.user["id"]):
            return self._err(404, "conversation not found")
        b = self._json_body()
        try:
            c = self.app.history.update(cid, title=b.get("title"), pinned=b.get("pinned"), archived=b.get("archived"),
                                        unread=b.get("unread"))
        except ValueError as e:
            return self._err(400, str(e))
        self._json({"ok": True, "conversation": c})

    def api_conversation_delete(self, q, cid):
        if not self.app.history.exists(cid, self.user["id"]):
            return self._err(404, "conversation not found")
        self.app.history.delete(cid)
        self._json({"ok": True})

    def api_history_clear(self, q):
        self.app.history.clear(self.user["id"])  # only this account's history
        self._json({"ok": True})

    def api_local_models(self, q):
        try:
            installed = build_local(self.app.settings).list_models()
            running = True
        except LLMError as e:
            installed, running = [], False
            return self._json({"running": False, "installed": [], "error": str(e), "hint": e.hint,
                               "recommended": RECOMMENDED_MODELS})
        self._json({"running": running, "installed": installed, "recommended": RECOMMENDED_MODELS})

    def api_local_preload(self, q):
        threading.Thread(target=self.app.engine.preload_local, daemon=True).start()
        self._json({"ok": True})

    def api_voices(self, q):
        self._json({"piper": models_catalog.installed_voices(), "system": self.app.engine.tts.system.voices()
                    if self.app.engine.tts.system.available() else [], "catalog": models_catalog.TTS_VOICES})

    def api_tts_test(self, q):
        from . import audio as _audio
        text = self._json_body().get("text") or "Hello! This is how I sound."
        eng = self.app.engine.tts.local_engine()
        if eng is None:
            return self._err(400, "No voice engine available.", "Click \"Set up offline AI\" (Settings → Local AI).")
        samples, sr = eng.synth(text)
        self._send(200, _audio.float32_to_wav(samples, sr), "audio/wav")

    def api_selftest(self, q):
        from .diagnostics import run_selftest
        self._json(run_selftest(self.app.settings, self.app.engine))

    def api_ping(self, q):
        self.app.last_ping = time.time()
        self.app.bye_at = None
        self._json({"ok": True})

    def api_bye(self, q):
        self.app.bye_at = time.time()
        self._json({"ok": True})

    def api_quit(self, q):
        self._json({"ok": True})
        threading.Thread(target=lambda: (time.sleep(0.3), self.app.shutdown_event.set()), daemon=True).start()


ROUTES_EXACT = {
    ("GET", "/api/state"): Handler.api_state,
    ("GET", "/api/version"): Handler.api_version,
    ("GET", "/api/setup/status"): Handler.api_setup_status,
    ("POST", "/api/setup/start"): Handler.api_setup_start,
    ("POST", "/api/setup/cancel"): Handler.api_setup_cancel,
    ("GET", "/api/auth/me"): Handler.api_auth_me,
    ("POST", "/api/auth/signup"): Handler.api_auth_signup,
    ("POST", "/api/auth/login"): Handler.api_auth_login,
    ("POST", "/api/auth/logout"): Handler.api_auth_logout,
    ("POST", "/api/auth/recover"): Handler.api_auth_recover,
    ("POST", "/api/account/profile"): Handler.api_account_profile,
    ("POST", "/api/account/password"): Handler.api_account_password,
    ("GET", "/api/cloud/usage"): Handler.api_cloud_usage,
    ("POST", "/api/account/delete"): Handler.api_account_delete,
    ("GET", "/api/events"): Handler.api_events,
    ("POST", "/api/turn"): Handler.api_turn,
    ("POST", "/api/transcribe"): Handler.api_transcribe,
    ("POST", "/api/cancel"): Handler.api_cancel,
    ("GET", "/api/settings"): Handler.api_settings_get,
    ("POST", "/api/settings"): Handler.api_settings_post,
    ("POST", "/api/switch_back"): Handler.api_switch_back,
    ("POST", "/api/network/recheck"): Handler.api_recheck,
    ("GET", "/api/conversations"): Handler.api_conversations,
    ("POST", "/api/conversations"): Handler.api_conversation_new,
    ("POST", "/api/history/clear"): Handler.api_history_clear,
    ("GET", "/api/local/models"): Handler.api_local_models,
    ("POST", "/api/local/preload"): Handler.api_local_preload,
    ("GET", "/api/voices"): Handler.api_voices,
    ("POST", "/api/tts/test"): Handler.api_tts_test,
    ("POST", "/api/selftest"): Handler.api_selftest,
    ("POST", "/api/ping"): Handler.api_ping,
    ("POST", "/api/bye"): Handler.api_bye,
    ("POST", "/api/quit"): Handler.api_quit,
}
ROUTES_RE = [
    (("GET", re.compile(r"/api/audio/([0-9a-f]{32})")), Handler.api_audio),
    (("GET", re.compile(r"/api/conversations/([0-9a-f]{12})")), Handler.api_conversation_get),
    (("DELETE", re.compile(r"/api/conversations/([0-9a-f]{12})")), Handler.api_conversation_delete),
    (("POST", re.compile(r"/api/conversations/([0-9a-f]{12})")), Handler.api_conversation_update),
]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Linux/macOS: reuse the port right after a restart. Windows: SO_REUSEADDR would allow port hijacking.
    allow_reuse_address = sys.platform != "win32"


def start_server(app: App, port: int = 0) -> Server:
    Handler.app = app
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError:
        srv = Server(("127.0.0.1", 0), Handler)  # requested port busy: pick a free one
    app.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    log.info("UI server on http://127.0.0.1:%d", app.port)
    return srv
