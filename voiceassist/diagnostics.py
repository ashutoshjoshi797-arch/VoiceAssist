"""Offline self-test: proves the whole voice loop works on this machine without the internet.

    python -m voiceassist.diagnostics           # CLI, blocks ALL non-loopback network access first
    (or Settings → Diagnostics → Run offline self-test in the app)

Steps: synthesise a question with the local voice -> recognise it with local Whisper ->
answer with the local LLM -> synthesise the answer. Reports timings for each stage.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from . import netutil

QUESTION = "What is the capital of France?"


def run_selftest(settings, engine) -> dict:
    report: dict = {"steps": [], "ok": True}

    def step(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            report["steps"].append({"name": name, "ok": True, "seconds": round(time.time() - t0, 2), "detail": detail})
            return detail
        except Exception as e:  # noqa: BLE001
            report["ok"] = False
            report["steps"].append({"name": name, "ok": False, "seconds": round(time.time() - t0, 2),
                                    "error": str(e), "hint": getattr(e, "hint", "")})
            return None

    from .llm import build_local

    tts = engine.tts.local_engine()
    holder = {}

    def speak_question():
        holder["wav"] = _synth(tts, QUESTION)
        return f"{tts.describe()}: {len(holder['wav'][0]) / holder['wav'][1]:.1f}s of audio"

    step("Text-to-speech (local voice)", speak_question)
    heard = None
    if "wav" in holder:
        samples, sr = holder["wav"]
        heard = step("Speech-to-text (local Whisper)", lambda: engine.stt.transcribe(samples, sr)["text"])
    question = heard or QUESTION

    def ask():
        engine.ensure_ollama_running()
        lp = build_local(settings)
        out = "".join(lp.stream_chat("Answer in one short sentence.", [{"role": "user", "content": question}],
                                     threading.Event()))
        if not out.strip():
            raise RuntimeError("local model returned nothing")
        return f"{lp.describe()['model']}: {out.strip()[:200]}"

    answer = step("Local language model", ask)
    if answer and tts is not None:
        def speak_answer():
            x, sr = _synth(tts, answer.split(": ", 1)[-1])
            return f"{len(x) / sr:.1f}s of audio"

        step("Speak the answer (local voice)", speak_answer)
    report["question_heard"] = heard
    report["network_violations"] = list(netutil.violations)
    return report


def _synth(tts, text):
    if tts is None:
        raise RuntimeError("no local voice installed — click \"Set up offline AI\" in Settings → Local AI")
    samples, sr = tts.synth(text)
    if len(samples) == 0:
        raise RuntimeError("voice produced no audio")
    return samples, sr


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--allow-network", action="store_true", help="don't block outbound connections")
    a = ap.parse_args(argv)
    if not a.allow_network:
        netutil.set_socket_guard(True)  # any attempt to reach the internet now raises
    from .config import Settings
    from .connectivity import ConnectionManager
    from .engine import Engine, EventBus
    from .history import HistoryStore

    s = Settings()
    engine = Engine(s, ConnectionManager(s), HistoryStore(None), EventBus())
    rep = run_selftest(s, engine)
    print(json.dumps(rep, indent=2))
    if rep["network_violations"]:
        print("FAIL: something tried to use the network:", rep["network_violations"])
        return 2
    print("\nOFFLINE SELF-TEST PASSED" if rep["ok"] else "\nOFFLINE SELF-TEST FAILED")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
