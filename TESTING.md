# Testing VoiceAssist

## What has already been tested (and how)

All automated tests were run on a Linux x64 build machine **without your Windows ARM64 laptop**.
"No network" tests ran inside a fresh Linux network namespace (`unshare -rn`), where only the loopback
interface exists: no Wi-Fi/Ethernet, no route and no DNS (`Network is unreachable`, DNS lookups fail).
That matches what the app sees on an air-gapped PC. Ollama, the app and a real Chromium window all ran inside it.
The microphone was Chromium's fake capture device playing a WAV of a spoken question
(made with a different Piper voice from the one that answers).

| Test | Result |
|---|---|
| **B: Offline.** Hands-free mic → spoken "What is the capital of France?" → local Whisper → local LLM → spoken answer | ✅ heard exactly, answered "Paris", audio played, voice in 1.1–1.8 s |
| **C: Airplane mode.** Fresh app start with every radio blocked (simulated through Linux rfkill) and no network | ✅ indicator **AIRPLANE MODE**, full voice conversation works |
| Follow-up questions in the same conversation without restarting | ✅ |
| Interrupting by speaking over a long answer (barge-in) | ✅ story interrupted and marked as interrupted, new question answered |
| Push-to-talk (hold Space), nothing recorded after release | ✅ |
| Mute: nothing heard while muted; heard again after unmuting | ✅ |
| Microphone permission denied → clear message, typing still works | ✅ |
| No microphone → clear message, typing still works | ✅ |
| **A: Online** against a stand-in VoiceAssist cloud (`tests/fake_supabase.py`: same Supabase Auth REST API and the same streaming format as the `chat` function) | ✅ answer from the cloud AI, spoken, tagged ☁; the request carries only the conversation (no AI key) |
| **Shared cloud, no user keys** (`tests/e2e_cloud_api.py`, run against the desktop engine **and** the phone engine): sign-up · cloud answer · questions-left count · daily limit reached → offline AI answers · profile · password change · log out / log in · reset email · delete account | ✅ 18/18 on both engines |
| Automatic switching in the UI: ONLINE → cloud unreachable → OFFLINE + local answer → back online → **asks** to switch back → cloud again; one conversation kept throughout | ✅ |
| Connection drops mid-answer → same answer restarted on the local model | ✅ |
| Connection stalls mid-answer (no data) → aborted when the network is detected down, local answer | ✅ (in under 25 s, not after a 60 s stall) |
| Expired cloud sign-in / daily limit / cloud busy → message shown + local answer | ✅ |
| Missing local model → "ollama pull <model>" hint · Ollama not running → hint · missing Whisper model → download command · missing voice → fallback or message | ✅ |
| Long conversation (24 turns, context trimming) | ✅ |
| Silence/noise doesn't produce fake transcripts | ✅ |
| Privacy guard: local engines refused unless on loopback; strict mode blocks all non-loopback sockets/DNS | ✅ |
| CLI offline self-test with network blocked **and Ollama not running** (app starts it) | ✅ `OFFLINE SELF-TEST PASSED`, 0 network attempts |
| Windows voice-worker protocol (PowerShell stdin/stdout, Unicode) using real PowerShell 7 with the SAPI engine stubbed | ✅ |
| `install.ps1` parsed with PowerShell 7 **and as Windows PowerShell 5.1 reads it** (ANSI code page) | ✅ no parse errors |
| Voice controls collapsed behind the mic button next to Send; Space/M/Esc work while closed | ✅ |
| **Website (real Chrome, fake mic, stand-in cloud):** sign up with email · one shared cloud AI (no key fields) · questions left shown · typed + spoken questions answered · daily limit message · offline → asks to reconnect (no offline answers) · reload keeps the session · wrong password · log in again · forgot password → email → reset link → new password | ✅ all checks (`tests/e2e_web.py`) |
| **Accounts (API):** email sign-up / login through Supabase Auth, email-confirmation flow, every app endpoint closed without a session, forged cookie rejected, other users' conversations return 404, older local conversations handed to the first cloud account | ✅ `tests/test_accounts.py` |
| **Check before sending** (network namespace, real Whisper + local LLM): spoken question appears in the message box, listening pauses, nothing is sent until Send; the edited text is what's sent and answered. Same on the website with the cloud. Send-straight-away mode still works | ✅ |
| **Offline after the change** (network namespace: no adapter, no route, no DNS): voice question → local Whisper → local LLM → spoken answer | ✅ re-run 2026-11-22 |

**Not tested yet — these need your Windows ARM64 PC:**
* Real microphone and speakers, Windows audio-device selection, echo cancellation on laptop speakers.
* The Windows airplane-mode registry value and Wi-Fi toggling (the detection logic is shared, but the Windows signal itself was not exercised).
* The Edge app window and the real Windows SAPI voices.
* `install.ps1` end to end (winget, venv, shortcuts) and the sherpa-onnx ARM64 wheel at runtime.
* The real Supabase project and the real Gemini free tier — until the project exists, all cloud tests use the stand-in (`tests/fake_supabase.py`).

## Run the automated tests yourself

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m unittest tests.test_backend -v     # needs Ollama running + speech models
.venv\Scripts\python -m unittest tests.test_accounts -v    # account system (no models needed)
.venv\Scripts\python tests\e2e_cloud_api.py --data %TEMP%\va-e2e -- .venv\Scripts\python -m voiceassist --no-window --port 8799
.venv\Scripts\python -m voiceassist.diagnostics            # offline self-test (blocks network in-process)
```
`tests/e2e_ui.py` and `tests/e2e_switch.py` drive a real browser (`playwright install chromium`, or
pass `--chrome "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"`).
`tests/run_offline_e2e.sh` is the Linux no-network harness used above.

## Manual test plan on your Windows PC

Run `install.ps1` once while online. Then:

### Test A — Online
1. Connect to Wi-Fi and start VoiceAssist; log in. There is nothing to configure — no API key.
2. Expected: the pill shows **ONLINE**, and the top bar says "Answering with VoiceAssist cloud AI". Settings → Cloud AI shows "Questions left today: 50 of 50".
3. Press the mic (allow microphone access the first time). Ask "What's a good name for a cat?"
4. Expected: your words appear, an answer streams in tagged **☁ Cloud AI**, and you hear it.

### Test B — Offline (Wi-Fi disconnected)
1. While the app is running, turn Wi-Fi off (or unplug Ethernet).
2. Expected within ~3–8 s: the pill turns **OFFLINE**, toast "Offline — switched to on-device AI", and the top bar shows the local model.
3. Ask a question by voice. Expected: answer tagged **🔒 on this PC**, spoken aloud.
4. Turn Wi-Fi back on. Expected: banner "Internet is back. Switch back to the cloud AI?" Choose either option.

### Test C — Airplane mode
1. Press Win + A → **Airplane mode** on (check Wi-Fi and Bluetooth are both off).
2. Close VoiceAssist completely and start it again from the Start menu.
3. Expected: the pill shows **AIRPLANE MODE**. Press the mic, ask a question, and hear the answer.
4. Optional: Settings → Diagnostics → **Run offline self-test**, or run `selftest.bat`. Expected: all steps ✓.

### Accounts
1. Start VoiceAssist (online): the sign-in page appears. **Sign up** with your email and a password.
2. Bottom of the sidebar → your name → **Log out**. Log in again: your conversations are back.
3. Log in with the same email on the phone app and on the website: the same account works there.
4. Log out → **Forgot password?** → your email → open the email's link → choose a new password → you're signed in.

### Extra checks
| Check | How | Expected |
|---|---|---|
| Mic permission error | Windows Settings → Privacy & security → Microphone → off, then press the mic | Red banner explaining how to allow it; typing still works |
| Missing local model | Settings → Local AI → pick a model marked "(not installed)" while offline | Banner with the exact `ollama pull …` command |
| Model loading failure | Quit Ollama from the tray, ask a question | VoiceAssist restarts Ollama; if it can't, it shows "Ollama is not running" with a hint |
| Switching while answering | Ask a long question online, turn Wi-Fi off mid-answer | Banner "Lost connection… answering with the local model", answer continues locally |
| Interrupt speech | While it is talking, start speaking (or press Esc / the stop button) | Voice stops at once; the answer is marked "interrupted"; your new question is answered |
| Long conversation | Have 20+ exchanges | Stays responsive; older turns are trimmed from the model's context automatically |
| Speaker selection | Settings → Audio → Speaker → pick headphones → Settings → Voice → Test voice | Plays on the chosen device |

## Installers and the Android app

| What | How it is tested | Status |
|---|---|---|
| Phone engine (Kotlin) — cloud accounts, history, routing, cloud AI | `tests/e2e_cloud_api.py` against the Kotlin engine on the desktop (`gradle -PcoreOnly :core:devServer`) and the stand-in cloud; `tests/e2e_ui.py` for voice with real sherpa-onnx and llama.cpp | Passed here (18/18); also runs in CI |
| On-device LLM bridge | `core/src/test/.../LlamaTest.kt`: real GGUF generation, multi-turn memory, cancel | Passed here |
| Android shell code | compiled against the Android 15 framework classes | Compiles here; the APK itself is built by CI |
| Android APK end to end | `packaging/android/smoke_test.py` on an API 34 emulator in GitHub Actions: install, sign-in screen visible, cloud sign-up, one-click offline setup, typed + spoken questions answered by the cloud AI, then **airplane mode on** (proves ping/DNS fail) and asks again — must be answered on the phone with the indicator showing AIRPLANE MODE | Runs on every push |
| Windows installer | `packaging/windows/smoke_test.py` on `windows-latest` (x64) and `windows-11-arm` (ARM64): silent install with no admin, engine start, cloud sign-up + cloud answer, one-click setup (silent Ollama install + model pull), typed + spoken questions, offline self-test with the network blocked, silent uninstall | Runs on every push |
