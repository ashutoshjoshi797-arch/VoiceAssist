"""Backend tests: switching, failover, error handling, privacy guard, long conversations.

    python -m unittest tests.test_backend -v

Needs the speech models (python -m voiceassist.setup_models) and Ollama running with at least
one model (set VOICEASSIST_TEST_LOCAL_MODEL, default: the first installed model). Tests that
need Ollama are skipped if it isn't running. The VoiceAssist cloud is simulated by tests/fake_cloud.py.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Use the real models folder, but an isolated settings/history folder.
from voiceassist import paths  # noqa: E402

os.environ.setdefault("VOICEASSIST_MODELS", str(paths.models_dir()))
_TMP = tempfile.mkdtemp(prefix="va-test-")
os.environ["VOICEASSIST_HOME"] = _TMP
for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(var, None)

from tests.fake_cloud import FakeCloud  # noqa: E402
from voiceassist import audio, netutil  # noqa: E402
from voiceassist.cloud import CloudAccounts, VoiceAssistCloudProvider  # noqa: E402
from voiceassist.config import Settings  # noqa: E402
from voiceassist.connectivity import AIRPLANE, OFFLINE, ONLINE, ConnectionManager  # noqa: E402
from voiceassist.engine import Engine, EventBus  # noqa: E402
from voiceassist.history import HistoryStore, build_context  # noqa: E402
from voiceassist.llm import build_local  # noqa: E402
from voiceassist.llm.base import strip_think  # noqa: E402
from voiceassist.llm.cloud import merge_roles  # noqa: E402
from voiceassist.tts import SentenceChunker, speakable  # noqa: E402



def ollama_models() -> list[str]:
    try:
        import json
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except Exception:
        return []


LOCAL_MODELS = ollama_models()
LOCAL_MODEL = os.environ.get("VOICEASSIST_TEST_LOCAL_MODEL") or (LOCAL_MODELS[0] if LOCAL_MODELS else "")
need_ollama = unittest.skipUnless(LOCAL_MODEL, "Ollama with a model is required")


def cloud_accounts(base_url: str) -> CloudAccounts:
    """A signed-in VoiceAssist cloud account (user "u1") pointing at the fake cloud."""
    acc = CloudAccounts(str(Path(_TMP) / f"cloud-{time.time()}.json"), {"url": base_url, "key": "test-anon", "site_url": ""})
    acc.db["users"]["u1"] = {"access": "tok-u1", "refresh": "ref-u1", "expires_at": time.time() + 3600,
                             "user": {"id": "u1", "email": "t@example.com", "username": "t@example.com", "display_name": "T"}}
    return acc


def run_turn(engine, conv=None, text=None, wav=None, timeout=120, user_id="u1"):
    t = engine.start_turn(conv, wav=wav, text=text, user_id=user_id)
    evs = []
    deadline = time.time() + timeout
    for ev in t.iter_events():
        evs.append(ev)
        if time.time() > deadline:
            raise TimeoutError
    return evs, t


def kinds(evs):
    return [e["type"] for e in evs]


def answer_text(evs):
    return "".join(e["text"] for e in evs if e["type"] == "token")


# =========================================================================== pure units
class TestUnits(unittest.TestCase):
    def test_chunker_first_chunk_early_and_abbreviations(self):
        c = SentenceChunker()
        out = []
        for piece in "Sure. Dr. Smith said e.g. apples are 3.5 dollars. That is cheap! Bye".split(" "):
            out += c.feed(piece + " ")
        out += c.flush()
        self.assertEqual(out[0], "Sure. Dr. Smith said e.g. apples are 3.5 dollars.")
        self.assertIn("That is cheap!", " ".join(out))
        self.assertTrue(out[-1].endswith("Bye"))

    def test_chunker_long_runon_is_split(self):
        c = SentenceChunker()
        out = c.feed("word, " * 120) + c.flush()
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(x) <= 260 for x in out))

    def test_speakable_strips_markdown(self):
        s = speakable("## Title\n- **bold** item\n1. `code` [link](http://x.y) 😀")
        self.assertEqual(s, "Title bold item code link")

    def test_strip_think(self):
        chunks = ["Hel", "lo <thi", "nk>secret plan</th", "ink> world"]
        self.assertEqual("".join(strip_think(iter(chunks))), "Hello  world")

    def test_merge_roles_alternates_and_starts_with_user(self):
        m = merge_roles([{"role": "assistant", "content": "hi"}, {"role": "user", "content": "a"},
                         {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}])
        self.assertEqual([x["role"] for x in m], ["user", "assistant"])
        self.assertEqual(m[0]["content"], "a\n\nb")

    def test_build_context_trims_old_messages(self):
        hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 400} for i in range(100)]
        ctx = build_context(hist, max_chars=3000)
        self.assertLess(sum(len(m["content"]) for m in ctx), 3500)
        self.assertIn("message 99", ctx[-1]["content"])
        self.assertIn("omitted", ctx[0]["content"])

    def test_wav_roundtrip(self):
        import numpy as np
        x = (np.sin(np.arange(16000) / 10) * 0.5).astype("float32")
        y, sr = audio.wav_to_float32(audio.float32_to_wav(x, 16000))
        self.assertEqual(sr, 16000)
        self.assertLess(float(abs(x - y).max()), 1e-3)

    def test_history_store(self):
        h = HistoryStore(None)
        c = h.create()
        h.add(c, "user", "What is the weather like on Mars today?")
        h.add(c, "assistant", "Cold.")
        self.assertEqual(len(h.messages(c)), 2)
        self.assertTrue(h.list()[0]["title"].startswith("What is the weather"))
        h.clear()
        self.assertEqual(h.list(), [])

    def test_settings_validation_and_persistence(self):
        s = Settings(Path(_TMP) / "s1.json")
        with self.assertRaises(ValueError):
            s.update({"mode": "cloud-always"})
        s.update({"local": {"model": "abc:1b"}})
        self.assertEqual(Settings(Path(_TMP) / "s1.json").get("local.model"), "abc:1b")
        self.assertNotIn("key", (Path(_TMP) / "s1.json").read_text().lower().replace("keep_alive", ""))


# =========================================================================== privacy guard
class TestPrivacyGuard(unittest.TestCase):
    def tearDown(self):
        netutil.set_socket_guard(False)
        netutil.violations.clear()

    def test_local_engine_must_be_loopback(self):
        with self.assertRaises(netutil.OfflineViolation):
            netutil.open_request("GET", "https://api.example.com/v1/models", local_only=True)
        with self.assertRaises(netutil.OfflineViolation):
            netutil.open_request("GET", "http://192.168.1.20:11434/api/tags", local_only=True)

    def test_socket_guard_blocks_internet_but_not_loopback(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        netutil.set_socket_guard(True)
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises((netutil.OfflineViolation, urllib.error.URLError)) as cm:
            direct.open("https://example.com", timeout=3)
        self.assertIn("offline mode", str(cm.exception))
        with self.assertRaises(netutil.OfflineViolation):  # our own HTTP helper, even via a proxy
            netutil.open_request("GET", "https://api.anthropic.com/v1/models")
        with self.assertRaises(netutil.OfflineViolation):
            socket.create_connection(("1.1.1.1", 443), timeout=2)
        socket.create_connection(srv.getsockname(), timeout=2).close()  # loopback still fine
        self.assertIn("example.com", netutil.violations)
        srv.close()


# =========================================================================== connectivity
class TestConnectivity(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud().start()
        os.environ["VOICEASSIST_PROBE_HOSTS"] = f"127.0.0.1:{self.fake.port}"
        self.s = Settings(Path(_TMP) / f"c-{time.time()}.json")
        self.c = ConnectionManager(self.s)
        self.events = []
        self.c.on_change(lambda k, snap: self.events.append(k))

    def tearDown(self):
        self.fake.stop()
        os.environ.pop("VOICEASSIST_PROBE_HOSTS", None)
        os.environ.pop("VOICEASSIST_RFKILL_DIR", None)

    def test_online_offline_offer_switch_back(self):
        self.assertEqual(self.c.check_now()["state"], ONLINE)
        self.assertEqual(self.c.route()[0], "online")
        port = self.fake.port
        self.fake.stop()
        self.assertEqual(self.c.check_now()["state"], OFFLINE)
        self.assertEqual(self.c.route(), ("local", "No internet"))
        self.fake = FakeCloud(port).start()
        self.assertEqual(self.c.check_now()["state"], ONLINE)
        self.assertIn("switch_back_offer", self.events)
        self.assertEqual(self.c.route()[0], "local", "must ASK before switching back")
        self.c.accept_switch_back(True)
        self.assertEqual(self.c.route()[0], "online")

    def test_auto_switch_back(self):
        self.s.update({"auto_switch_back": True})
        self.c.check_now()
        port = self.fake.port
        self.fake.stop()
        self.c.check_now()
        self.fake = FakeCloud(port).start()
        self.c.check_now()
        self.assertIn("auto_switched_back", self.events)
        self.assertEqual(self.c.route()[0], "online")

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses the Linux rfkill simulation; Windows reads the registry")
    def test_airplane_is_not_just_offline(self):
        d = Path(tempfile.mkdtemp()) / "rfkill0"
        d.mkdir()
        (d / "type").write_text("wlan")
        (d / "soft").write_text("1")
        (d / "hard").write_text("0")
        os.environ["VOICEASSIST_RFKILL_DIR"] = str(d.parent)
        port = self.fake.port
        self.fake.stop()
        self.assertEqual(self.c.check_now()["state"], AIRPLANE)
        (d / "soft").write_text("0")  # radios back on, but still no internet
        self.assertEqual(self.c.check_now()["state"], OFFLINE)
        self.fake = FakeCloud(port).start()

    def test_local_only_mode_and_missing_key(self):
        self.c.check_now()
        self.s.update({"mode": "local"})
        self.assertEqual(self.c.route()[0], "local")
        self.s.update({"mode": "auto"})
        self.c.cloud_ready = lambda: False
        self.assertEqual(self.c.route(), ("local", "This copy isn't connected to the VoiceAssist cloud"))

    def test_strict_offline_never_probes(self):
        self.s.update({"strict_offline": True})
        before = len(self.fake.requests)
        snap = self.c.check_now()
        self.assertEqual(snap["state"], OFFLINE)
        self.assertEqual(self.c.route()[0], "local")
        self.assertEqual(len(self.fake.requests), before)


# =========================================================================== the VoiceAssist cloud AI (wire format)
class TestCloudProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeCloud().start()
        cls.acc = cloud_accounts(f"http://127.0.0.1:{cls.fake.port}")

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def test_stream_headers_and_no_ai_key(self):
        p = VoiceAssistCloudProvider(self.acc, "u1", max_tokens=300)
        out = "".join(p.stream_chat("sys", [{"role": "user", "content": "hi"}], threading.Event()))
        self.assertEqual(out, "This is the cloud model answering. It works fine.")
        req = self.fake.requests[-1]
        self.assertEqual(req["path"], "/functions/v1/chat")
        self.assertEqual(req["headers"]["authorization"], "Bearer tok-u1")   # the user's sign-in, not an AI key
        self.assertEqual(req["headers"]["apikey"], "test-anon")
        self.assertEqual(req["body"]["max_tokens"], 300)
        self.assertEqual(req["body"]["messages"][-1], {"role": "user", "content": "hi"})

    def test_not_signed_in(self):
        from voiceassist.llm import AuthError
        with self.assertRaises(AuthError):
            list(VoiceAssistCloudProvider(self.acc, None).stream_chat("s", [{"role": "user", "content": "x"}], threading.Event()))

# =========================================================================== engine end-to-end
@need_ollama
class TestEngine(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCloud().start()
        os.environ["VOICEASSIST_PROBE_HOSTS"] = f"127.0.0.1:{self.fake.port}"
        self.s = Settings(Path(_TMP) / f"e-{time.time()}.json")
        self.s.update({"local": {"model": LOCAL_MODEL, "preload": False}})
        self.bus = EventBus()
        self.conn = ConnectionManager(self.s)
        self.conn.check_now()
        self.hist = HistoryStore(None)
        self.eng = Engine(self.s, self.conn, self.hist, self.bus)
        self.eng.cloud_accounts = cloud_accounts(f"http://127.0.0.1:{self.fake.port}")

    def tearDown(self):
        self.fake.stop()
        os.environ.pop("VOICEASSIST_PROBE_HOSTS", None)

    def test_online_answer_uses_cloud(self):
        evs, t = run_turn(self.eng, text="Hello there")
        start = next(e for e in evs if e["type"] == "llm_start")
        self.assertEqual(start["kind"], "cloud")
        self.assertEqual(answer_text(evs), "This is the cloud model answering. It works fine.")
        self.assertIn("audio", kinds(evs))
        self.assertEqual(kinds(evs)[-1], "done")

    def test_connection_drop_mid_answer_fails_over_to_local(self):
        self.fake.mode = "drop"
        evs, t = run_turn(self.eng, text="What is the capital of Italy? One word.")
        ks = kinds(evs)
        self.assertIn("failover", ks)
        starts = [e for e in evs if e["type"] == "llm_start"]
        self.assertEqual([s["kind"] for s in starts], ["cloud", "local"])
        self.assertEqual(ks[-1], "done")
        local_answer = answer_text(evs[ks.index("failover"):])
        self.assertTrue(local_answer.strip(), "local model produced the answer after failover")
        self.assertNotIn("cloud model", local_answer)
        self.assertEqual(self.conn.route()[0], "local")  # stays local until the user says otherwise

    def test_stalled_stream_aborts_when_network_detected_down(self):
        self.fake.mode = "hang"
        t = self.eng.start_turn(None, text="Tell me about Paris.", user_id="u1")
        evs, t0 = [], time.time()
        for ev in t.iter_events():
            evs.append(ev)
            if ev["type"] == "token" and len([e for e in evs if e["type"] == "token"]) == 3:
                self.fake.stop()            # the cloud becomes unreachable…
                self.conn.check_now()       # …the connection manager notices (normally within ~8 s)
        ks = kinds(evs)
        self.assertIn("failover", ks)
        self.assertEqual(ks[-1], "done")
        self.assertLess(time.time() - t0, 25, "must not wait for the 60 s stall")

    def test_auth_error_still_answers_locally(self):
        self.fake.mode = "401"
        evs, _ = run_turn(self.eng, text="Say hello.")
        fo = next(e for e in evs if e["type"] == "failover")
        self.assertIn("sign-in", fo["message"])
        self.assertEqual(kinds(evs)[-1], "done")

    def test_history_preserved_across_switching(self):
        evs, _ = run_turn(self.eng, text="My name is Mohit. Remember it.")
        conv = next(e for e in evs if e["type"] == "user_saved")["conversation_id"]
        port = self.fake.port
        self.fake.stop()
        self.conn.check_now()  # internet gone
        evs2, _ = run_turn(self.eng, conv, text="What is my name?")
        self.assertEqual(next(e for e in evs2 if e["type"] == "llm_start")["kind"], "local")
        self.assertIn("mohit", answer_text(evs2).lower())
        self.fake = FakeCloud(port).start()
        self.conn.check_now()
        self.conn.accept_switch_back(True)
        evs3, _ = run_turn(self.eng, conv, text="Thanks!")
        self.assertEqual(next(e for e in evs3 if e["type"] == "llm_start")["kind"], "cloud")
        sent = self.fake.requests[-1]["body"]["messages"]
        joined = " ".join(m["content"] for m in sent)
        self.assertIn("My name is Mohit", joined)
        self.assertIn("What is my name?", joined)  # the offline part of the conversation went along
        self.assertEqual(len(self.hist.messages(conv)), 6)

    def test_voice_turn_offline(self):
        self.fake.stop()
        self.conn.check_now()
        tts = self.eng.tts.local_engine()
        samples, sr = tts.synth("What is the capital of Germany?")
        evs, _ = run_turn(self.eng, wav=audio.float32_to_wav(samples, sr))
        tr = next(e for e in evs if e["type"] == "transcript")
        self.assertIn("germany", tr["text"].lower())
        self.assertIn("berlin", answer_text(evs).lower())
        self.assertIn("audio", kinds(evs))

    def test_silence_is_ignored(self):
        import numpy as np
        noise = (np.random.RandomState(0).randn(16000 * 2) * 0.002).astype("float32")
        evs, _ = run_turn(self.eng, wav=audio.float32_to_wav(noise, 16000))
        self.assertIn("no_speech", kinds(evs))

    def test_interrupt_cancels_and_keeps_partial(self):
        self.fake.stop()
        self.conn.check_now()
        t = self.eng.start_turn(None, text="Write a very long story about the ocean, at least 400 words.")
        evs = []
        for ev in t.iter_events():
            evs.append(ev)
            if ev["type"] == "audio":
                self.eng.cancel(t.id)  # user barged in while it was speaking
        self.assertIn("cancelled", kinds(evs))
        self.assertNotIn("done", kinds(evs))
        msgs = self.hist.messages(t.conv_id)
        self.assertTrue(msgs[-1]["meta"].get("interrupted"))

    def test_missing_local_model_gives_clear_hint(self):
        self.fake.stop()
        self.conn.check_now()
        self.s.update({"local": {"model": "does-not-exist:1b"}})
        evs, _ = run_turn(self.eng, text="hi")
        err = next(e for e in evs if e["type"] == "error")
        self.assertIn("not installed", err["message"])
        self.assertIn("Set up offline AI", err["hint"])

    def test_local_engine_not_running(self):
        self.fake.stop()
        self.conn.check_now()
        self.s.update({"local": {"ollama_url": "http://127.0.0.1:9"}})
        evs, _ = run_turn(self.eng, text="hi")
        err = next(e for e in evs if e["type"] == "error")
        self.assertIn("not running", err["message"])

    def test_missing_speech_model_gives_clear_hint(self):
        self.s.update({"stt": {"model": "whisper-small"}})  # not downloaded
        evs, _ = run_turn(self.eng, wav=audio.float32_to_wav([0.1] * 16000, 16000))
        err = next(e for e in evs if e["type"] == "error")
        self.assertIn("not installed", err["message"])
        self.assertIn("Set up offline AI", err["hint"])

    def test_missing_voice_falls_back_or_reports(self):
        self.s.update({"tts": {"voice": "xx_XX-nobody-low"}})
        evs, _ = run_turn(self.eng, text="Say OK.")
        # Either the system voice took over (audio) or a clear voice error was reported; text still arrives.
        self.assertTrue("audio" in kinds(evs) or "tts_error" in kinds(evs))
        self.assertEqual(kinds(evs)[-1], "done")

    def test_long_conversation(self):
        self.fake.stop()
        self.conn.check_now()
        self.s.update({"assistant": {"max_context_chars_local": 2500}})
        conv = None
        for i in range(24):
            evs, _ = run_turn(self.eng, conv, text=f"Turn {i}: give me one fun fact in one sentence.")
            conv = next(e for e in evs if e["type"] == "user_saved")["conversation_id"]
            self.assertEqual(kinds(evs)[-1], "done", f"turn {i}: {evs[-1]}")
        self.assertEqual(len(self.hist.messages(conv)), 48)


if __name__ == "__main__":
    unittest.main(verbosity=2)
