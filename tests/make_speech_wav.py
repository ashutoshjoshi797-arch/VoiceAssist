"""Build a test WAV for the fake microphone: [silence] phrase [silence] phrase ... using a local Piper voice.

    python tests/make_speech_wav.py out.wav "1.5" "What is the capital of France?" "6"
Numbers are seconds of silence; strings are spoken.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voiceassist import audio, models_catalog  # noqa: E402

import sherpa_onnx  # noqa: E402

voice = "en_US-lessac-medium" if models_catalog.tts_files("en_US-lessac-medium") else "en_US-amy-low"
f = models_catalog.tts_files(voice)
tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
    vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=f["model"], tokens=f["tokens"], data_dir=f["data_dir"]), num_threads=2)))
out, sr = [], 48000
for part in sys.argv[2:]:
    try:
        out.append(np.zeros(int(float(part) * sr), np.float32))
    except ValueError:
        a = tts.generate(part, sid=0, speed=1.0)
        out.append(audio.resample(np.asarray(a.samples, np.float32), a.sample_rate, sr) * 0.8)
x = np.concatenate(out)
Path(sys.argv[1]).write_bytes(audio.float32_to_wav(x, sr))
print(f"{sys.argv[1]}: {len(x) / sr:.1f}s, voice {voice}")
