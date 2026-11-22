"""Catalog of downloadable on-device speech models (all served from the sherpa-onnx GitHub releases).

Layout on disk (under paths.models_dir()):
    stt/<name>/<prefix>-encoder[.int8].onnx, <prefix>-decoder[.int8].onnx, <prefix>-tokens.txt
    tts/<voice>/<voice>.onnx, tokens.txt, espeak-ng-data/
    vad/silero_vad.onnx
"""
from __future__ import annotations

from pathlib import Path

from . import paths

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

STT_MODELS = {
    "whisper-tiny.en": {"archive": "sherpa-onnx-whisper-tiny.en", "prefix": "tiny.en", "size_mb": 118,
                        "lang": "en", "note": "Fastest, lowest RAM, English only"},
    "whisper-base.en": {"archive": "sherpa-onnx-whisper-base.en", "prefix": "base.en", "size_mb": 208,
                        "lang": "en", "note": "Recommended: good accuracy, fast on CPU, English"},
    "whisper-small.en": {"archive": "sherpa-onnx-whisper-small.en", "prefix": "small.en", "size_mb": 610,
                         "lang": "en", "note": "More accurate, slower; English"},
    "whisper-base": {"archive": "sherpa-onnx-whisper-base", "prefix": "base", "size_mb": 208,
                     "lang": "multi", "note": "Multilingual (Hindi and 90+ languages)"},
    "whisper-small": {"archive": "sherpa-onnx-whisper-small", "prefix": "small", "size_mb": 610,
                      "lang": "multi", "note": "Multilingual, more accurate"},
}

TTS_VOICES = {
    "en_US-amy-low": {"note": "US English, female — fast", "size_mb": 67},
    "en_US-lessac-medium": {"note": "US English, male/neutral — natural", "size_mb": 67},
    "en_US-ryan-medium": {"note": "US English, male", "size_mb": 67},
    "en_GB-alba-medium": {"note": "British English, female", "size_mb": 67},
}

VAD_URL = f"{RELEASES}/asr-models/silero_vad.onnx"


def stt_url(name: str) -> str:
    return f"{RELEASES}/asr-models/{STT_MODELS[name]['archive']}.tar.bz2"


def tts_url(voice: str) -> str:
    return f"{RELEASES}/tts-models/vits-piper-{voice}.tar.bz2"


def stt_files(name: str) -> dict | None:
    """Return paths for an installed Whisper model, preferring int8 weights; None if missing."""
    meta = STT_MODELS.get(name)
    d = paths.stt_dir() / name
    if not d.is_dir():
        return None
    prefix = meta["prefix"] if meta else None
    if prefix is None:  # user-supplied folder: discover files
        enc = next(iter(sorted(d.glob("*-encoder*.onnx"))), None)
        if not enc:
            return None
        prefix = enc.name.split("-encoder")[0]

    def pick(kind: str) -> Path | None:
        for cand in (d / f"{prefix}-{kind}.int8.onnx", d / f"{prefix}-{kind}.onnx"):
            if cand.exists():
                return cand
        return None

    enc, dec, tok = pick("encoder"), pick("decoder"), d / f"{prefix}-tokens.txt"
    if enc and dec and tok.exists():
        return {"encoder": str(enc), "decoder": str(dec), "tokens": str(tok)}
    return None


def tts_files(voice: str) -> dict | None:
    d = paths.tts_dir() / voice
    model = d / f"{voice}.onnx"
    if not model.exists():
        cands = sorted(d.glob("*.onnx")) if d.is_dir() else []
        model = cands[0] if cands else None
    if not model or not (d / "tokens.txt").exists():
        return None
    data_dir = d / "espeak-ng-data"
    return {"model": str(model), "tokens": str(d / "tokens.txt"), "data_dir": str(data_dir) if data_dir.is_dir() else ""}


def installed_stt() -> list[str]:
    root = paths.stt_dir()
    return sorted(p.name for p in root.iterdir() if p.is_dir() and stt_files(p.name)) if root.exists() else []


def installed_voices() -> list[str]:
    root = paths.tts_dir()
    return sorted(p.name for p in root.iterdir() if p.is_dir() and tts_files(p.name)) if root.exists() else []


def vad_installed() -> bool:
    return paths.vad_model_path().exists()
