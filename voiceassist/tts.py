"""Text-to-speech.

* PiperTTS     — Piper neural voices on-device through sherpa-onnx (natural, fast on CPU).
* SystemTTS    — the operating system's built-in offline voices (Windows SAPI via a persistent
                 PowerShell worker, `say` on macOS, espeak-ng on Linux). Zero downloads; used as a
                 fallback when no Piper voice is installed.
* OpenAICloudTTS — optional cloud voice, only when the user enables cloud speech.

Plus SentenceChunker, which turns a token stream into speakable chunks so audio starts playing
while the model is still writing the rest of the answer.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from . import audio, models_catalog, netutil

log = logging.getLogger(__name__)

try:
    import sherpa_onnx  # type: ignore
except Exception:  # pragma: no cover
    sherpa_onnx = None


class TTSError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------- text shaping
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+")
_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.S)


def speakable(text: str) -> str:
    """Strip markdown and symbols that sound bad when read aloud."""
    t = _CODE_BLOCK.sub(" (code shown on screen) ", text)
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub(" (link on screen) ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__|\*|_|~~)", "", t)
    t = t.replace("|", " ").replace("&", " and ")
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", t)  # emoji
    return re.sub(r"\s+", " ", t).strip()


class SentenceChunker:
    """Accumulates streamed text and emits chunks at sentence boundaries.

    The first chunk is emitted as early as possible (short clause) to minimise time-to-first-audio;
    later chunks are whole sentences, merged up to ~240 characters for natural prosody.
    """

    _END = re.compile(r"([.!?…]+[\"')\]]?)(\s+|$)|(\n\s*\n|\n(?=\s*[-*•\d]))")
    _ABBREV = re.compile(r"\b(e\.g|i\.e|Mr|Mrs|Ms|Dr|vs|etc|St|No|approx)$", re.I)

    def __init__(self, first_min: int = 24, max_len: int = 240):
        self.buf = ""
        self.first_min = first_min
        self.max_len = max_len
        self.emitted = 0
        self._in_code = False

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            chunk = self._take()
            if chunk is None:
                break
            if chunk.strip():
                out.append(chunk.strip())
                self.emitted += 1
        return out

    def _take(self) -> str | None:
        if not self.buf:
            return None
        min_len = self.first_min if self.emitted == 0 else 40
        pos = 0
        for m in self._END.finditer(self.buf):
            if m.group(3) is None:  # sentence punctuation
                if m.group(2) == "":
                    continue  # punctuation at the very end of the buffer: wait for more text
                if self._ABBREV.search(self.buf[: m.start()]):
                    continue  # "e.g." / "Dr." are not sentence ends
            if m.end() >= min_len:
                pos = m.end()
                break
        if not pos and len(self.buf) > self.max_len:
            # very long run-on: break at the last comma/semicolon/space
            cut = max(self.buf.rfind(", ", 0, self.max_len), self.buf.rfind("; ", 0, self.max_len))
            pos = cut + 1 if cut > 40 else self.buf.rfind(" ", 0, self.max_len) + 1 or self.max_len
        if not pos:
            return None
        chunk, self.buf = self.buf[:pos], self.buf[pos:]
        return chunk

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


# --------------------------------------------------------------------------- engines
class PiperTTS:
    name = "piper"

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._tts = None
        self._key = None
        self.load_error: str | None = None

    def _cfg_key(self):
        t = self.settings.get("tts")
        return (t["voice"], self.settings.tts_threads(), t.get("provider", "cpu"))

    def available(self) -> bool:
        return sherpa_onnx is not None and models_catalog.tts_files(self.settings.get("tts.voice")) is not None

    def ensure_loaded(self):
        key = self._cfg_key()
        with self._lock:
            if self._tts is not None and self._key == key:
                return self._tts
            if sherpa_onnx is None:
                raise TTSError("sherpa-onnx is not installed.")
            voice, threads, provider = key
            files = models_catalog.tts_files(voice)
            if not files:
                self.load_error = f"Voice '{voice}' is not installed."
                raise TTSError(self.load_error, "Click \"Set up offline AI\" to download it (Settings → Local AI).")
            t0 = time.time()
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=files["model"], tokens=files["tokens"], data_dir=files["data_dir"]),
                    num_threads=threads, provider=provider),
                max_num_sentences=1,
            )
            try:
                tts = sherpa_onnx.OfflineTts(cfg)
            except Exception as e:
                self.load_error = f"Could not load voice '{voice}': {e}"
                raise TTSError(self.load_error) from e
            log.info("TTS voice %s loaded in %.2fs", voice, time.time() - t0)
            self._tts, self._key, self.load_error = tts, key, None
            return tts

    def synth(self, text: str, cancel_event=None) -> tuple[np.ndarray, int]:
        tts = self.ensure_loaded()
        sid = int(self.settings.get("tts.speaker_id", 0) or 0)
        if sid >= tts.num_speakers:
            sid = 0
        speed = float(self.settings.get("tts.speed", 1.0) or 1.0)

        def cb(_samples, _progress):
            return 0 if (cancel_event is not None and cancel_event.is_set()) else 1

        a = tts.generate(text, sid=sid, speed=speed, callback=cb)
        samples = np.asarray(a.samples, dtype=np.float32)
        if len(samples) == 0 and re.search(r"\w", text) and not (cancel_event is not None and cancel_event.is_set()):
            raise TTSError(f"Voice '{self.settings.get('tts.voice')}' produced no audio.",
                           "Reinstall the voice or speech engine (requirements pin sherpa-onnx 1.13.8).")
        return samples, int(a.sample_rate)

    def describe(self) -> str:
        return f"Piper {self.settings.get('tts.voice')} (on-device)"


class SystemTTS:
    """OS built-in offline voices."""

    name = "system"

    _PS_WORKER = r"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
[Console]::Out.WriteLine("READY"); [Console]::Out.Flush()
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($line -eq $null) { break }
  try {
    $req = $line | ConvertFrom-Json
    if ($req.cmd -eq "voices") {
      $names = $s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo.Name }
      [Console]::Out.WriteLine(("VOICES " + (ConvertTo-Json @($names) -Compress))); [Console]::Out.Flush(); continue
    }
    if ($req.voice) { try { $s.SelectVoice($req.voice) } catch {} }
    $s.Rate = [int]$req.rate
    $s.SetOutputToWaveFile($req.path)
    $s.Speak([string]$req.text)
    $s.SetOutputToNull()
    [Console]::Out.WriteLine("OK"); [Console]::Out.Flush()
  } catch { [Console]::Out.WriteLine("ERR " + $_.Exception.Message); [Console]::Out.Flush() }
}
"""

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._proc = None

    def available(self) -> bool:
        if sys.platform == "win32":
            return shutil.which("powershell") is not None or shutil.which("pwsh") is not None
        if sys.platform == "darwin":
            return shutil.which("say") is not None
        return shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None

    def _worker(self):
        if self._proc and self._proc.poll() is None:
            return self._proc
        exe = shutil.which("powershell") or shutil.which("pwsh")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        script = Path(tempfile.gettempdir()) / "voiceassist_sapi_worker.ps1"
        script.write_text(self._PS_WORKER, encoding="utf-8-sig")
        self._proc = subprocess.Popen(
            [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", creationflags=flags, bufsize=1,
        )
        line = self._proc.stdout.readline().strip()
        if line != "READY":
            raise TTSError(f"Windows speech engine did not start ({line!r}).")
        return self._proc

    def voices(self) -> list[str]:
        try:
            if sys.platform == "win32":
                with self._lock:
                    p = self._worker()
                    p.stdin.write(json.dumps({"cmd": "voices"}) + "\r\n")
                    p.stdin.flush()
                    line = p.stdout.readline().strip()
                return json.loads(line[7:]) if line.startswith("VOICES ") else []
            if sys.platform == "darwin":
                out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
                return [ln.split()[0] for ln in out.splitlines() if ln.strip()]
        except Exception as e:
            log.warning("listing system voices failed: %s", e)
        return []

    def synth(self, text: str, cancel_event=None) -> tuple[np.ndarray, int]:
        speed = float(self.settings.get("tts.speed", 1.0) or 1.0)
        voice = self.settings.get("tts.system_voice") or ""
        fd, path = tempfile.mkstemp(suffix=".wav" if sys.platform != "darwin" else ".aiff")
        os.close(fd)
        try:
            if sys.platform == "win32":
                rate = max(-10, min(10, int(round((speed - 1.0) * 10))))
                with self._lock:
                    p = self._worker()
                    p.stdin.write(json.dumps({"text": text, "path": path, "voice": voice, "rate": rate}) + "\r\n")
                    p.stdin.flush()
                    line = p.stdout.readline().strip()
                if line != "OK":
                    raise TTSError(f"Windows speech failed: {line}")
                data = Path(path).read_bytes()
            elif sys.platform == "darwin":
                wav = path[:-5] + ".wav"
                cmd = ["say", "-o", wav, "--data-format=LEI16@22050", "-r", str(int(175 * speed))]
                if voice:
                    cmd += ["-v", voice]
                subprocess.run(cmd + [text], check=True, timeout=60)
                data = Path(wav).read_bytes()
                Path(wav).unlink(missing_ok=True)
            else:
                exe = shutil.which("espeak-ng") or shutil.which("espeak")
                subprocess.run([exe, "-s", str(int(165 * speed)), "-w", path, text], check=True, timeout=60,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                data = Path(path).read_bytes()
            return audio.wav_to_float32(data)
        finally:
            Path(path).unlink(missing_ok=True)

    def describe(self) -> str:
        return "System voice (on-device)"


class OpenAICloudTTS:
    name = "cloud"

    def __init__(self, settings, api_key: str):
        self.settings, self.api_key = settings, api_key
        self.base = os.environ.get("VOICEASSIST_OPENAI_BASE", "https://api.openai.com/v1").rstrip("/")

    def synth(self, text: str, cancel_event=None) -> tuple[np.ndarray, int]:
        body = {
            "model": self.settings.get("online.openai_tts_model", "gpt-4o-mini-tts"),
            "voice": self.settings.get("online.openai_tts_voice", "alloy"),
            "input": text, "response_format": "wav",
            "speed": float(self.settings.get("tts.speed", 1.0) or 1.0),
        }
        s = netutil.open_request("POST", f"{self.base}/audio/speech", json_body=body, timeout=30,
                                 headers={"Authorization": f"Bearer {self.api_key}"}, cancel_event=cancel_event)
        return audio.wav_to_float32(s.read_all())

    def describe(self) -> str:
        return f"OpenAI {self.settings.get('online.openai_tts_model')} (cloud)"


class TTSRouter:
    """Picks the TTS engine: cloud (if enabled & online) → Piper → system voice → none."""

    def __init__(self, settings):
        self.settings = settings
        self.piper = PiperTTS(settings)
        self.system = SystemTTS(settings)

    def local_engine(self):
        engine = self.settings.get("tts.engine", "piper")
        if engine == "none":
            return None
        if engine == "piper" and self.piper.available():
            return self.piper
        if self.system.available():
            return self.system
        if self.piper.available():
            return self.piper
        return None

    def status(self) -> dict:
        voice = self.settings.get("tts.voice")
        eng = self.local_engine()
        return {
            "engine": self.settings.get("tts.engine"),
            "active": eng.describe() if eng else "none (text only)",
            "voice": voice,
            "installed": models_catalog.tts_files(voice) is not None,
            "voices": models_catalog.installed_voices(),
            "system_available": self.system.available(),
            "error": self.piper.load_error,
        }
