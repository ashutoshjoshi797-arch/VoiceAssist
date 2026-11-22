"""One-click offline setup, driven from the app (no terminal):

1. speech models (Whisper, Piper voice, Silero VAD) — if any are missing
2. the local AI engine (Ollama) — downloaded and installed silently on Windows
3. the local AI model — pulled through Ollama's API

Progress is published on the event bus as {"type": "setup", ...} so every open window shows it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import models_catalog, netutil, paths
from .llm import RECOMMENDED_MODELS, build_local

log = logging.getLogger(__name__)

OLLAMA_WINDOWS_INSTALLER = os.environ.get("VOICEASSIST_OLLAMA_INSTALLER_URL", "https://ollama.com/download/OllamaSetup.exe")

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


def total_ram_gb() -> float | None:
    try:
        return round(psutil.virtual_memory().total / 2 ** 30, 1) if psutil else None
    except Exception:
        return None


def recommended_model() -> str:
    ram = total_ram_gb()
    if ram is not None and ram < 7.5:
        return "llama3.2:1b"
    return "llama3.2:3b"


def find_ollama() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    if sys.platform == "win32":
        p = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "Ollama" / "ollama.exe"
        if p.exists():
            return str(p)
    return None


class SetupError(Exception):
    pass


class LocalAISetup:
    def __init__(self, settings, engine, bus):
        self.settings, self.engine, self.bus = settings, engine, bus
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.job: dict = {"state": "idle"}

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        model = self.settings.get("local.model")
        lp = build_local(self.settings)
        running = lp.is_running()
        installed_models: list[str] = []
        if running:
            try:
                installed_models = lp.list_models()
            except Exception:
                pass
        have_model = model in installed_models or f"{model}:latest" in installed_models
        stt, voice = self.settings.get("stt.model"), self.settings.get("tts.voice")
        speech_ok = bool(models_catalog.stt_files(stt)) and (
            self.settings.get("tts.engine") != "piper" or bool(models_catalog.tts_files(voice)))
        return {
            "platform": sys.platform,
            "engine_installed": bool(find_ollama()) or running,
            "engine_running": running,
            "model": model,
            "model_installed": have_model,
            "installed_models": installed_models,
            "speech_installed": speech_ok,
            "ready": running and have_model and speech_ok,
            "can_auto_install_engine": sys.platform == "win32",
            "ram_gb": total_ram_gb(),
            "recommended": recommended_model(),
            "choices": RECOMMENDED_MODELS,
            "job": self.job,
        }

    # ------------------------------------------------------------------ job control
    def start(self, model: str | None = None) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.job
            if self.settings.get("strict_offline"):
                raise SetupError("Strict offline mode is on — turn it off in Settings → General to download.")
            model = (model or self.settings.get("local.model") or recommended_model()).strip()
            self._cancel.clear()
            self._set(state="starting", model=model, step="Preparing…", progress=0.0)
            self._thread = threading.Thread(target=self._run, args=(model,), daemon=True, name="local-ai-setup")
            self._thread.start()
            return self.job

    def cancel(self) -> None:
        self._cancel.set()

    def _set(self, **kw) -> None:
        self.job = {**self.job, **kw, "updated": time.time()}
        self.bus.publish({"type": "setup", **self.job})

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise SetupError("Cancelled.")

    # ------------------------------------------------------------------ the job
    def _run(self, model: str) -> None:
        try:
            self._speech_models()
            self._engine()
            self._pull(model)
            self.settings.update({"local": {"model": model}})
            self._set(state="loading", step=f"Loading {model}…", progress=1.0)
            self.engine.preload_local()
            self._set(state="done", step="Offline AI is ready. You can now use VoiceAssist without internet.", progress=1.0)
            log.info("offline setup finished (%s)", model)
        except SetupError as e:
            self._set(state="error", step=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("offline setup failed")
            self._set(state="error", step=f"Setup failed: {e}")

    def _download(self, url: str, dest: Path, label: str, base: float, span: float) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "VoiceAssist-setup"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                last = 0.0
                while True:
                    self._check_cancel()
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 0.3:
                        last = time.time()
                        frac = done / total if total else 0
                        self._set(step=f"{label} — {done >> 20} of {total >> 20} MB" if total else f"{label} — {done >> 20} MB",
                                  progress=round(base + span * frac, 4), bytes_done=done, bytes_total=total)
        except OSError as e:
            raise SetupError(f"Download failed ({e}). Check your internet connection and try again.") from e

    def _speech_models(self) -> None:
        from . import setup_models
        stt, voice = self.settings.get("stt.model"), self.settings.get("tts.voice")
        todo = []
        if not models_catalog.stt_files(stt) and stt in models_catalog.STT_MODELS:
            todo.append(("stt", stt, models_catalog.stt_url(stt)))
        if self.settings.get("tts.engine") == "piper" and not models_catalog.tts_files(voice):
            todo.append(("tts", voice, models_catalog.tts_url(voice)))
        if not models_catalog.vad_installed():
            todo.append(("vad", "silero", models_catalog.VAD_URL))
        for i, (kind, name, url) in enumerate(todo):
            self._set(state="speech", step=f"Downloading speech model {name}…")
            if kind == "vad":
                p = paths.vad_model_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                self._download(url, p, "Voice detection model", 0.0, 0.05)
                continue
            root = paths.stt_dir() if kind == "stt" else paths.tts_dir()
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=root) as td:
                arc = Path(td) / "m.tar.bz2"
                self._download(url, arc, f"Speech model {name}", 0.0, 0.05)
                self._set(step=f"Unpacking {name}…")
                setup_models._extract_into(arc, root / name)
        if todo:
            self.engine.warmup()

    def _engine(self) -> None:
        lp = build_local(self.settings)
        if getattr(lp, "name", "") != "ollama":
            if not lp.is_running():
                raise SetupError("The selected local engine isn't running. Start it, or switch Settings → Local AI → Engine to Ollama.")
            return
        if not find_ollama() and not lp.is_running():
            if sys.platform != "win32":
                raise SetupError("Install Ollama from https://ollama.com/download, then click Set up again.")
            tmp = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
            self._set(state="engine", step="Downloading the offline AI engine (Ollama)…", progress=0.05)
            self._download(OLLAMA_WINDOWS_INSTALLER, tmp, "Downloading Ollama", 0.05, 0.25)
            self._check_cancel()
            self._set(step="Installing the offline AI engine…", progress=0.30)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                rc = subprocess.run([str(tmp), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
                                    timeout=20 * 60, creationflags=flags).returncode
            except (OSError, subprocess.TimeoutExpired) as e:
                raise SetupError(f"The Ollama installer could not run: {e}") from e
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            if rc != 0 or not find_ollama():
                raise SetupError(f"Installing Ollama failed (code {rc}). Try again, or install it from https://ollama.com/download.")
        self._set(state="engine", step="Starting the offline AI engine…", progress=0.33)
        if not self.engine.ensure_ollama_running():
            raise SetupError("The offline AI engine is installed but didn't start. Restart your computer and try again.")

    def _pull(self, model: str) -> None:
        lp = build_local(self.settings)
        try:
            if model in lp.list_models() or f"{model}:latest" in lp.list_models():
                self._set(state="model", step=f"{model} is already downloaded.", progress=0.99)
                return
        except Exception:
            pass
        self._set(state="model", step=f"Downloading {model}…", progress=0.35)
        base = self.settings.get("local.ollama_url").rstrip("/")
        stream = netutil.open_request("POST", f"{base}/api/pull", json_body={"model": model, "stream": True},
                                      timeout=600, cancel_event=self._cancel, local_only=True,
                                      allow_lan=bool(self.settings.get("local.allow_lan")))
        layers: dict[str, tuple[int, int]] = {}
        last = 0.0
        try:
            for line in stream.iter_lines():
                if not line.strip():
                    continue
                j = json.loads(line)
                if j.get("error"):
                    raise SetupError(f"Downloading {model} failed: {j['error']}")
                if j.get("digest") and j.get("total"):
                    layers[j["digest"]] = (int(j.get("completed") or 0), int(j["total"]))
                if j.get("status") == "success":
                    break
                if time.time() - last > 0.3:
                    last = time.time()
                    done = sum(c for c, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    frac = done / total if total else 0
                    label = f"Downloading {model} — {done / 2**30:.2f} of {total / 2**30:.2f} GB" if total else f"{model}: {j.get('status', '')}"
                    self._set(step=label, progress=round(0.35 + 0.64 * frac, 4), bytes_done=done, bytes_total=total)
        except netutil.Cancelled as e:
            raise SetupError("Cancelled.") from e
        except netutil.NetworkError as e:
            raise SetupError(f"Download interrupted ({e}). Click Set up again to resume.") from e
