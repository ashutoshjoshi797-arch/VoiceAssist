"""The conversation engine: audio in -> STT -> chat model (cloud or local) -> streamed TTS out.

One turn runs at a time. Starting a new turn (or calling cancel) interrupts the current one, which
is how barge-in works: the UI detects the user speaking over the assistant, stops playback and
cancels the turn; the partial answer is kept in the history marked as interrupted.

A turn yields events (dicts) that the HTTP layer streams to the UI as NDJSON:
  route, transcript, no_speech, user_saved, llm_start, token, audio, failover, tts_error,
  error, cancelled, done
"""
from __future__ import annotations

import collections
import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

from . import audio, netutil, paths, secrets_store
from .history import HistoryStore, build_context
from .llm import AuthError, LLMError, LocalEngineUnavailable, ModelMissing, build_cloud, build_local
from .stt import LocalWhisperSTT, OpenAICloudSTT, STTError
from .tts import OpenAICloudTTS, SentenceChunker, TTSError, TTSRouter, speakable

log = logging.getLogger(__name__)

_END = object()


class EventBus:
    """Fan-out of app-wide status events to every open UI (server-sent events)."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.last: dict[str, dict] = {}

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
            for ev in self.last.values():  # replay latest status of each kind
                q.put_nowait(ev)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, ev: dict, sticky: bool = True) -> None:
        with self._lock:
            if sticky:
                self.last[ev["type"]] = ev
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass


class AudioStore:
    """Synthesised speech chunks held in memory until the UI fetches them (never written to disk)."""

    def __init__(self, cap: int = 256):
        self._d: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._lock = threading.Lock()
        self.cap = cap

    def put(self, wav: bytes) -> str:
        aid = uuid.uuid4().hex
        with self._lock:
            self._d[aid] = wav
            while len(self._d) > self.cap:
                self._d.popitem(last=False)
        return aid

    def pop(self, aid: str) -> bytes | None:
        with self._lock:
            return self._d.pop(aid, None)


class Turn:
    def __init__(self, conv_id: str | None, user_id: int | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.conv_id = conv_id
        self.user_id = user_id
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.started = time.time()

    def put(self, **ev) -> None:
        ev.setdefault("turn_id", self.id)
        ev["t"] = round(time.time() - self.started, 3)
        self.events.put(ev)

    def iter_events(self):
        while True:
            ev = self.events.get()
            if ev is _END:
                return
            yield ev


class SpeechPipeline:
    """Turns streamed text into audio chunks on a worker thread, in order, as sentences complete."""

    def __init__(self, engine: "Engine", turn: Turn, cloud_tts: OpenAICloudTTS | None):
        self.engine, self.turn, self.cloud = engine, turn, cloud_tts
        self.abort = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.chunker = SentenceChunker()
        self.seq = 0
        self.first_audio_at: float | None = None
        self.tts_failed = False
        self.local = engine.tts.local_engine()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"tts-{turn.id}")
        self.thread.start()

    def _stopped(self) -> bool:
        return self.abort.is_set() or self.turn.cancel.is_set()

    def feed(self, delta: str) -> None:
        for chunk in self.chunker.feed(delta):
            self.q.put(chunk)

    def finish(self) -> None:
        for chunk in self.chunker.flush():
            self.q.put(chunk)
        self.q.put(None)

    def stop(self) -> None:
        self.abort.set()
        self.q.put(None)

    def join(self) -> None:
        self.thread.join()

    def _synth(self, text: str):
        if self.cloud is not None:
            try:
                return self.cloud.synth(text, self.turn.cancel), self.cloud.describe()
            except (netutil.NetworkError, netutil.HTTPStatusError) as e:
                log.warning("cloud TTS failed (%s); using local voice", e)
                self.cloud = None
                if isinstance(e, netutil.NetworkError):
                    self.engine.conn.report_network_failure(f"cloud voice: {e}")
        if self.local is None:
            return None, None
        return self.local.synth(text, self.turn.cancel), self.local.describe()

    def _run(self) -> None:
        while True:
            chunk = self.q.get()
            if chunk is None or self._stopped():
                return
            text = speakable(chunk)
            if not text or self.tts_failed:
                continue
            try:
                res, desc = self._synth(text)
            except (TTSError, Exception) as e:  # noqa: BLE001
                if self._stopped():
                    return
                self.tts_failed = True
                hint = getattr(e, "hint", "")
                log.error("TTS failed: %s", e)
                self.turn.put(type="tts_error", message=f"Voice output failed: {e}", hint=hint)
                continue
            if res is None:
                if not self.tts_failed:
                    self.tts_failed = True
                    self.turn.put(type="tts_error", message="No voice engine is available — showing text only.",
                                  hint="Click \"Set up offline AI\" to download a voice (Settings → Local AI).")
                continue
            if self._stopped():
                return
            samples, sr = res
            if len(samples) == 0:
                continue
            aid = self.engine.audio.put(audio.float32_to_wav(samples, sr))
            if self.first_audio_at is None:
                self.first_audio_at = time.time()
            self.turn.put(type="audio", id=aid, seq=self.seq, text=chunk, duration=round(len(samples) / sr, 2),
                          voice=desc)
            self.seq += 1


class Engine:
    def __init__(self, settings, conn, history: HistoryStore, bus: EventBus):
        self.settings, self.conn, self.history, self.bus = settings, conn, history, bus
        self.stt = LocalWhisperSTT(settings)
        self.tts = TTSRouter(settings)
        self.audio = AudioStore()
        self._lock = threading.Lock()
        self.current: Turn | None = None
        self.local_status: dict = {"type": "local_model", "state": "unknown"}
        self._preload_lock = threading.Lock()
        self._cloud_abort: threading.Event | None = None
        conn.on_change(self._on_network)

    def _on_network(self, kind: str, snap: dict) -> None:
        # Network went away while a cloud answer is streaming: abort it now (instead of waiting for a
        # read timeout) so the turn fails over to the local model within a few seconds.
        if snap["state"] != "ONLINE" and self._cloud_abort is not None:
            self._cloud_abort.set()

    # ------------------------------------------------------------------ warm-up
    def warmup(self) -> None:
        threading.Thread(target=self._warmup, daemon=True, name="warmup").start()

    def _warmup(self) -> None:
        self.bus.publish({"type": "speech_models", "state": "loading"})
        stt_err = tts_err = None
        try:
            self.stt.ensure_loaded()
        except STTError as e:
            stt_err = {"message": str(e), "hint": e.hint}
        eng = self.tts.local_engine()
        try:
            if eng is self.tts.piper:
                eng.ensure_loaded()
                eng.synth("Ready.")  # first inference allocates buffers; do it now, not on the first answer
        except TTSError as e:
            tts_err = {"message": str(e), "hint": e.hint}
        if eng is None:
            tts_err = {"message": "No voice installed — answers will be text only.",
                       "hint": "Click \"Set up offline AI\" to download a voice (Settings → Local AI)."}
        self.bus.publish({"type": "speech_models", "state": "error" if (stt_err or tts_err) else "ready",
                          "stt": self.stt.status(), "tts": self.tts.status(), "stt_error": stt_err, "tts_error": tts_err})
        if self.settings.get("local.preload", True):
            self.preload_local()

    def ensure_ollama_running(self) -> bool:
        """Start `ollama serve` if Ollama is installed but not running (works fully offline)."""
        lp = build_local(self.settings)
        if getattr(lp, "name", "") != "ollama" or lp.is_running():
            return lp.is_running()
        exe = shutil.which("ollama")
        if not exe and sys.platform == "win32":
            cand = paths.Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"
            exe = str(cand) if cand.exists() else None
        if not exe:
            return False
        log.info("starting Ollama: %s serve", exe)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        try:
            subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except OSError as e:
            log.warning("could not start Ollama: %s", e)
            return False
        for _ in range(40):
            time.sleep(0.25)
            if lp.is_running():
                return True
        return False

    def preload_local(self) -> None:
        if not self._preload_lock.acquire(blocking=False):
            return
        try:
            model = self.settings.get("local.model")
            self._set_local({"state": "loading", "model": model, "message": f"Loading {model}…"})
            self.ensure_ollama_running()
            lp = build_local(self.settings)
            t0 = time.time()
            lp.preload()
            self._set_local({"state": "ready", "model": model, "message": f"{model} ready",
                             "seconds": round(time.time() - t0, 1)})
        except ModelMissing as e:
            self._set_local({"state": "missing", "model": self.settings.get("local.model"), "message": str(e), "hint": e.hint})
        except LocalEngineUnavailable as e:
            self._set_local({"state": "unavailable", "message": str(e), "hint": e.hint})
        except Exception as e:  # noqa: BLE001
            self._set_local({"state": "error", "message": f"Local model failed to load: {e}"})
        finally:
            self._preload_lock.release()

    def _set_local(self, st: dict) -> None:
        self.local_status = {"type": "local_model", **st}
        self.bus.publish(self.local_status)

    # ------------------------------------------------------------------ turns
    def transcribe_only(self, wav: bytes) -> dict:
        """Speech -> text without answering: the window shows the words so the user can check them first."""
        samples, sr = audio.wav_to_float32(wav)
        if self.settings.get("privacy.save_recordings", False):
            (paths.recordings_dir() / time.strftime("rec-%Y%m%d-%H%M%S.wav")).write_bytes(wav)
        r = self.stt.transcribe(samples, sr)
        return {"text": (r.get("text") or "").strip(), "engine": r.get("engine", ""), "seconds": r.get("seconds")}

    def start_turn(self, conv_id: str | None, wav: bytes | None = None, text: str | None = None,
                   user_id: int | None = None) -> Turn:
        with self._lock:
            if self.current is not None:
                self.current.cancel.set()
            turn = Turn(conv_id, user_id)
            self.current = turn
        turn.thread = threading.Thread(target=self._run_turn_safe, args=(turn, wav, text), daemon=True,
                                       name=f"turn-{turn.id}")
        turn.thread.start()
        return turn

    def cancel(self, turn_id: str | None = None) -> bool:
        with self._lock:
            t = self.current
        if t is not None and (turn_id is None or t.id == turn_id):
            t.cancel.set()
            return True
        return False

    def _run_turn_safe(self, turn: Turn, wav, text) -> None:
        try:
            self._run_turn(turn, wav, text)
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            turn.put(type="error", message=f"Unexpected error: {e}")
        finally:
            turn.events.put(_END)
            with self._lock:
                if self.current is turn:
                    self.current = None

    def _run_turn(self, turn: Turn, wav: bytes | None, text: str | None) -> None:
        route, reason = self.conn.route()
        net = self.conn.snapshot()["state"]
        turn.put(type="route", route=route, reason=reason, network=net)
        t_stt = None

        # ---------------------------------------------------------------- 1. speech -> text
        stt_engine = "typed"
        if wav is not None:
            try:
                samples, sr = audio.wav_to_float32(wav)
            except Exception as e:
                turn.put(type="error", message=f"Could not read the recorded audio: {e}")
                return
            if self.settings.get("privacy.save_recordings", False):
                fname = paths.recordings_dir() / time.strftime("rec-%Y%m%d-%H%M%S.wav")
                fname.write_bytes(wav)
            result = None
            if route == "online" and self.settings.get("online.cloud_stt") and secrets_store.get_key("openai"):
                try:
                    result = OpenAICloudSTT(self.settings, secrets_store.get_key("openai")).transcribe(samples, sr, turn.cancel)
                except netutil.NetworkError as e:
                    self.conn.report_network_failure(f"cloud speech recognition: {e}")
                    route, reason = "local", "Internet lost during speech recognition"
                    turn.put(type="failover", message="Cloud speech recognition unreachable — using on-device Whisper.")
                except Exception as e:  # noqa: BLE001
                    log.warning("cloud STT failed: %s; using local", e)
            if turn.cancel.is_set():
                turn.put(type="cancelled", stage="stt")
                return
            if result is None:
                try:
                    result = self.stt.transcribe(samples, sr)
                except STTError as e:
                    turn.put(type="error", message=str(e), hint=e.hint)
                    return
            text = result["text"]
            stt_engine = result.get("engine", "")
            t_stt = result.get("seconds")
            turn.put(type="transcript", text=text, engine=stt_engine, seconds=t_stt, audio_seconds=round(result.get("duration", 0), 2))
            if not text:
                turn.put(type="no_speech", reason=result.get("reason", "nothing recognised"))
                turn.put(type="done", spoken=False)
                return
        text = (text or "").strip()
        if not text:
            turn.put(type="done", spoken=False)
            return
        if turn.cancel.is_set():
            turn.put(type="cancelled", stage="stt")
            return

        # ---------------------------------------------------------------- 2. history
        cid = turn.conv_id
        if not cid or not self.history.exists(cid, turn.user_id):
            cid = self.history.create(user_id=turn.user_id)  # never write into another user's conversation
            turn.conv_id = cid
        user_mid = self.history.add(cid, "user", text, {"input": "voice" if wav is not None else "text", "stt": stt_engine})
        turn.put(type="user_saved", conversation_id=cid, message_id=user_mid)

        # ---------------------------------------------------------------- 3. chat model (with failover)
        routes = [route] + (["local"] if route == "online" else [])
        for attempt in routes:
            try:
                provider = build_cloud(self.settings) if attempt == "online" else build_local(self.settings)
            except AuthError as e:
                if attempt == "online":
                    continue
                turn.put(type="error", message=str(e), hint=e.hint)
                return
            budget = int(self.settings.get(
                "assistant.max_context_chars_online" if attempt == "online" else "assistant.max_context_chars_local"))
            ctx = build_context(self.history.messages(cid), budget)
            outcome = self._answer(turn, provider, ctx, cid, t_stt)
            if outcome == "failover":
                continue
            return
        turn.put(type="error", message="No chat model could answer.", hint="Check Settings → Local model.")

    def _answer(self, turn: Turn, provider, ctx, cid: str, t_stt) -> str:
        """Returns 'ok', 'failover', 'cancelled' or 'error'."""
        info = provider.describe()
        turn.put(type="llm_start", **info)
        cloud_tts = None
        if info["kind"] == "cloud" and self.settings.get("online.cloud_tts") and secrets_store.get_key("openai"):
            cloud_tts = OpenAICloudTTS(self.settings, secrets_store.get_key("openai"))
        speech = SpeechPipeline(self, turn, cloud_tts)
        parts: list[str] = []
        t0 = time.time()
        t_first = None
        system = self.settings.get("assistant.system_prompt")
        stop_event = turn.cancel
        if info["kind"] == "cloud":
            stop_event = threading.Event()
            self._cloud_abort = stop_event

            def _link():
                while not stop_event.is_set():
                    if turn.cancel.wait(0.2):
                        stop_event.set()

            threading.Thread(target=_link, daemon=True).start()
        try:
            if info["kind"] == "local":
                self.ensure_ollama_running() if info["provider"] == "ollama" else None
            for delta in provider.stream_chat(system, ctx, stop_event):
                if turn.cancel.is_set():
                    raise netutil.Cancelled()
                if t_first is None:
                    t_first = time.time()
                parts.append(delta)
                turn.put(type="token", text=delta)
                speech.feed(delta)
            speech.finish()
            speech.join()
        except netutil.Cancelled:
            speech.stop()
            if not turn.cancel.is_set() and info["kind"] == "cloud":
                # aborted because the network went away, not by the user
                turn.put(type="failover", reset=True,
                         message=f"Lost connection to {info['label']} — answering with the local model instead.")
                threading.Thread(target=self.preload_local, daemon=True).start()
                return "failover"
            partial = "".join(parts).strip()
            if partial:
                self.history.add(cid, "assistant", partial, {**info, "interrupted": True})
            turn.put(type="cancelled", stage="answer", partial=partial)
            return "cancelled"
        except netutil.NetworkError as e:
            speech.stop()
            if turn.cancel.is_set():
                turn.put(type="cancelled", stage="answer")
                return "cancelled"
            if info["kind"] == "cloud":
                self.conn.report_network_failure(str(e))
                turn.put(type="failover", reset=True,
                         message=f"Lost connection to {info['label']} — answering with the local model instead.")
                threading.Thread(target=self.preload_local, daemon=True).start()
                return "failover"
            turn.put(type="error", message=f"Local model connection failed: {e}")
            return "error"
        except (LocalEngineUnavailable, ModelMissing) as e:
            speech.stop()
            self._set_local({"state": "missing" if isinstance(e, ModelMissing) else "unavailable",
                             "message": str(e), "hint": e.hint, "model": self.settings.get("local.model")})
            turn.put(type="error", message=str(e), hint=e.hint)
            return "error"
        except LLMError as e:
            speech.stop()
            if info["kind"] == "cloud" and not turn.cancel.is_set():
                # Cloud provider refused (bad key, quota, outage): still answer, using the local model.
                turn.put(type="failover", reset=True, message=f"{e} — answering with the local model instead.",
                         hint=e.hint)
                return "failover"
            turn.put(type="error", message=str(e), hint=e.hint)
            return "error"
        except netutil.OfflineViolation as e:
            speech.stop()
            turn.put(type="error", message=str(e))
            return "error"

        finally:
            if self._cloud_abort is stop_event:
                self._cloud_abort = None
            if stop_event is not turn.cancel:
                stop_event.set()  # ends the link thread

        answer = "".join(parts).strip()
        if turn.cancel.is_set():
            if answer:
                self.history.add(cid, "assistant", answer, {**info, "interrupted": True})
            turn.put(type="cancelled", stage="speech", partial=answer)
            return "cancelled"
        if not answer:
            turn.put(type="error", message=f"{info['label']} returned an empty answer.")
            return "error"
        mid = self.history.add(cid, "assistant", answer, info)
        now = time.time()
        turn.put(
            type="done", spoken=speech.seq > 0, message_id=mid, conversation_id=cid, provider=info,
            timings={
                "stt_s": t_stt,
                "first_token_s": round(t_first - t0, 2) if t_first else None,
                "first_audio_s": round(speech.first_audio_at - turn.started, 2) if speech.first_audio_at else None,
                "total_s": round(now - turn.started, 2),
            },
        )
        return "ok"
