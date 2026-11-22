"""Speech-to-text.

* LocalWhisperSTT — OpenAI Whisper running on-device through sherpa-onnx (ONNX Runtime, CPU or GPU).
  Optional Silero VAD trims silence before decoding (faster, fewer hallucinations).
* OpenAICloudSTT  — OpenAI transcription API, used only when the user enables cloud speech.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np

from . import audio, models_catalog, netutil, paths

log = logging.getLogger(__name__)

try:
    import sherpa_onnx  # type: ignore
except Exception as _e:  # pragma: no cover
    sherpa_onnx = None
    _IMPORT_ERR = _e
else:
    _IMPORT_ERR = None


class STTError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


# Whisper tends to "hear" these in silence/noise.
_HALLUCINATIONS = {
    "", "you", "thank you", "thank you.", "thanks for watching!", "thanks for watching.", ".", "bye.",
    "thank you for watching.", "[blank_audio]", "(silence)",
}
_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*(music|silence|noise|applause|laugh)[^)]*\)", re.I)


def clean_transcript(text: str) -> str:
    t = _BRACKETED.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    if t.lower() in _HALLUCINATIONS:
        return ""
    return t


class LocalWhisperSTT:
    name = "local"

    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._rec = None
        self._rec_key = None
        self._vad_cfg = None
        self.load_error: str | None = None

    # -- loading ------------------------------------------------------------
    def _key(self):
        s = self.settings.get("stt")
        return (s["model"], s.get("language", "en"), self.settings.stt_threads(), s.get("provider", "cpu"))

    def ensure_loaded(self):
        key = self._key()
        with self._lock:
            if self._rec is not None and self._rec_key == key:
                return self._rec
            if sherpa_onnx is None:
                raise STTError(f"sherpa-onnx is not installed ({_IMPORT_ERR}).", "Run install.ps1 / pip install -r requirements.txt")
            name, lang, threads, provider = key
            files = models_catalog.stt_files(name)
            if not files:
                self.load_error = f"Speech recognition model '{name}' is not installed."
                raise STTError(self.load_error, "Click \"Set up offline AI\" to download it (Settings → Local AI).")
            meta = models_catalog.STT_MODELS.get(name, {})
            language = lang if meta.get("lang") == "multi" else "en"
            t0 = time.time()
            try:
                rec = sherpa_onnx.OfflineRecognizer.from_whisper(
                    encoder=files["encoder"], decoder=files["decoder"], tokens=files["tokens"],
                    language=language, task="transcribe", num_threads=threads, provider=provider,
                )
            except Exception as e:
                if provider != "cpu":
                    log.warning("STT provider %s failed (%s); falling back to CPU", provider, e)
                    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
                        encoder=files["encoder"], decoder=files["decoder"], tokens=files["tokens"],
                        language=language, task="transcribe", num_threads=threads, provider="cpu",
                    )
                else:
                    self.load_error = f"Could not load speech model '{name}': {e}"
                    raise STTError(self.load_error, "Click \"Set up offline AI\" to download it again (Settings → Local AI).") from e
            log.info("STT %s loaded in %.2fs (%d threads, %s)", name, time.time() - t0, threads, provider)
            self._rec, self._rec_key, self.load_error = rec, key, None
            return rec

    def _vad_trim(self, x: np.ndarray) -> np.ndarray | None:
        """Return only the speech parts (None = no speech at all). Falls back to `x` if VAD unavailable."""
        if not self.settings.get("stt.use_vad_trim", True) or sherpa_onnx is None:
            return x
        vad_path = paths.vad_model_path()
        if not vad_path.exists():
            return x
        try:
            cfg = sherpa_onnx.VadModelConfig()
            cfg.silero_vad.model = str(vad_path)
            cfg.silero_vad.threshold = 0.4
            cfg.silero_vad.min_silence_duration = 0.4
            cfg.silero_vad.min_speech_duration = 0.15
            cfg.silero_vad.max_speech_duration = 30
            cfg.sample_rate = 16000
            cfg.num_threads = 1
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=max(35, len(x) / 16000 + 5))
            win = cfg.silero_vad.window_size or 512
            for i in range(0, len(x), win):
                vad.accept_waveform(x[i : i + win])
            vad.flush()
            parts = []
            while not vad.empty():
                seg = vad.front
                parts.append(np.asarray(seg.samples, dtype=np.float32))
                vad.pop()
            if not parts:
                return None
            pad = np.zeros(int(0.15 * 16000), dtype=np.float32)
            out = [pad]
            for p in parts:
                out += [p, pad]
            return np.concatenate(out)
        except Exception as e:
            log.warning("VAD trim failed, using raw audio: %s", e)
            return x

    # -- transcription --------------------------------------------------------
    def transcribe(self, samples: np.ndarray, sample_rate: int) -> dict:
        rec = self.ensure_loaded()
        x = audio.resample(np.asarray(samples, dtype=np.float32), sample_rate, 16000)
        dur = len(x) / 16000
        if dur < 0.2:
            return {"text": "", "duration": dur, "engine": "whisper (on-device)", "reason": "too short"}
        t0 = time.time()
        trimmed = self._vad_trim(x)
        if trimmed is None:
            return {"text": "", "duration": dur, "engine": "whisper (on-device)", "reason": "no speech detected"}
        # Whisper handles ≤30 s per pass; longer clips are split.
        texts = []
        step = 28 * 16000
        for i in range(0, len(trimmed), step):
            st = rec.create_stream()
            st.accept_waveform(16000, trimmed[i : i + step])
            rec.decode_stream(st)
            texts.append(st.result.text)
        text = clean_transcript(" ".join(texts))
        return {"text": text, "duration": dur, "seconds": time.time() - t0,
                "engine": f"whisper {self.settings.get('stt.model')} (on-device)"}

    def status(self) -> dict:
        name = self.settings.get("stt.model")
        return {"model": name, "installed": bool(models_catalog.stt_files(name)), "loaded": self._rec is not None,
                "error": self.load_error, "available": models_catalog.installed_stt(),
                "vad": models_catalog.vad_installed(), "engine_ok": sherpa_onnx is not None}


class OpenAICloudSTT:
    name = "cloud"

    def __init__(self, settings, api_key: str):
        self.settings, self.api_key = settings, api_key
        import os
        self.base = os.environ.get("VOICEASSIST_OPENAI_BASE", "https://api.openai.com/v1").rstrip("/")

    def transcribe(self, samples: np.ndarray, sample_rate: int, cancel_event=None) -> dict:
        wav = audio.float32_to_wav(audio.resample(samples, sample_rate, 16000), 16000)
        model = self.settings.get("online.openai_stt_model", "gpt-transcribe")
        fields = {"model": model, "response_format": "json"}
        lang = self.settings.get("stt.language")
        if lang:
            fields["language"] = lang
        body, ctype = netutil.multipart_body(fields, {"file": ("speech.wav", "audio/wav", wav)})
        t0 = time.time()
        j = netutil.request_json(
            "POST", f"{self.base}/audio/transcriptions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": ctype},
            timeout=30, cancel_event=cancel_event,
        )
        return {"text": clean_transcript(j.get("text", "")), "duration": len(samples) / sample_rate,
                "seconds": time.time() - t0, "engine": f"OpenAI {model} (cloud)"}
