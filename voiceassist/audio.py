"""Small audio helpers: WAV <-> float32 numpy, resampling, simple stats."""
from __future__ import annotations

import io
import wave

import numpy as np


def wav_to_float32(data: bytes) -> tuple[np.ndarray, int]:
    """Decode a PCM WAV (8/16/24/32-bit, any channels) to mono float32 in [-1, 1]."""
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        i = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        i = np.where(i & 0x800000, i - 0x1000000, i)
        x = i.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"unsupported WAV sample width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x.astype(np.float32), sr


def float32_to_wav(samples, sample_rate: int) -> bytes:
    x = np.asarray(samples, dtype=np.float32)
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm)
    return buf.getvalue()


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or len(x) == 0:
        return x.astype(np.float32)
    if sr_in > sr_out:
        # crude anti-aliasing: moving-average low-pass before decimation
        k = max(1, int(round(sr_in / sr_out)))
        if k > 1:
            x = np.convolve(x, np.ones(k, dtype=np.float32) / k, mode="same")
    n_out = int(round(len(x) * sr_out / sr_in))
    t = np.arange(n_out, dtype=np.float64) * (sr_in / sr_out)
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


def rms_dbfs(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    r = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * np.log10(max(r, 1e-6))


def concat_wavs(chunks: list[tuple[np.ndarray, int]]) -> tuple[np.ndarray, int]:
    if not chunks:
        return np.zeros(0, dtype=np.float32), 16000
    sr = chunks[0][1]
    return np.concatenate([resample(c, s, sr) for c, s in chunks]), sr
