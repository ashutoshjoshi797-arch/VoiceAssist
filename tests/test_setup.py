"""One-click offline setup tests (fake Ollama server; no downloads from the internet).

    python -m unittest tests.test_setup -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voiceassist import paths  # noqa: E402

os.environ.setdefault("VOICEASSIST_MODELS", str(paths.models_dir()))
os.environ["VOICEASSIST_HOME"] = tempfile.mkdtemp(prefix="va-setup-")

from voiceassist import setup_wizard  # noqa: E402
from voiceassist.config import Settings  # noqa: E402
from voiceassist.connectivity import ConnectionManager  # noqa: E402
from voiceassist.engine import Engine, EventBus  # noqa: E402
from voiceassist.history import HistoryStore  # noqa: E402


class FakeOllama:
    """Speaks the parts of Ollama's API the setup uses: version, tags, pull (streamed progress), generate."""

    def __init__(self, fail_pull=False):
        self.models = []
        self.fail_pull = fail_pull
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _json(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._json({"version": "0.0-fake"})
                if self.path == "/api/tags":
                    return self._json({"models": [{"name": m} for m in fake.models]})
                self.send_response(404); self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/pull":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    w = lambda o: (self.wfile.write((json.dumps(o) + "\n").encode()), self.wfile.flush())
                    w({"status": "pulling manifest"})
                    if fake.fail_pull:
                        w({"error": "pull model manifest: file does not exist"})
                        return
                    total = 50_000_000
                    for done in range(0, total + 1, 10_000_000):
                        w({"status": "pulling abc", "digest": "sha256:abc", "total": total, "completed": done})
                        time.sleep(0.15)
                    w({"status": "verifying sha256 digest"})
                    w({"status": "success"})
                    fake.models.append(body["model"])
                    return
                if self.path == "/api/generate":
                    return self._json({"done": True})
                self.send_response(404); self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


class TestSetup(unittest.TestCase):
    def make(self, fake):
        s = Settings(Path(os.environ["VOICEASSIST_HOME"]) / f"s{time.time()}.json")
        s.update({"local": {"ollama_url": f"http://127.0.0.1:{fake.port}", "model": "llama3.2:1b", "preload": False}})
        bus = EventBus()
        eng = Engine(s, ConnectionManager(s), HistoryStore(None), bus)
        return s, bus, setup_wizard.LocalAISetup(s, eng, bus)

    def wait(self, setup, timeout=30):
        t0 = time.time()
        while setup.job["state"] not in ("done", "error") and time.time() - t0 < timeout:
            time.sleep(0.1)
        return setup.job

    def test_status_before_and_after(self):
        fake = FakeOllama()
        s, bus, setup = self.make(fake)
        st = setup.status()
        self.assertTrue(st["engine_running"])
        self.assertFalse(st["model_installed"])
        self.assertFalse(st["ready"])
        self.assertIn(st["recommended"], ("llama3.2:1b", "llama3.2:3b"))
        q = bus.subscribe()
        setup.start("qwen2.5:0.5b")
        job = self.wait(setup)
        self.assertEqual(job["state"], "done", job)
        progress = []
        while not q.empty():
            ev = q.get()
            if ev["type"] == "setup":
                progress.append(ev.get("progress", 0))
        self.assertGreater(len(progress), 3, "progress events were published")
        self.assertEqual(progress, sorted(progress), "progress only moves forward")
        self.assertEqual(s.get("local.model"), "qwen2.5:0.5b", "the downloaded model becomes the local model")
        self.assertTrue(setup.status()["model_installed"])
        fake.stop()

    def test_pull_error_is_reported(self):
        fake = FakeOllama(fail_pull=True)
        _, _, setup = self.make(fake)
        setup.start("does-not-exist:1b")
        job = self.wait(setup)
        self.assertEqual(job["state"], "error")
        self.assertIn("file does not exist", job["step"])
        fake.stop()

    def test_strict_offline_refuses(self):
        fake = FakeOllama()
        s, _, setup = self.make(fake)
        s.update({"strict_offline": True})
        with self.assertRaises(setup_wizard.SetupError):
            setup.start()
        fake.stop()

    @unittest.skipIf(sys.platform == "win32", "on Windows the engine is installed automatically")
    def test_engine_missing_on_non_windows_gives_instructions(self):
        s = Settings(Path(os.environ["VOICEASSIST_HOME"]) / "nx.json")
        s.update({"local": {"ollama_url": "http://127.0.0.1:9", "preload": False}})
        bus = EventBus()
        setup = setup_wizard.LocalAISetup(s, Engine(s, ConnectionManager(s), HistoryStore(None), bus), bus)
        old = setup_wizard.find_ollama
        setup_wizard.find_ollama = lambda: None
        try:
            setup.start()
            job = self.wait(setup)
        finally:
            setup_wizard.find_ollama = old
        self.assertEqual(job["state"], "error")
        self.assertIn("ollama.com/download", job["step"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
