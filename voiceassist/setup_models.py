"""Download the on-device models (run this while you still have internet).

    python -m voiceassist.setup_models                      # recommended set
    python -m voiceassist.setup_models --stt whisper-tiny.en --voice en_US-lessac-medium
    python -m voiceassist.setup_models --list
    python -m voiceassist.setup_models --ollama llama3.2:3b # also pull a local LLM via Ollama
    python -m voiceassist.setup_models --verify             # check everything is present

Speech models come from the sherpa-onnx GitHub releases; the chat model comes from Ollama.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import models_catalog as cat
from . import paths


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "VoiceAssist-setup"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r  {done >> 20} / {total >> 20} MB ({pct}%)", end="", flush=True)
    print()


def _extract_into(archive: Path, target: Path) -> None:
    """Extract a .tar.bz2 whose single top folder becomes `target`."""
    tmp = Path(tempfile.mkdtemp(dir=target.parent))
    try:
        with tarfile.open(archive, "r:bz2") as t:
            t.extractall(tmp, filter="data")
        tops = [p for p in tmp.iterdir()]
        src = tops[0] if len(tops) == 1 and tops[0].is_dir() else tmp
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(src), str(target))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install_stt(name: str, force: bool = False) -> None:
    if name not in cat.STT_MODELS:
        raise SystemExit(f"Unknown STT model {name}. Use --list.")
    if cat.stt_files(name) and not force:
        print(f"[ok] speech recognition model {name} already installed")
        return
    root = paths.stt_dir()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as td:
        arc = Path(td) / "m.tar.bz2"
        _download(cat.stt_url(name), arc)
        _extract_into(arc, root / name)
    # keep the int8 weights only (smaller, faster on CPU)
    for p in (root / name).glob("*.onnx"):
        if ".int8." not in p.name and (root / name / p.name.replace(".onnx", ".int8.onnx")).exists():
            p.unlink()
    shutil.rmtree(root / name / "test_wavs", ignore_errors=True)
    print(f"[ok] installed {name}")


def install_voice(voice: str, force: bool = False) -> None:
    if cat.tts_files(voice) and not force:
        print(f"[ok] voice {voice} already installed")
        return
    root = paths.tts_dir()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as td:
        arc = Path(td) / "v.tar.bz2"
        _download(cat.tts_url(voice), arc)
        _extract_into(arc, root / voice)
    print(f"[ok] installed voice {voice}")


def install_vad(force: bool = False) -> None:
    p = paths.vad_model_path()
    if p.exists() and not force:
        print("[ok] voice activity model already installed")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    _download(cat.VAD_URL, p)
    print("[ok] installed silero VAD")


def pull_ollama(model: str) -> None:
    exe = shutil.which("ollama")
    if not exe:
        print("[!!] Ollama is not installed. Get it from https://ollama.com/download then run:\n"
              f"     ollama pull {model}")
        return
    print(f"  ollama pull {model}")
    subprocess.run([exe, "pull", model], check=False)


def verify(settings=None) -> bool:
    from .config import Settings
    s = settings or Settings()
    ok = True
    stt = s.get("stt.model")
    voice = s.get("tts.voice")
    print(f"Models folder: {paths.models_dir()}")
    print(("[ok] " if cat.stt_files(stt) else "[MISSING] ") + f"speech recognition: {stt}")
    ok &= bool(cat.stt_files(stt))
    if s.get("tts.engine") == "piper":
        print(("[ok] " if cat.tts_files(voice) else "[MISSING] ") + f"voice: {voice}")
        ok &= bool(cat.tts_files(voice))
    print(("[ok] " if cat.vad_installed() else "[optional] ") + "silero VAD")
    try:
        from .llm import build_local
        lp = build_local(s)
        if lp.is_running():
            names = lp.list_models()
            have = s.get("local.model") in names or s.get("local.model") + ":latest" in names
            print(("[ok] " if have else "[MISSING] ") + f"local LLM: {s.get('local.model')}  (installed: {', '.join(names) or 'none'})")
            ok &= have
        else:
            print(f"[MISSING] local LLM engine not running ({s.get('local.engine')})")
            ok = False
    except Exception as e:
        print(f"[MISSING] local LLM: {e}")
        ok = False
    print("READY FOR OFFLINE USE" if ok else "NOT READY — install the missing items while online")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stt", action="append", help="speech recognition model(s) to install")
    ap.add_argument("--voice", action="append", help="Piper voice(s) to install")
    ap.add_argument("--ollama", help="Ollama model tag to pull, e.g. llama3.2:3b")
    ap.add_argument("--local-model", help="local Ollama model tag to use (with --save-defaults)")
    ap.add_argument("--save-defaults", action="store_true",
                    help="only save --stt/--voice/--local-model as the app's default models, no downloads")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args(argv)
    if a.list:
        print(json.dumps({"stt": cat.STT_MODELS, "voices": cat.TTS_VOICES}, indent=2))
        return 0
    if a.verify:
        return 0 if verify() else 1
    if a.save_defaults:
        from .config import Settings
        patch = {}
        if a.stt:
            patch["stt"] = {"model": a.stt[0]}
        if a.voice:
            patch["tts"] = {"voice": a.voice[0]}
        if a.local_model:
            patch["local"] = {"model": a.local_model}
        s = Settings()
        s.update(patch)
        print(f"[ok] default models saved to {paths.settings_path()}: {patch}")
        return 0
    for m in a.stt or ["whisper-base.en"]:
        install_stt(m, a.force)
    for v in a.voice or ["en_US-amy-low"]:
        install_voice(v, a.force)
    install_vad(a.force)
    if a.ollama:
        pull_ollama(a.ollama)
    return 0


if __name__ == "__main__":
    sys.exit(main())
