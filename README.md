# VoiceAssist — desktop AI voice assistant that works online, offline and in airplane mode

Talk to an AI with your voice and hear it answer. When the internet is available it can use
**Claude, OpenAI or Gemini**; when Wi-Fi is off, Ethernet is unplugged or **airplane mode** is on,
it keeps working with **on-device models only** — speech recognition (Whisper), the language model
(Ollama), and the voice (Piper) all run on your computer.

Built for Windows 10/11, native on **Windows-on-ARM (ARM64)** and on x64, and as an **Android app** with
the same features (everything, including the offline AI, runs on the phone). It also runs on Linux/macOS.

### Install — no terminal needed

| Device | Download | Then |
|---|---|---|
| Windows 10/11 (ARM64 or x64) | `VoiceAssist-Setup-<version>.exe` | Double-click → Next → Install. No admin rights needed. Open **VoiceAssist** from the Start menu or desktop |
| Any device with Chrome / Edge — no download | the website link (GitHub Pages) | Open it, create an account, add your Claude, OpenAI or Gemini API key in Settings, talk. **Online only** — for offline use install the app |
| Android 8.0+ (64-bit phone) | `VoiceAssist-<version>.apk` | Open the file on the phone → allow "install unknown apps" for your browser/Files app when asked → Install |

Both files are built and tested automatically by GitHub Actions (`.github/workflows/build.yml`) and attached
to every **Release** (and to each Actions run as artifacts). The first time the app opens: create your
account, then click **Set up offline AI** — it downloads the speech models and a local AI model sized for
your device's memory (on Windows it also installs Ollama silently). After that it works with Wi-Fi off.

---

## 1. Features

| Area | What you get |
|---|---|
| Voice | Mic button, **hands-free** continuous conversation with voice-activity detection, **push-to-talk** (hold the button or <kbd>Space</kbd>), **mute/unmute** (<kbd>M</kbd>), **stop speaking** (<kbd>Esc</kbd>), **interrupt by talking** over the assistant (barge-in), follow-up questions without restarting |
| Speech-to-text | On-device Whisper (tiny/base/small, English or multilingual) via sherpa-onnx; Silero VAD trims silence. Optional OpenAI cloud transcription when online |
| AI models | Cloud: Claude (Anthropic), OpenAI, Gemini — pick the model in the top bar. Local: any Ollama model (or llama.cpp / LM Studio server) |
| Text-to-speech | Piper neural voices on-device, streamed sentence-by-sentence so speech starts while the answer is still being written. Fallback: Windows built-in voices. Optional OpenAI cloud voice |
| Connectivity | Detects **ONLINE / OFFLINE / AIRPLANE MODE** (airplane mode read from Windows, not guessed), switches to the local model automatically, **asks** before switching back when the internet returns, never loses the conversation |
| UI | Animated mic, live waveform, speaking animation, transcript with per-answer "cloud" / "on this PC" badges, model picker, history sidebar, settings (voices, mic, speaker, VAD sensitivity, privacy), offline self-test |
| Privacy | Offline = nothing leaves the PC (enforced: local engines must be on 127.0.0.1). Recordings not stored unless enabled. API keys in Windows Credential Manager. One-click "delete all history". Strict-offline switch blocks every network connection |

## 2. Architecture

```
 ┌──────────────── Desktop window (Edge/Chrome app mode, 127.0.0.1 only) ────────────────┐
 │  web/app.js: mic capture (echo cancellation) → AudioWorklet → adaptive VAD →            │
 │  WAV 16 kHz → POST /api/turn  ◄── NDJSON stream: transcript, tokens, audio chunks       │
 │  waveform, playback queue (speaker selection), barge-in → POST /api/cancel              │
 └───────────────────────────────────────┬─────────────────────────────────────────────────┘
                                         │ HTTP on 127.0.0.1 (per-launch token)
 ┌───────────────────────────────────────▼──────────── Python backend (voiceassist/) ───────┐
 │ server.py ── engine.py (one turn at a time, cancellable)                                  │
 │                 │ 1. STT: stt.py  LocalWhisperSTT (sherpa-onnx)  | OpenAICloudSTT (opt-in) │
 │                 │ 2. history.py  SQLite + context window trimming                          │
 │                 │ 3. LLM: llm/cloud.py  Claude / OpenAI / Gemini (streaming)               │
 │                 │         llm/local.py  Ollama / OpenAI-compatible localhost server        │
 │                 │    failover: cloud error or connection loss → local model, same turn     │
 │                 │ 4. TTS: tts.py  SentenceChunker → Piper | Windows SAPI | OpenAI (opt-in) │
 │ connectivity.py: airplane flag (registry) + adapters (psutil) + TCP probe → route          │
 │ netutil.py: stdlib HTTP, loopback-only enforcement for local engines, socket guard         │
 └───────────────────────────────────────────────────────────────────────────────────────────┘
```

Why this design:

* **Pure-Python backend + standard library HTTP** → no native build tools, works on ARM64.
  The only native package is `sherpa-onnx` (prebuilt Windows ARM64 wheels) which bundles ONNX Runtime.
* **Audio is captured in the window** (Chromium's WebRTC stack) — that gives hardware-grade echo
  cancellation, which is what makes talking over the assistant (barge-in) work on laptop speakers,
  plus device selection and the waveform for free.
* **Edge app mode** is built into Windows (native ARM64), so there is no Electron/Qt download, and
  the window keeps its own private profile so microphone permission is remembered.
* **Ollama** runs natively on Windows ARM64 and on x64 with automatic GPU use where available.

## 3. Installation from source (developers)

Customers should use the installers above. To run from source instead:

Requirements: Windows 10/11 (ARM64 or x64), 8 GB RAM recommended (4 GB works with `-LowRam`),
about 3–5 GB disk for models.

```powershell
# from the VoiceAssistant folder
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer: finds or installs Python 3.12 (native ARM64 on ARM PCs) → creates `.venv` →
installs packages → downloads speech models → installs Ollama and pulls the local model →
verifies offline readiness → runs the offline self-test → creates Start-menu/Desktop shortcuts.

Options: `-LowRam` (llama3.2:1b + whisper-tiny.en), `-LocalModel gemma3:4b`,
`-SttModel whisper-small.en`, `-Voice en_US-lessac-medium`, `-SkipOllama`, `-NoShortcuts`.

<details><summary>Manual installation (any OS)</summary>

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m voiceassist.setup_models --stt whisper-base.en --voice en_US-amy-low
# install Ollama from https://ollama.com/download, then
ollama pull llama3.2:3b
python -m voiceassist.setup_models --verify
python -m voiceassist
```
</details>

## 4. Offline model installation

| Component | Default | Smaller (low RAM) | Download command |
|---|---|---|---|
| Speech recognition | `whisper-base.en` (~200 MB) | `whisper-tiny.en` (~120 MB) | `python -m voiceassist.setup_models --stt whisper-tiny.en` |
| Multilingual (Hindi etc.) | `whisper-base` | – | `--stt whisper-base` then Settings → Voice → Language `hi` |
| Voice | `en_US-amy-low` | – | `--voice en_US-lessac-medium` (also `en_US-ryan-medium`, `en_GB-alba-medium`) |
| VAD | Silero (2 MB) | – | installed automatically |
| Language model | `llama3.2:3b` (~2 GB) | `llama3.2:1b`, `qwen2.5:1.5b`, `qwen2.5:0.5b` | `ollama pull llama3.2:1b` |

Models live in `%LOCALAPPDATA%\VoiceAssist\models` (speech) and Ollama's own folder
(`%USERPROFILE%\.ollama\models`). **Air-gapped PC:** install on a connected PC, then copy both
folders plus this project folder (with `.venv`) to the same paths on the offline PC.

Check readiness any time: `python -m voiceassist.setup_models --verify` → `READY FOR OFFLINE USE`.

## 5. Online AI provider configuration

API keys are **never hard-coded or written to the settings file**. Either:

* **Settings → Online AI → API keys → Save** (stored in Windows Credential Manager under "VoiceAssist"), or
* environment variables: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`).
  `setx ANTHROPIC_API_KEY "sk-ant-..."` then restart the app.

Pick provider and model in the top-bar model menu or Settings → Online AI. Defaults:
`claude-sonnet-5-5`, `gpt-5.5`, `gemini-3.8-flash` — the model fields are free text, so you can type
any model ID your account supports. Cloud speech recognition / voice (OpenAI) are **off by default**;
enable them in Settings → Online AI → Cloud speech.

## 6. Using it

**Check before sending (on by default).** When you speak, VoiceAssist writes what it heard into the message box
and stops listening. Fix anything that's wrong, then press **Send** (or Enter) — or **Discard**. Untick
"Check before sending" (next to the mic, or Settings → Audio) to have spoken questions sent straight away.

* **Hands-free** (default): press the mic once. Speak; it detects when you stop, answers aloud, and
  keeps listening for your follow-up. Talk over it to interrupt. Press the mic again to stop.
* **Push-to-talk**: switch the toggle, then hold the mic button or <kbd>Space</kbd> while speaking.
* <kbd>M</kbd> mute/unmute · <kbd>Esc</kbd> stop speaking · type in the box if you can't talk.
* **Auto / Local only** (top bar): Auto uses the cloud when online; Local only never touches the internet.
* Status pill: **ONLINE** (green), **OFFLINE** (amber — no internet), **AIRPLANE MODE** (violet —
  Windows reports airplane mode). Next to it: which model is answering and why.
* Every answer is tagged **☁ provider** or **🔒 on this PC**, and the line under the controls says
  exactly what data leaves the computer right now.
* Settings → Diagnostics → **Run offline self-test** proves the full local voice loop works.

### Switching behaviour
1. Internet available + API key → cloud model.
2. Internet lost (detected within ~3–8 s, or immediately when a cloud request fails) → local model.
   If it happens mid-answer, the answer is restarted on the local model automatically.
3. Internet back → a banner asks "Switch to Claude or stay local?" (or switch automatically:
   Settings → General → auto switch back).
4. The conversation history is kept across every switch; the cloud model sees what was said offline.
5. If network detection is unavailable, the app simply runs locally.

## 7. Accounts

VoiceAssist opens on a **sign-in page** with two sections, **Log in** and **Sign up**. Accounts are
**local to this computer**, so they work offline and in airplane mode; nothing is sent to a server.

* **Sign up:** name, username, optional email, password (8+ characters, letters plus a number or symbol).
  The first account on a PC takes over any conversations made before accounts existed.
* **Recovery code:** shown once after sign-up (Copy / Download .txt). There's no email reset because
  everything is offline — use **Log in → Forgot password?** with this code. Each use issues a new code.
* **Each account has its own conversation history**; one account can't see or delete another's.
  App settings (models, voices, microphone) and API keys are shared by everyone on the PC.
* **Keep me signed in** = 30 days (sliding); unticked = until the window closes (max 12 hours).
* **Account menu** (bottom of the sidebar) → Account settings: change name/email, change password
  (other signed-in windows are logged out), new recovery code, log out, delete account (+ its history).
* Security: passwords hashed with salted scrypt; session tokens are random 256-bit values kept in an
  HttpOnly, SameSite=Strict cookie and stored only as hashes; 5 wrong passwords lock the account for
  60 s (doubling up to 15 min); login errors don't reveal which usernames exist.
* Stored in `%LOCALAPPDATA%\VoiceAssist\accounts.sqlite3`. Deleting that file removes all accounts.

## 8. Privacy and data handling

* **Local/offline:** microphone audio is processed in memory by Whisper on this PC, the text goes to
  Ollama on `127.0.0.1`, and the voice is generated locally. The code refuses to send local-engine
  requests anywhere except loopback (`allow_lan` must be enabled explicitly for a LAN server).
* **Online:** the recognised *text* of your question and recent conversation go to the provider you
  selected. Audio goes to OpenAI only if you enable cloud speech.
* **Recordings** are not saved unless Settings → Privacy → "Save my voice recordings" is on.
* **History** is in `%LOCALAPPDATA%\VoiceAssist\history.sqlite3`; turn saving off or delete it all in Settings → Privacy.
* **Strict offline** (Settings → General) blocks every non-loopback network connection from the app, including connectivity checks.
* The window's Content-Security-Policy forbids loading anything from the internet; the local server
  only accepts requests from this window (per-launch token + Host check).

## 9. Performance tuning

* Low RAM: `llama3.2:1b` or `qwen2.5:0.5b`, `whisper-tiny.en`, Settings → Local AI → context 2048.
* Faster first answer offline: keep "Load the local model at startup" on (uses RAM).
* GPU: Ollama uses NVIDIA/AMD GPUs automatically (on Windows ARM it runs on the CPU's NEON cores).
  Whisper/Piper can use CUDA/DirectML via Settings → Voice → Acceleration if your sherpa-onnx build supports it; it falls back to CPU automatically.
* Threads: Settings → Voice → CPU threads (0 = auto).
* Inference runs on background threads/processes — the window never freezes.

Measured in the test sandbox (4-core x64 VM, CPU only, qwen2.5:0.5b, whisper-base.en): speech
recognised in ~0.5 s, **first audio ~1.1–1.8 s after you stop talking**. Bigger models are slower.

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| "Microphone access was blocked" | Click the site-permissions icon in the window title bar → allow microphone; and Windows Settings → Privacy & security → Microphone → allow desktop apps |
| "No microphone was found" | Connect/enable a mic (Settings → System → Sound → Input), press Retry |
| "Ollama is not running" | Start Ollama from the Start menu (VoiceAssist also tries to start it automatically) |
| "Local model … is not installed" | While online: `ollama pull <model>` (exact command shown in the banner) |
| "Speech recognition model … is not installed" | `python -m voiceassist.setup_models --stt <model>` |
| No voice, text only | Install a voice (`--voice en_US-amy-low`) or choose "Windows / system voice" |
| Cloud answers fail | Check the key (Settings → Online AI), or the model name; the app answers locally meanwhile |
| Forgot password | Log in → Forgot password? → enter username + recovery code |
| Lost password AND recovery code | Another account can't reset it (no admin backdoor). Delete `accounts.sqlite3` to remove all accounts (history stays in `history.sqlite3` and goes to the next first account) |
| Window doesn't open | Open the URL printed by `run.bat` (also in `%LOCALAPPDATA%\VoiceAssist\last_url.txt`) |

Logs: `%LOCALAPPDATA%\VoiceAssist\logs\voiceassist.log` (no transcripts are logged).

## 11. Project layout

```
voiceassist/
  main.py            entry point, wiring, window lifecycle
  server.py          local HTTP API + static UI (127.0.0.1, token-protected)
  engine.py          turn pipeline: STT → LLM (failover) → streamed TTS; cancellation/barge-in
  connectivity.py    ONLINE / OFFLINE / AIRPLANE detection and routing policy
  stt.py  tts.py     on-device + optional cloud speech
  llm/               cloud.py (Claude, OpenAI, Gemini), local.py (Ollama, OpenAI-compatible)
  accounts.py        local accounts: scrypt passwords, sessions, lockout, recovery codes
  history.py         SQLite conversations (per account) + context trimming
  netutil.py         HTTP, loopback enforcement, offline socket guard
  secrets_store.py   API keys (env vars / Windows Credential Manager)
  setup_models.py    model downloader + readiness check
  diagnostics.py     offline self-test (blocks all network while running)
  window.py          opens the Edge/Chrome app window
web/                 index.html, app.css, app.js (UI), capture-worklet.js (mic frames)
tests/               unit/integration tests, UI tests with a fake microphone, fake cloud server
install.ps1 run.bat selftest.bat requirements.txt
packaging/windows/   Inno Setup script, payload builder (embedded Python x64 + ARM64), installed-app test
packaging/android/   emulator end-to-end test (online + airplane mode)
android/core/        the phone engine in Kotlin: same HTTP API, accounts, routing, cloud + on-device AI
android/app/         Android shell: WebView (same web/ UI), mic permission, sherpa-onnx speech, Keystore keys
android/llama-jni/   llama.cpp (pinned) + JNI bridge for the on-device language model
.github/workflows/   builds + tests the Windows installer and the Android APK, publishes releases
```

### Website edition (online only)

`packaging/web/build_site.py` builds `site/` from the same `web/` UI plus `web/web-engine.js`, which answers the
UI's `/api` calls inside the browser: accounts (PBKDF2), history and API keys are stored in that browser only;
answers stream directly from Claude / OpenAI / Gemini; voice input uses OpenAI or Gemini transcription when that
key is present, otherwise Chrome's speech recognition; replies use the browser's voice or OpenAI's. It has no
offline mode by design: without internet it shows OFFLINE and asks the user to reconnect. CI tests it with
`tests/e2e_web.py` and publishes it to GitHub Pages.

### Android app

The phone runs the same UI (`web/`) in a WebView, talking to a Kotlin engine on `127.0.0.1` that mirrors the
Python API. On-device pieces: Whisper + Silero VAD + Piper through sherpa-onnx 1.13.8, and llama.cpp (GGUF
models from Hugging Face: Llama 3.2 1B/3B, Qwen 2.5 0.5B/1.5B). Airplane mode is read from Android settings and
network changes from `ConnectivityManager`. Cloud API keys are encrypted with an Android Keystore AES-GCM key.
While the offline AI downloads, a small notification keeps the download alive if you switch apps.
iPhone is not included (it requires a paid Apple developer account to install apps outside the App Store).

See **TESTING.md** for the test plan and results.
