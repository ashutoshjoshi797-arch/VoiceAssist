/* VoiceAssist desktop UI.
 * Audio capture, voice-activity detection, waveform and playback all run locally in this window;
 * recognised speech is sent only to the local VoiceAssist backend (127.0.0.1).
 */
"use strict";

// ------------------------------------------------------------------ bootstrap / API
const TOKEN = new URLSearchParams(location.hash.slice(1)).get("token") || sessionStorage.getItem("va_token") || "";
if (TOKEN) { sessionStorage.setItem("va_token", TOKEN); history.replaceState(null, "", location.pathname); }

window.__vaStats = { chunksPlayed: 0, secondsPlayed: 0 }; // read by the automated UI tests
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, { method = "GET", json, body, headers = {}, raw = false, signal } = {}) {
  const h = { "X-App-Token": TOKEN, ...headers };
  if (json !== undefined) { h["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  let res;
  try { res = await fetch(path, { method, headers: h, body, signal }); }
  catch (err) {
    if (err.name === "AbortError" || WEB()) throw err;
    // the window lost its engine (it was closed or stopped): say what to do instead of "Failed to fetch"
    const e = new Error("VoiceAssist stopped running in the background. Close this window and open VoiceAssist again from the Start menu.");
    e.status = 0; throw e;
  }
  if (raw) return res;
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && data.auth === "required" && S.user) { signedOut(); }
  if (!res.ok) { const e = new Error(data.error || res.statusText); e.hint = data.hint; e.field = data.field; e.status = res.status; throw e; }
  return data;
}

const WEB = () => S.platform === "web";   // website edition: online only, data kept in this browser
const dev = () => (S.platform === "android" || S.platform === "jvm" ? "phone" : WEB() ? "browser" : "PC");
const devLong = () => (dev() === "phone" ? "phone" : WEB() ? "browser" : "computer");
function setPlatform(p) {
  if (!p || S.platform === p) return;
  S.platform = p; document.body.classList.add(p);
  if (dev() !== "PC") {  // static wording in the page: "this computer" -> "this phone" / "this browser"
    const d = devLong();
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n; (n = w.nextNode());) if (n.nodeValue.includes("computer")) n.nodeValue = n.nodeValue.replace(/this computer/g, "this " + d).replace(/On this computer/g, "On this " + d);
    $$("[title]").forEach((el) => (el.title = el.title.replace(/this computer/g, "this " + d)));
  }
}
const S = {
  platform: "desktop",
  user: null,
  settings: null, state: null, net: null, route: null,
  convId: null, style: "handsfree", muted: false, session: false, // session = hands-free listening active
  status: "off", turn: null, localModels: [],
};

// ------------------------------------------------------------------ small UI helpers
function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

function banner(id, kind, message, { hint = "", actions = [], sticky = true, timeout = 0 } = {}) {
  let el = document.getElementById("banner-" + id);
  if (!el) { el = document.createElement("div"); el.id = "banner-" + id; $("#banners").appendChild(el); }
  el.className = "banner " + kind;
  el.innerHTML = `<div class="msg">${esc(message)}${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</div>`;
  for (const a of actions) {
    const b = document.createElement("button"); b.className = "btn small " + (a.primary ? "primary" : "");
    b.textContent = a.label; b.onclick = () => { a.onClick(); if (!a.keep) removeBanner(id); }; el.appendChild(b);
  }
  const x = document.createElement("button"); x.className = "x"; x.textContent = "×"; x.title = "Dismiss";
  x.onclick = () => removeBanner(id); el.appendChild(x);
  if (timeout) setTimeout(() => removeBanner(id), timeout);
  return el;
}
function removeBanner(id) { document.getElementById("banner-" + id)?.remove(); }

function setStatus(status, text) {
  S.status = status;
  const mic = $("#btn-mic");
  mic.classList.remove("listening", "hearing", "thinking", "speaking", "off", "muted", "ptt-held");
  const label = {
    off: S.style === "ptt" ? (dev() === "phone" ? "Hold the mic to talk" : "Hold the mic or Space to talk") : "Microphone off — press to start",
    listening: "Listening…", hearing: "Hearing you…", thinking: "Thinking…", speaking: "Speaking — talk to interrupt",
    muted: "Microphone muted", ptt: "Recording — release to send",
  }[status] || status;
  if (status === "off") mic.classList.add("off");
  else if (status === "muted") mic.classList.add("muted");
  else if (status === "ptt") mic.classList.add("ptt-held");
  else mic.classList.add(status === "hearing" ? "hearing" : status === "listening" ? "listening" : status);
  if (S.session && status !== "listening" && status !== "hearing" && status !== "muted") mic.classList.add("listening");
  $("#mic-state").textContent = text || label;
  $("#live-caption").textContent = status === "off" ? (S.style === "ptt" ? (dev() === "phone" ? "Hold the mic button to talk" : "Hold Space or the mic button to talk") : "Press the microphone to start a conversation") : "";
  $("#btn-stop").disabled = !(player.speaking || (S.turn && !S.turn.done));
  const vt = $("#btn-voice-toggle");
  vt.className = "voice-toggle" + (status === "off" ? "" : " st-" + status);
  vt.title = (vt.getAttribute("aria-expanded") === "true" ? "Hide" : "Show") + " voice controls — " + (text || label);
  mic.setAttribute("aria-label", status === "off" ? "Start listening" : "Stop listening");
}

// ------------------------------------------------------------------ audio: playback
class Player {
  constructor() { this.ctx = null; this.analyser = null; this.queue = new Map(); this.next = 0; this.cur = null; this.gen = 0; this.speaking = false; this.turnDone = false; this.sink = ""; }
  ensure() {
    if (!this.ctx) {
      this.ctx = new AudioContext({ latencyHint: "interactive" });
      this.analyser = this.ctx.createAnalyser(); this.analyser.fftSize = 1024;
      this.analyser.connect(this.ctx.destination);
      if (this.sink) this.setSink(this.sink);
    }
    if (this.ctx.state === "suspended") this.ctx.resume();
  }
  async setSink(id) {
    this.sink = id || "";
    if (this.ctx && typeof this.ctx.setSinkId === "function") {
      try { await this.ctx.setSinkId(this.sink); } catch (e) { toast("Could not use that speaker: " + e.message); }
    }
  }
  reset() { this.stop(); this.turnDone = false; this.next = 0; }
  stop() {
    this.gen++;
    if (this.saying) { this.saying = false; try { speechSynthesis.cancel(); } catch (_) {} }
    if (this.cur) { try { this.cur.onended = null; this.cur.stop(); } catch (_) {} this.cur = null; }
    this.queue.clear(); this.setSpeaking(false);
  }
  async enqueue(id, seq) {
    const gen = this.gen; this.ensure();
    let buf = null;
    try {
      const res = await api("/api/audio/" + id, { raw: true });
      if (res.ok) buf = await this.ctx.decodeAudioData(await res.arrayBuffer());
    } catch (e) { console.warn("audio chunk failed", e); }
    if (gen !== this.gen) return;
    this.queue.set(seq, buf); this.pump();
  }
  /** Website edition: a sentence spoken by the browser's own voice (no audio file). */
  enqueueSay(seq, text, voice, rate) { this.ensure(); this.queue.set(seq, { say: text, voice, rate }); this.pump(); }
  sayNow(item) {
    const u = new SpeechSynthesisUtterance(item.say);
    const v = item.voice && speechSynthesis.getVoices().find((x) => x.name === item.voice);
    if (v) u.voice = v; u.rate = item.rate || 1;
    const gen = this.gen;
    let finished = false;
    const done = () => { if (finished || gen !== this.gen) return; finished = true; clearTimeout(guard); this.cur = null; this.saying = false; this.pump(); };
    // safety net: some browsers never fire 'end' (e.g. no voices installed)
    const guard = setTimeout(done, 4000 + (item.say.length / 12 / (item.rate || 1)) * 1000);
    u.onend = u.onerror = done;
    this.cur = { stop() { speechSynthesis.cancel(); } }; this.saying = true;
    speechSynthesis.speak(u); this.setSpeaking(true);
    window.__vaStats.chunksPlayed++;
  }
  pump() {
    if (this.cur) return;
    while (this.queue.has(this.next) && this.queue.get(this.next) === null) { this.queue.delete(this.next); this.next++; }
    const b = this.queue.get(this.next);
    if (!b) { if (this.turnDone || !S.turn || S.turn.done) this.setSpeaking(false); return; }
    this.queue.delete(this.next); this.next++;
    if (b.say !== undefined) { this.sayNow(b); return; }
    const src = this.ctx.createBufferSource(); src.buffer = b; src.connect(this.analyser);
    src.onended = () => { this.cur = null; this.pump(); };
    src.start(); this.cur = src; this.setSpeaking(true);
    window.__vaStats.chunksPlayed++; window.__vaStats.secondsPlayed += b.duration;
  }
  setSpeaking(v) {
    if (this.speaking === v) return;
    this.speaking = v;
    document.querySelector(".msg-row.assistant.speaking")?.classList.remove("speaking");
    if (v) { S.turn?.botRow?.classList.add("speaking"); setStatus("speaking"); }
    else afterActivity();
  }
}
const player = new Player();

// ------------------------------------------------------------------ audio: microphone + VAD
class Mic {
  constructor() { this.stream = null; this.ctx = null; this.node = null; this.analyser = null; this.sr = 48000; this.onFrame = null; this.deviceId = ""; }
  get active() { return !!this.stream; }
  /** Windows refused the microphone ("busy or blocked"). Often only one device or one audio-processing mode fails —
   *  e.g. the driver's own sound effects clash with echo cancellation, or the default device is a headset that isn't
   *  connected — so try plain audio, then every other microphone. */
  async fallbackStream(firstError) {
    const gum = (c) => navigator.mediaDevices.getUserMedia({ audio: c });
    await new Promise((r) => setTimeout(r, 400));
    try { return await gum({ echoCancellation: false, noiseSuppression: false, autoGainControl: false }); } catch (_) {}
    try { return await gum(true); } catch (_) {}
    let inputs = [];
    try { inputs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput" && d.deviceId && !["default", "communications"].includes(d.deviceId)); } catch (_) {}
    for (const d of inputs) {
      try {
        const st = await gum({ deviceId: { exact: d.deviceId } });
        toast(`Using microphone: ${d.label || "another microphone"}`, 4000);
        return st;
      } catch (_) {}
    }
    throw firstError;
  }
  async start(deviceId) {
    if (this.stream) return;
    if (!navigator.mediaDevices?.getUserMedia) throw Object.assign(new Error("This window cannot access a microphone."), { name: "NotSupportedError" });
    this.deviceId = deviceId || "";
    const constraints = { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } };
    if (this.deviceId) constraints.audio.deviceId = { exact: this.deviceId };
    try {
      this.stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (e) {
      if (this.deviceId && (e.name === "OverconstrainedError" || e.name === "NotFoundError")) {
        toast("Selected microphone not found — using the default one");
        delete constraints.audio.deviceId; this.stream = await navigator.mediaDevices.getUserMedia(constraints);
      } else if (e.name === "NotReadableError" || e.name === "TrackStartError" || e.name === "AbortError") {
        this.stream = await this.fallbackStream(e);
      } else throw e;
    }
    const track = this.stream.getAudioTracks()[0];
    track.onended = () => { this.stop(); onMicLost(); };
    this.ctx = new AudioContext({ latencyHint: "interactive" });
    this.sr = this.ctx.sampleRate;
    await this.ctx.audioWorklet.addModule("capture-worklet.js");
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.analyser = this.ctx.createAnalyser(); this.analyser.fftSize = 1024;
    this.node = new AudioWorkletNode(this.ctx, "capture", { processorOptions: { frameMs: 20 } });
    this.node.port.onmessage = (e) => this.onFrame && this.onFrame(e.data.frame, e.data.rms);
    const sink = this.ctx.createGain(); sink.gain.value = 0;
    src.connect(this.analyser); src.connect(this.node); this.node.connect(sink); sink.connect(this.ctx.destination);
    track.enabled = !S.muted;
    refreshDevices();
  }
  stop() {
    try { this.stream?.getTracks().forEach((t) => { t.onended = null; t.stop(); }); } catch (_) {}
    try { this.ctx?.close(); } catch (_) {}
    this.stream = this.ctx = this.node = this.analyser = null;
  }
  setMuted(m) { this.stream?.getAudioTracks().forEach((t) => (t.enabled = !m)); }
}
const mic = new Mic();

class VAD {
  // Adaptive energy VAD: tracks the background noise floor and detects speech above it.
  constructor() { this.noise = -62; this.reset(); }
  reset() { this.inSpeech = false; this.run = 0; this.silence = 0; this.speechFrames = 0; this.frames = []; this.pre = []; }
  threshold(barge) {
    const sens = Number(S.settings?.ui?.vad_sensitivity ?? 0.5);
    const margin = 16 - sens * 10;            // 6..16 dB above the noise floor
    const floor = -50 - sens * 10;            // absolute floor -60..-50 dBFS
    return Math.max(this.noise + margin, floor) + (barge ? 8 : 0);
  }
  // returns null | "start" | {end: Float32Array[]} | "discard"
  push(frame, db, barge) {
    const ms = 20;
    const thr = this.threshold(barge);
    const isSpeech = db > thr;
    if (!this.inSpeech) {
      // noise floor: falls fast, rises slowly
      this.noise = db < this.noise ? this.noise * 0.9 + db * 0.1 : this.noise * 0.997 + db * 0.003;
      this.pre.push(frame); if (this.pre.length > 25) this.pre.shift();      // 500 ms pre-roll (keeps the first word when interrupting)
      this.run = isSpeech ? this.run + 1 : 0;
      const need = barge ? 12 : 4;                                             // 240 ms when interrupting, 80 ms otherwise
      if (this.run >= need) {
        this.inSpeech = true; this.frames = this.pre.slice(); this.pre = []; this.silence = 0; this.speechFrames = this.run;
        return "start";
      }
      return null;
    }
    this.frames.push(frame);
    if (isSpeech) { this.silence = 0; this.speechFrames++; } else this.silence++;
    const silenceMs = Number(S.settings?.ui?.silence_ms ?? 800);
    const tooLong = this.frames.length * ms > 30000;
    if (this.silence * ms >= silenceMs || tooLong) {
      const frames = this.frames, speech = this.speechFrames;
      this.reset();
      return speech * ms < 250 ? "discard" : { end: frames };
    }
    return null;
  }
}
const vad = new VAD();
let ptt = null; // {frames: []} while push-to-talk is held
let lastDb = -100;

mic.onFrame = (frame, rms) => {
  const db = 20 * Math.log10(Math.max(rms, 1e-6));
  lastDb = db;
  if (S.muted) return;
  if (ptt) { ptt.frames.push(frame); return; }
  if (!S.session) return;
  const speaking = player.speaking;
  if (speaking && (!S.settings.ui.barge_in || player.saying)) return;
  const r = vad.push(frame, db, speaking);
  if (r === "start") {
    if (speaking || (S.turn && !S.turn.done)) interrupt("barge-in");
    setStatus("hearing");
  } else if (r === "discard") {
    if (S.status === "hearing") afterActivity();
  } else if (r && r.end) {
    sendAudio(r.end, mic.sr);
  }
};

async function encodeWav16k(frames, sr) {
  const n = frames.reduce((a, f) => a + f.length, 0);
  const all = new Float32Array(n); let o = 0;
  for (const f of frames) { all.set(f, o); o += f.length; }
  const outLen = Math.ceil(n * 16000 / sr);
  const off = new OfflineAudioContext(1, outLen, 16000);
  const ab = off.createBuffer(1, n, sr); ab.copyToChannel(all, 0);
  const s = off.createBufferSource(); s.buffer = ab; s.connect(off.destination); s.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
  const w = (p, str) => { for (let i = 0; i < str.length; i++) v.setUint8(p + i, str.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 16000, true);
  v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) { const x = Math.max(-1, Math.min(1, pcm[i])); v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true); }
  return new Blob([buf], { type: "audio/wav" });
}

// ------------------------------------------------------------------ microphone errors
function micErrorMessage(e) {
  switch (e && e.name) {
    case "NotAllowedError": case "SecurityError":
      return ["Microphone access was blocked.", "Allow it: click the site-permissions icon at the left of this window's title, or Windows Settings → Privacy & security → Microphone → turn on 'Microphone access' and 'Let desktop apps access your microphone'."];
    case "NotFoundError": case "OverconstrainedError":
      return ["No microphone was found.", "Plug in or enable a microphone, then press the mic button again. Check Windows Settings → System → Sound → Input."];
    case "NotReadableError": case "AbortError":
      return ["The microphone is busy or blocked by Windows.", "Close other windows using the mic (other Edge windows, Teams, Zoom, WhatsApp, Sound Recorder). Also check Windows Settings → Privacy & security → Microphone: 'Microphone access' and 'Let desktop apps access your microphone' must be on. Then press Retry."];
    default:
      return ["Could not start the microphone: " + (e?.message || e), "You can still type messages below."];
  }
}
function onMicLost() {
  S.session = false; setStatus("off");
  banner("mic", "error", "The microphone was disconnected.", { hint: "Reconnect it and press the mic button.", actions: [{ label: "Retry", primary: true, onClick: () => startSession() }] });
}

async function ensureMic() {
  if (mic.active) return true;
  try {
    await mic.start(S.settings.ui.mic_device);
    removeBanner("mic");
    return true;
  } catch (e) {
    const [msg, hint] = micErrorMessage(e);
    banner("mic", "error", msg, { hint, actions: [{ label: "Retry", primary: true, onClick: () => (S.style === "ptt" ? ensureMic() : startSession()) }] });
    setStatus("off");
    return false;
  }
}

async function startSession() {
  player.ensure();
  if (!(await ensureMic())) return;
  S.session = true; vad.reset();
  setStatus(S.muted ? "muted" : "listening");
}
function stopSession() {
  S.session = false; vad.reset(); interrupt("stop");
  mic.stop(); setStatus("off");
}

function afterActivity() {
  if (S.turn && !S.turn.done) { if (!player.speaking) setStatus("thinking"); return; }
  if (player.speaking) return;
  if (S.muted && (S.session || S.style === "ptt")) setStatus("muted");
  else if (S.session) setStatus("listening");
  else setStatus("off");
}

// ------------------------------------------------------------------ conversation rendering
function clearTranscript() { $("#transcript").innerHTML = ""; }
function showEmpty(v) {
  const t = $("#transcript");
  if (v && !t.children.length) t.innerHTML = $("#empty-template")?.innerHTML || `<div class="empty" id="empty-state"><div class="orb-hero"></div><h1>Talk to your AI — online or offline</h1><p>Press the microphone and start speaking.</p></div>`;
  if (!v) $("#empty-state")?.remove();
}

function addMsg(role, text, meta = {}) {
  showEmpty(false);
  const row = document.createElement("div");
  row.className = "msg-row " + role;
  const b = document.createElement("div"); b.className = "bubble"; b.textContent = text;
  const m = document.createElement("div"); m.className = "meta";
  row.append(b, m);
  $("#transcript").appendChild(row);
  setMeta(row, meta);
  scrollDown();
  return row;
}
function setMeta(row, meta) {
  const m = row.querySelector(".meta"); const parts = [];
  if (meta.kind) parts.push(`<span class="badge ${meta.kind === "cloud" ? "cloud" : "local"}">${meta.kind === "cloud" ? "☁ " + esc(meta.label || meta.provider) : "🔒 on this " + dev()}</span>`);
  if (meta.model) parts.push(`<span>${esc(meta.model)}</span>`);
  if (meta.stt) parts.push(`<span>${esc(meta.stt)}</span>`);
  if (meta.interrupted) parts.push(`<span class="badge int">interrupted</span>`);
  if (meta.timing) parts.push(`<span>${esc(meta.timing)}</span>`);
  m.innerHTML = parts.join("");
}
function scrollDown() { const t = $("#transcript"); t.scrollTop = t.scrollHeight; }

// ------------------------------------------------------------------ turns
async function sendAudio(frames, sr) {
  const blob = await encodeWav16k(frames, sr);
  if (S.settings?.ui?.review_voice !== false) return reviewAudio(blob);
  runTurn(blob, "audio/wav", null);
}

/** "Check before sending": write down what was said into the message box; nothing is sent until the user presses Send. */
async function reviewAudio(blob) {
  interrupt("new-turn");
  if (S.session) { S.session = false; vad.reset(); mic.stop(); }   // stop listening while the user checks the words
  setStatus("thinking", "Writing down what you said…");
  try {
    const r = await api("/api/transcribe", { method: "POST", body: blob, headers: { "Content-Type": "audio/wav" } });
    const heard = (r.text || "").trim();
    if (!heard) { toast("Didn't catch that — please try again"); return; }
    const box = $("#text-in");
    box.value = box.value.trim() ? box.value.trim() + " " + heard : heard;
    showReview(true);
    box.focus(); box.setSelectionRange(box.value.length, box.value.length);
  } catch (e) {
    toast(e.message || "Couldn't recognise the speech");
  } finally {
    afterActivity();
    if (S.status === "off" && $("#text-in").value) $("#live-caption").textContent = "Check the message below, then press Send";
  }
}
function showReview(on) {
  $("#review-hint").hidden = !on;
  $("#text-form").classList.toggle("review", on);
}
function sendText(text) {
  showReview(false);
  if (!text.trim()) return;
  player.ensure();
  runTurn(JSON.stringify({ text, conversation_id: S.convId }), "application/json", text);
}

function interrupt(reason) {
  const t = S.turn;
  player.stop();
  if (t && !t.done) {
    t.done = true; t.cancelled = true;
    if (t.id) api("/api/cancel", { method: "POST", json: { turn_id: t.id } }).catch(() => {});
    try { t.ctrl.abort(); } catch (_) {}
    if (t.botRow) { setMeta(t.botRow, { ...t.meta, interrupted: true }); t.botRow.querySelector(".bubble").classList.remove("cursor"); }
    if (t.userRow && t.userRow.querySelector(".bubble.pending")) t.userRow.remove();
  }
  S.turn = null;
  if (reason !== "barge-in") afterActivity();
}

async function runTurn(body, ctype, typedText) {
  interrupt("new-turn");
  player.reset();
  const t = { id: null, ctrl: new AbortController(), done: false, text: "", meta: {}, userRow: null, botRow: null };
  S.turn = t;
  t.userRow = addMsg("user", typedText ?? "…", {});
  if (typedText == null) t.userRow.querySelector(".bubble").classList.add("pending");
  setStatus("thinking");
  let res;
  try {
    const q = S.convId ? "?conversation_id=" + encodeURIComponent(S.convId) : "";
    res = await fetch("/api/turn" + q, { method: "POST", body, headers: { "X-App-Token": TOKEN, "Content-Type": ctype }, signal: t.ctrl.signal });
  } catch (e) {
    if (!t.cancelled) failTurn(t, "Could not reach the VoiceAssist engine: " + e.message, "Is the app still running? Restart it from the Start menu shortcut.");
    return;
  }
  if (!res.ok) { const d = await res.json().catch(() => ({})); failTurn(t, d.error || res.statusText, d.hint); return; }
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (line.trim()) handleEvent(t, JSON.parse(line));
      }
    }
  } catch (e) {
    if (!t.cancelled) failTurn(t, "Connection to the engine was interrupted.", "");
  }
  if (S.turn === t) { t.done = true; player.turnDone = true; if (!player.speaking) { S.turn = null; } afterActivity(); }
}

function failTurn(t, msg, hint) {
  t.done = true;
  if (t.userRow && t.userRow.querySelector(".bubble.pending")) t.userRow.remove();
  const row = t.botRow || addMsg("assistant", "");
  const b = row.querySelector(".bubble"); b.classList.remove("cursor"); b.classList.add("error");
  b.innerHTML = esc(msg) + (hint ? `<span class="hint">${esc(hint)}</span>` : "");
  if (S.turn === t) S.turn = null;
  afterActivity();
}

function handleEvent(t, ev) {
  if (t.cancelled || S.turn !== t) return;
  switch (ev.type) {
    case "accepted": t.id = ev.turn_id; break;
    case "route": updateRoute({ route: ev.route, reason: ev.reason }); break;
    case "transcript": {
      const b = t.userRow.querySelector(".bubble"); b.classList.remove("pending");
      b.textContent = ev.text || "…"; setMeta(t.userRow, { stt: `${ev.engine}${ev.seconds ? " · " + ev.seconds.toFixed(2) + "s" : ""}` });
      break;
    }
    case "no_speech":
      t.userRow.remove(); t.userRow = null; toast("Didn't catch that — please try again"); break;
    case "user_saved":
      if (S.convId !== ev.conversation_id) { S.convId = ev.conversation_id; loadConversations(); }
      break;
    case "llm_start":
      t.meta = { kind: ev.kind, provider: ev.provider, label: ev.label, model: ev.model };
      if (!t.botRow) t.botRow = addMsg("assistant", "", t.meta); else setMeta(t.botRow, t.meta);
      t.botRow.querySelector(".bubble").classList.add("cursor");
      break;
    case "token":
      t.text += ev.text; t.botRow.querySelector(".bubble").textContent = t.text; scrollDown(); break;
    case "audio": player.enqueue(ev.id, ev.seq); break;
    case "say": player.enqueueSay(ev.seq, ev.text, ev.voice, ev.rate); break;
    case "failover":
      banner("failover", "warn", ev.message, { hint: ev.hint || "", timeout: 9000 });
      if (ev.reset && t.botRow) { player.stop(); player.next = 0; t.text = ""; t.botRow.querySelector(".bubble").textContent = ""; }
      break;
    case "tts_error": banner("tts", "warn", ev.message, { hint: ev.hint }); break;
    case "error": failTurn(t, ev.message, ev.hint); break;
    case "cancelled": break;
    case "done": {
      t.done = true; player.turnDone = true;
      if (t.botRow) {
        t.botRow.querySelector(".bubble").classList.remove("cursor");
        const tm = ev.timings || {};
        const timing = tm.first_audio_s != null ? `voice in ${tm.first_audio_s}s` : tm.total_s != null ? `${tm.total_s}s` : "";
        setMeta(t.botRow, { ...t.meta, timing });
      }
      if (!player.speaking && player.queue.size === 0) { S.turn = null; }
      player.pump();
      afterActivity();
      break;
    }
  }
}

// ------------------------------------------------------------------ status: network / route / privacy
function updateNetwork(net) {
  S.net = net;
  const pill = $("#net-pill");
  const st = net.state || "UNKNOWN";
  pill.className = "net-pill " + { ONLINE: "online", OFFLINE: "offline", AIRPLANE: "airplane" }[st] ?? "unknown";
  if (!["ONLINE", "OFFLINE", "AIRPLANE"].includes(st)) pill.className = "net-pill unknown";
  $("#net-label").textContent = st === "AIRPLANE" ? "AIRPLANE MODE" : st === "UNKNOWN" ? "LOCAL" : st;
  $("#net-icon").innerHTML = st === "AIRPLANE" ? `<svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M10.5 21l1.5-1 1.5 1v-2.5L12 17.5V13l8 3v-2l-8-5V4.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-3v4.5l-1.5 1z" fill="currentColor" stroke="none" transform="translate(1.5 0)"/></svg>` : "";
  pill.title = net.detail || "";
  const d = $("#net-details");
  if (d) d.innerHTML = `<dt>Network</dt><dd>${esc(st)}</dd><dt>Detail</dt><dd>${esc(net.detail)}</dd><dt>Airplane mode</dt><dd>${net.airplane == null ? "not reported by OS" : net.airplane ? "on" : "off"}</dd><dt>Adapters up</dt><dd>${net.adapters_up == null ? "unknown" : net.adapters_up}</dd><dt>Cloud reachable</dt><dd>${net.internet == null ? "not checked" : net.internet}</dd>`;
}

function updateRoute(r) {
  S.route = r;
  const st = S.settings; if (!st) return;
  const el = $("#route-label");
  if (r.route === "online") {
    const p = st.online.provider, model = st.online.models[p];
    el.innerHTML = `Answering with <b>${esc(model)}</b> · cloud`;
  } else if (WEB()) {
    el.innerHTML = `<b>Offline</b> — reconnect to the internet to keep talking`;
  } else {
    el.innerHTML = `Answering with <b>${esc(st.local.model)}</b> · on this ${dev()} <span class="muted">(${esc(r.reason)})</span>`;
  }
  el.title = r.reason;
  updatePrivacyLine();
  syncModelSelect();
}

function updatePrivacyLine() {
  const st = S.settings; if (!st || !S.route) return;
  const providerName = { anthropic: "Anthropic (Claude)", openai: "OpenAI", gemini: "Google (Gemini)" }[st.online.provider];
  let html;
  if (WEB()) {
    html = S.route.route === "offline" ? "⚠ <b>No internet.</b> The website works online only — reconnect, or use the installed app for offline use."
      : `☁ Your question is sent to <b>${esc(providerName)}</b>. Your account, history and API keys stay in this browser.`;
    $("#privacy-line").innerHTML = html;
    const pb = $("#privacy-box");
    if (pb) pb.innerHTML = `<b>How your data is handled (website)</b><ul>
      <li>Your account, conversation history and API keys are stored only in this browser on this device. Nothing is stored on a VoiceAssist server.</li>
      <li>The text of your question and recent conversation goes directly from this browser to ${esc(providerName)}.</li>
      <li>Voice input is recognised by OpenAI or Gemini when you've added that key, otherwise by Chrome's speech service (Google).</li>
      <li>Replies are spoken by your browser's own voice${st.web?.tts === "openai" ? ", or by OpenAI if you chose that voice" : ""}.</li>
      <li>Clearing this site's data in your browser deletes everything.</li></ul>`;
    return;
  }
  if (S.route.route === "local") html = `🔒 <b>Everything stays on this ${devLong()}</b> — speech recognition, AI and voice run locally.`;
  else {
    const sttCloud = st.online.cloud_stt, ttsCloud = st.online.cloud_tts;
    html = `☁ Your recognised <b>text</b> is sent to <b>${esc(providerName)}</b>. ` +
      (sttCloud ? "Your <b>voice audio</b> is sent to OpenAI for recognition. " : `Your voice audio stays on this ${devLong()}. `) +
      (ttsCloud ? "Replies are voiced by OpenAI." : "Replies are voiced locally.");
  }
  $("#privacy-line").innerHTML = html;
  const pb = $("#privacy-box");
  if (pb) pb.innerHTML = `<b>How your data is handled</b><ul>
    <li><b>Offline / Local only:</b> audio, transcripts and answers never leave this ${devLong()}. The app only talks to 127.0.0.1.</li>
    <li><b>Online:</b> the text of your question and recent conversation is sent to the cloud provider you chose (${esc(providerName)}). Audio is sent only if you enable cloud speech.</li>
    <li>Recordings are ${st.privacy.save_recordings ? "<b>saved</b> to the recordings folder" : "<b>not saved</b> (kept in memory only while being recognised)"}.</li>
    <li>History is ${st.privacy.save_history ? "saved locally in a SQLite file" : "kept in memory only and erased on exit"}.</li>
    <li>API keys live in the Windows Credential Manager or environment variables — never in the settings file.</li></ul>`;
}

// ------------------------------------------------------------------ model selector
function syncModelSelect() {
  const st = S.settings; if (!st) return;
  const sel = $("#model-select");
  const cur = S.route?.route === "online" ? `cloud|${st.online.provider}|${st.online.models[st.online.provider]}` : `local|${st.local.model}`;
  const labels = { anthropic: "Claude", openai: "OpenAI", gemini: "Gemini" };
  let html = "";
  for (const p of ["anthropic", "openai", "gemini"]) {
    const choices = new Set([...(st.online.model_choices[p] || []), st.online.models[p]]);
    const keyOk = S.state?.keys?.[p]?.configured;
    html += `<optgroup label="☁ ${labels[p]}${keyOk ? "" : " (no API key)"}">` +
      [...choices].map((m) => `<option value="cloud|${p}|${esc(m)}">${esc(m)}</option>`).join("") + "</optgroup>";
  }
  const locals = new Set([...(S.localModels || []), st.local.model]);
  if (!WEB()) html += `<optgroup label="🔒 On this ${devLong()}">` + [...locals].map((m) => `<option value="local|${esc(m)}">${esc(m)}${S.localModels.includes(m) ? "" : " (not installed)"}</option>`).join("") + "</optgroup>";
  sel.innerHTML = html; sel.value = cur;
  $$("#mode-seg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === st.mode));
}

async function onModelSelect(v) {
  const parts = v.split("|");
  if (parts[0] === "cloud") {
    const [, p, ...rest] = parts; const m = rest.join("|");
    await saveSettings({ mode: "auto", online: { provider: p, models: { [p]: m } } });
    if (!S.state.keys[p].configured) { toast("Add an API key in Settings → Online AI to use " + m); openSettings("online"); }
    else if (S.route?.route !== "online") toast(`${m} will be used when the internet is available`);
  } else {
    await saveSettings({ mode: "local", local: { model: parts.slice(1).join("|") } });
    toast("Local only — answers come from " + parts.slice(1).join("|") + ` on this ${devLong()}`);
  }
}

// ------------------------------------------------------------------ settings
async function saveSettings(patch) {
  try {
    const r = await api("/api/settings", { method: "POST", json: patch });
    S.settings = r.settings;
    await refreshState();
  } catch (e) { toast("Setting not saved: " + e.message); }
}

function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function patchFor(path, value) { const parts = path.split("."); const root = {}; let o = root; parts.slice(0, -1).forEach((k) => (o = o[k] = {})); o[parts.at(-1)] = value; return root; }

function fillSettingsForm() {
  const st = S.settings;
  for (const el of $$("[data-setting]")) {
    const v = getPath(st, el.dataset.setting);
    if (el.type === "checkbox") el.checked = !!v;
    else if (document.activeElement !== el) el.value = v ?? "";
  }
  $("#speed-val").textContent = Number(st.tts.speed).toFixed(2) + "×";
  $("#sens-val").textContent = Math.round(st.ui.vad_sensitivity * 100) + "%";
  for (const p of ["anthropic", "openai", "gemini"]) {
    const dl = $("#dl-" + p); dl.innerHTML = (st.online.model_choices[p] || []).map((m) => `<option value="${esc(m)}">`).join("");
  }
}

function bindSettingsForm() {
  for (const el of $$("[data-setting]")) {
    const handler = () => {
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "number" || el.type === "range") v = Number(v);
      saveSettings(patchFor(el.dataset.setting, v));
    };
    if (el.type === "range") {
      el.addEventListener("input", debounce(handler, 250));
      el.addEventListener("input", () => { if (el.dataset.setting === "tts.speed") $("#speed-val").textContent = Number(el.value).toFixed(2) + "×"; else $("#sens-val").textContent = Math.round(el.value * 100) + "%"; });
    } else el.addEventListener("change", handler);
  }
  for (const row of $$(".key-row")) {
    row.querySelector("button").onclick = async () => {
      const key = row.querySelector("input").value;
      try {
        await api("/api/keys", { method: "POST", json: { provider: row.dataset.provider, key } });
        row.querySelector("input").value = ""; toast(key ? "API key saved securely" : "API key removed");
        await refreshState();
      } catch (e) { toast(e.message, 5000); }
    };
  }
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function renderStatusPanels() {
  const s = S.state; if (!s) return;
  // keys
  for (const row of $$(".key-row")) {
    const k = s.keys[row.dataset.provider]; const em = row.querySelector("em");
    em.textContent = k.configured ? `✓ configured (${k.source})` : "not configured";
    em.className = k.configured ? "ok" : "";
  }
  if (!s.credential_store) $$(".key-row em").forEach((em) => { if (!em.classList.contains("ok")) em.textContent = "no credential store — use environment variables"; });
  // stt models
  const sttSel = $("#stt-model-select");
  sttSel.innerHTML = Object.entries(s.catalog.stt).map(([k, m]) => `<option value="${k}">${k} — ${esc(m.note)}${s.stt.available.includes(k) ? "" : " (not downloaded)"}</option>`).join("");
  sttSel.value = s.settings.stt.model;
  $("#stt-status").innerHTML = `<dt>Model</dt><dd>${esc(s.stt.model)} ${s.stt.installed ? "✓ installed" : "✗ not installed"}</dd><dt>Loaded</dt><dd>${s.stt.loaded}</dd><dt>Silence trimming (Silero VAD)</dt><dd>${s.stt.vad ? "on" : "model not installed"}</dd>${s.stt.error ? `<dt>Error</dt><dd>${esc(s.stt.error)}</dd>` : ""}`;
  // voices
  const vSel = $("#voice-select");
  const voices = new Set([...Object.keys(s.catalog.voices), ...s.tts.voices]);
  vSel.innerHTML = [...voices].map((v) => `<option value="${v}">${v}${s.tts.voices.includes(v) ? "" : " (not downloaded)"}</option>`).join("");
  vSel.value = s.settings.tts.voice;
  $("#tts-status").innerHTML = `<dt>Active voice</dt><dd>${esc(s.tts.active)}</dd><dt>System voices</dt><dd>${s.tts.system_available ? "available" : "not available"}</dd>${s.tts.error ? `<dt>Error</dt><dd>${esc(s.tts.error)}</dd>` : ""}`;
  // local
  renderLocal(s.local);
  $("#history-mode").textContent = s.settings.privacy.save_history ? `Saved on this ${devLong()}` : "Not saved (memory only)";
  $("#paths-box").innerHTML = `<dt>Data folder</dt><dd>${esc(s.data_dir)}</dd><dt>Models</dt><dd>${esc(s.models_dir)}</dd>`;
  $("#diag-json").textContent = JSON.stringify({ network: s.network, route: s.route, stt: s.stt, tts: s.tts, local: s.local }, null, 2);
}

function renderLocal(l) {
  S.localModels = l.installed || [];
  const sel = $("#local-model-select");
  const all = new Set([...(l.installed || []), ...(l.recommended || []).map((r) => r.tag), S.settings.local.model]);
  sel.innerHTML = [...all].map((m) => `<option value="${esc(m)}">${esc(m)}${(l.installed || []).includes(m) ? " ✓" : " (not installed)"}</option>`).join("");
  sel.value = S.settings.local.model;
  const stateTxt = { ready: "✓ loaded and ready", loading: "loading…", missing: "✗ not installed", unavailable: "✗ engine not running", error: "✗ error" }[l.state] || l.state;
  $("#local-status").innerHTML = `<dt>Engine</dt><dd>${l.running ? "running" : "not running"}</dd><dt>Model</dt><dd>${esc(S.settings.local.model)} — ${esc(stateTxt)}</dd>${l.message && l.state !== "ready" ? `<dt>Detail</dt><dd>${esc(l.message)}</dd>` : ""}${l.hint ? `<dt>Fix</dt><dd><code>${esc(l.hint)}</code></dd>` : ""}`;
  $("#local-reco").innerHTML = "Recommended local models: " + (l.recommended || []).map((r) => `<code>${esc(r.tag)}</code> ${esc(r.note)}`).join(" · ");
}

function onLocalModelStatus(ev) {
  if (!S.settings) return;
  const routeLocal = S.route?.route === "local";
  if (ev.state === "missing" || ev.state === "unavailable" || ev.state === "error") {
    banner("local", routeLocal ? "error" : "warn", (routeLocal ? "" : "Offline mode not ready: ") + ev.message, { actions: [
      { label: "Set up offline AI", primary: true, onClick: openSetup, keep: true },
      { label: "Retry", onClick: () => api("/api/local/preload", { method: "POST" }) }] });
  } else removeBanner("local");
  if (S.state) { S.state.local = { ...S.state.local, ...ev }; renderLocal(S.state.local); }
}

async function refreshState() {
  const s = await api("/api/state");
  setPlatform(s.platform);
  S.state = s; S.settings = s.settings;
  updateNetwork(s.network); updateRoute(s.route);
  fillSettingsForm(); renderStatusPanels(); syncModelSelect();
  S.style = s.settings.ui.conversation_style;
  $$("#style-seg button").forEach((b) => b.classList.toggle("active", b.dataset.style === S.style));
  if (s.local.state && s.local.state !== "unknown") onLocalModelStatus(s.local);
  return s;
}

// ------------------------------------------------------------------ devices
async function refreshDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const devs = await navigator.mediaDevices.enumerateDevices();
  const mics = devs.filter((d) => d.kind === "audioinput"), spks = devs.filter((d) => d.kind === "audiooutput");
  const ms = $("#mic-select"), ss = $("#spk-select");
  ms.innerHTML = `<option value="">System default</option>` + mics.filter((d) => d.deviceId !== "default").map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || "Microphone " + (i + 1))}</option>`).join("");
  ss.innerHTML = `<option value="">System default</option>` + spks.filter((d) => d.deviceId !== "default").map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || "Speaker " + (i + 1))}</option>`).join("");
  ms.value = S.settings?.ui.mic_device || ""; ss.value = S.settings?.ui.speaker_device || "";
  const canSink = typeof AudioContext.prototype.setSinkId === "function";
  $("#spk-note").textContent = !mics.length ? "No microphone detected." : (!mics[0].label ? "Device names appear after you allow microphone access." : (canSink ? "" : "Speaker selection isn't supported by this window — the system default speaker is used."));
}

// ------------------------------------------------------------------ waveform
function drawLoop() {
  const c = $("#wave"), g = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const data = new Uint8Array(1024);
  function frame() {
    if ($("#voice-panel").hidden) { requestAnimationFrame(frame); return; }
    const w = c.clientWidth, h = c.clientHeight;
    if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    let an = null, color = "#2a3048";
    if (player.speaking && player.analyser) { an = player.analyser; color = "#22c55e"; }
    else if (mic.analyser && !S.muted && (S.session || ptt)) { an = mic.analyser; color = S.status === "hearing" || ptt ? "#22d3ee" : "#7c5cff"; }
    let level = 0;
    g.lineWidth = 2; g.strokeStyle = color; g.beginPath();
    if (an) {
      an.getByteTimeDomainData(data);
      const n = an.fftSize;
      for (let i = 0; i < n; i++) {
        const v = (data[i] - 128) / 128; level = Math.max(level, Math.abs(v));
        const x = (i / (n - 1)) * w, y = h / 2 + v * h * 0.45 * 1.6;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
    } else { g.moveTo(0, h / 2); g.lineTo(w, h / 2); }
    g.stroke();
    $("#btn-mic").style.setProperty("--lvl", Math.min(1, level * 2.5).toFixed(3));
    // level meter in Audio settings
    const lb = $("#level-bar");
    if (lb && $("#drawer").classList.contains("open")) {
      const pct = (db) => Math.max(0, Math.min(100, ((db + 80) / 80) * 100));
      lb.style.width = pct(mic.active ? lastDb : -100) + "%";
      $("#thresh-mark").style.left = pct(vad.threshold(false)) + "%";
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// ------------------------------------------------------------------ conversations sidebar
async function loadConversations() {
  try {
    const { conversations } = await api("/api/conversations");
    $("#conv-list").innerHTML = conversations.map((c) => `<li data-id="${c.id}" class="${c.id === S.convId ? "active" : ""}"><span title="${esc(c.title)}">${esc(c.title)}</span><button title="Delete" aria-label="Delete conversation">✕</button></li>`).join("") || `<li class="muted small">No conversations yet</li>`;
  } catch (e) { console.warn(e); }
}
async function openConversation(id) {
  interrupt("switch");
  S.convId = id; clearTranscript();
  const { messages } = await api("/api/conversations/" + id);
  if (!messages.length) showEmpty(true);
  for (const m of messages) addMsg(m.role, m.content, m.role === "assistant" ? m.meta : { stt: m.meta.input === "voice" ? m.meta.stt : "typed" });
  loadConversations();
}
function newConversation() { interrupt("new"); S.convId = null; clearTranscript(); showEmpty(true); loadConversations(); }

// ------------------------------------------------------------------ settings drawer
function openSettings(tab) {
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false"); $("#drawer-backdrop").hidden = false;
  if (tab) selectTab(tab);
  refreshState().catch(() => {});
  refreshSetup();
  refreshDevices();
  api("/api/voices").then((v) => {
    const sel = $("#sysvoice-select");
    sel.innerHTML = `<option value="">Default system voice</option>` + v.system.map((n) => `<option>${esc(n)}</option>`).join("");
    sel.value = S.settings.tts.system_voice || "";
  }).catch(() => {});
}
function closeSettings() { $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); $("#drawer-backdrop").hidden = true; }
function selectTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
}

async function runSelftest() {
  const out = $("#selftest-out"); out.innerHTML = `<div class="st">Running… (first run may take a few seconds while models load)</div>`;
  try {
    const r = await api("/api/selftest", { method: "POST" });
    out.innerHTML = r.steps.map((s) => `<div class="st ${s.ok ? "ok" : "bad"}">${s.ok ? "✓" : "✗"} <b>${esc(s.name)}</b> — ${s.seconds}s<br>${esc(s.ok ? (typeof s.detail === "string" ? s.detail : "") : s.error)}${s.hint ? `<br><code>${esc(s.hint)}</code>` : ""}</div>`).join("") +
      `<div class="st ${r.ok ? "ok" : "bad"}"><b>${r.ok ? `Offline voice loop works on this ${devLong()}.` : "Some offline components are missing — see above."}</b></div>`;
  } catch (e) { out.innerHTML = `<div class="st bad">${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ events from the backend
function connectEvents() {
  const es = new EventSource("/api/events?token=" + encodeURIComponent(TOKEN));
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "network") {
      const prev = S.net?.state;
      updateNetwork(ev); updateRoute({ route: ev.route, reason: ev.route_reason });
      if (prev && prev !== ev.state) {
        if (ev.state === "ONLINE") toast("Back online");
        else toast(WEB() ? "You're offline — reconnect to keep talking" : ev.state === "AIRPLANE" ? "Airplane mode — switched to on-device AI" : "Offline — switched to on-device AI", 3500);
      }
      if (ev.kind === "auto_switched_back") toast("Internet is back — using the cloud model again");
      if (ev.switch_back_offered) offerSwitchBack(); else removeBanner("switchback");
    } else if (ev.type === "switch_back_offer") offerSwitchBack();
    else if (ev.type === "local_model") onLocalModelStatus(ev);
    else if (ev.type === "setup") { renderSetupJob(ev); if (["done", "error"].includes(ev.state)) refreshSetup(); }
    else if (ev.type === "speech_models") {
      if (ev.stt_error) banner("stt", "error", ev.stt_error.message, { actions: [{ label: "Set up offline AI", primary: true, onClick: openSetup, keep: true }] });
      else removeBanner("stt");
      if (ev.tts_error) banner("tts", "warn", ev.tts_error.message, { hint: ev.tts_error.hint }); else removeBanner("tts");
      if (S.state) { S.state.stt = ev.stt || S.state.stt; S.state.tts = ev.tts || S.state.tts; renderStatusPanels(); }
    }
  };
  es.onerror = () => { /* EventSource reconnects automatically */ };
}

function offerSwitchBack() {
  const p = S.settings?.online.provider; const name = { anthropic: "Claude", openai: "OpenAI", gemini: "Gemini" }[p] || "the cloud model";
  banner("switchback", "info", `Internet is back. Switch to ${name} (cloud), or keep using the local model?`, {
    actions: [
      { label: `Switch to ${name}`, primary: true, onClick: () => api("/api/switch_back", { method: "POST", json: { accept: true } }).then(refreshState) },
      { label: "Stay local", onClick: () => api("/api/switch_back", { method: "POST", json: { accept: false } }).then(refreshState) },
    ],
  });
}

// ------------------------------------------------------------------ voice panel (collapsible)
function setVoicePanel(open, save = true) {
  $("#voice-panel").hidden = !open;
  const vt = $("#btn-voice-toggle");
  vt.setAttribute("aria-expanded", String(open));
  vt.title = (open ? "Hide" : "Show") + " voice controls";
  if (save && S.settings) saveSettings({ ui: { voice_panel_open: open } });
}

// ------------------------------------------------------------------ wiring
function setStyle(style) {
  S.style = style;
  $$("#style-seg button").forEach((b) => b.classList.toggle("active", b.dataset.style === style));
  if (style === "ptt") { S.session = false; vad.reset(); }
  saveSettings({ ui: { conversation_style: style } });
  afterActivity();
}

async function pttStart() {
  if (S.style !== "ptt" || ptt) return;
  if (!(await ensureMic())) return;
  if (S.muted) { toast("Microphone is muted"); return; }
  interrupt("ptt"); player.ensure();
  ptt = { frames: [], t0: performance.now() };
  setStatus("ptt");
}
function pttEnd() {
  if (!ptt) return;
  const p = ptt; ptt = null;
  const dur = performance.now() - p.t0;
  if (dur < 300 || !p.frames.length) { afterActivity(); toast("Hold to talk, release to send"); return; }
  sendAudio(p.frames, mic.sr);
}

function wire() {
  const micBtn = $("#btn-mic");
  micBtn.addEventListener("click", () => {
    if (S.style === "ptt") return;
    S.session ? stopSession() : startSession();
  });
  micBtn.addEventListener("pointerdown", (e) => { if (S.style === "ptt") { e.preventDefault(); micBtn.setPointerCapture(e.pointerId); pttStart(); } });
  micBtn.addEventListener("pointerup", () => S.style === "ptt" && pttEnd());
  micBtn.addEventListener("pointercancel", () => S.style === "ptt" && pttEnd());

  $("#btn-mute").onclick = () => {
    S.muted = !S.muted; mic.setMuted(S.muted);
    $("#btn-mute").classList.toggle("muted", S.muted);
    $("#btn-mute").title = S.muted ? "Unmute microphone (M)" : "Mute microphone (M)";
    if (S.muted) vad.reset();
    afterActivity();
  };
  $("#btn-stop").onclick = () => interrupt("stop-button");
  $("#btn-voice-toggle").onclick = () => setVoicePanel($("#voice-panel").hidden);
  $$("#style-seg button").forEach((b) => (b.onclick = () => setStyle(b.dataset.style)));
  $$("#mode-seg button").forEach((b) => (b.onclick = () => saveSettings({ mode: b.dataset.mode })));
  $("#model-select").onchange = (e) => onModelSelect(e.target.value);
  $("#text-form").onsubmit = (e) => { e.preventDefault(); const v = $("#text-in").value; $("#text-in").value = ""; sendText(v); };
  $("#btn-review-discard").onclick = () => { $("#text-in").value = ""; showReview(false); };
  $("#text-in").addEventListener("input", () => { if (!$("#text-in").value) showReview(false); });

  $("#btn-settings").onclick = () => openSettings();
  $("#btn-close-settings").onclick = closeSettings;
  $("#drawer-backdrop").onclick = closeSettings;
  $$("#tabs button").forEach((b) => (b.onclick = () => selectTab(b.dataset.tab)));
  $("#btn-sidebar").onclick = () => $("#sidebar").classList.toggle("open");

  $("#btn-new").onclick = newConversation;
  $("#conv-list").onclick = async (e) => {
    const li = e.target.closest("li[data-id]"); if (!li) return;
    if (e.target.tagName === "BUTTON") {
      await api("/api/conversations/" + li.dataset.id, { method: "DELETE" });
      if (li.dataset.id === S.convId) newConversation(); else loadConversations();
    } else openConversation(li.dataset.id);
  };
  const clearAll = async () => {
    if (!confirm(`Delete all conversation history from this ${devLong()}?`)) return;
    await api("/api/history/clear", { method: "POST" }); newConversation(); toast("History deleted");
  };
  $("#btn-clear-history").onclick = clearAll; $("#btn-clear-history-2").onclick = clearAll;

  $("#btn-recheck").onclick = async () => { updateNetwork(await api("/api/network/recheck", { method: "POST" })); refreshState(); };
  $("#btn-refresh-local").onclick = async () => { try { renderLocal({ ...S.state.local, ...(await api("/api/local/models")) }); } catch (e) { toast(e.message); } };
  $("#local-model-select").onchange = (e) => saveSettings({ local: { model: e.target.value } });
  $("#btn-preload").onclick = () => { api("/api/local/preload", { method: "POST" }); toast("Loading local model…"); };
  $("#btn-test-voice").onclick = async () => {
    try {
      const res = await api("/api/tts/test", { method: "POST", json: { text: "Hello! This is how I will sound when I answer you." }, raw: true });
      if (!res.ok) { const d = await res.json(); throw Object.assign(new Error(d.error), { hint: d.hint }); }
      player.ensure(); player.reset();
      if ((res.headers.get("content-type") || "").includes("json")) {   // website: browser voice
        const d = await res.json(); player.turnDone = true;
        player.enqueueSay(0, d.say, S.settings.web?.browser_voice, S.settings.tts.speed); return;
      }
      const b = await player.ctx.decodeAudioData(await res.arrayBuffer());
      player.queue.set(0, b); player.turnDone = true; player.pump();
    } catch (e) { toast("Voice test failed: " + e.message, 5000); }
  };
  $("#mic-select").onchange = async (e) => {
    await saveSettings({ ui: { mic_device: e.target.value } });
    if (mic.active) { const was = S.session; mic.stop(); await ensureMic(); S.session = was; afterActivity(); }
  };
  $("#spk-select").onchange = async (e) => { await saveSettings({ ui: { speaker_device: e.target.value } }); player.setSink(e.target.value); };
  $("#btn-selftest").onclick = runSelftest;
  $("#btn-quit").onclick = async () => { await api("/api/quit", { method: "POST" }).catch(() => {}); window.close(); };
  navigator.mediaDevices?.addEventListener?.("devicechange", refreshDevices);

  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
    if (e.key === "Escape") { if ($("#drawer").classList.contains("open")) closeSettings(); else interrupt("esc"); }
    if (typing) return;
    if (e.code === "Space" && S.style === "ptt" && !e.repeat) { e.preventDefault(); pttStart(); }
    if (e.key === "m" || e.key === "M") $("#btn-mute").click();
  });
  document.addEventListener("keyup", (e) => { if (e.code === "Space" && S.style === "ptt") pttEnd(); });

}

// ------------------------------------------------------------------ accounts: sign-in page
function authTab(name) {
  $$("[data-auth-panel]").forEach((f) => (f.hidden = f.dataset.authPanel !== name));
  $$("[data-auth-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.authTab === name || (name === "recover" && b.dataset.authTab === "login"))));
  const form = $(`[data-auth-panel="${name}"]`);
  form.querySelector(".auth-error").textContent = "";
  setTimeout(() => form.querySelector("input:not([type=checkbox])")?.focus(), 30);
}

function showAuth(hasAccounts) {
  S.user = null;
  $("#app").hidden = true;
  $("#auth-view").hidden = false;
  $("#signup-note").textContent = hasAccounts
    ? `Your account is stored only on this ${devLong()}.`
    : `Create the first account on this ${devLong()}. Conversations you've already had will be moved into it.`;
  authTab(hasAccounts ? "login" : "signup");
}

function signedOut() {
  // session ended (logged out, expired, or password changed elsewhere): reload to a clean state, once
  if (S.leaving) return;
  S.leaving = true; S.user = null;
  location.reload();
}

function formData(form) {
  const d = {};
  for (const el of form.elements) if (el.name) d[el.name] = el.type === "checkbox" ? el.checked : el.value;
  return d;
}

function formError(form, err, fallbackField) {
  form.querySelectorAll(".invalid").forEach((x) => x.classList.remove("invalid"));
  const m = form.querySelector(".auth-error, .acct-msg");
  m.textContent = err.message || String(err);
  if (m.classList.contains("acct-msg")) m.className = "acct-msg bad";
  const f = form.elements[err.field || fallbackField];
  if (f && f.classList) { f.classList.add("invalid"); f.focus(); }
}

function passwordScore(pw) {
  if (!pw) return 0;
  let s = 0;
  if (pw.length >= 8) s++;
  if (pw.length >= 12) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) s++;
  if (pw.length < 8 || !/[A-Za-z]/.test(pw) || !/[^A-Za-z]/.test(pw)) s = Math.min(s, 1);
  return Math.max(1, Math.min(4, s));
}

function showRecoveryCode(code, username) {
  return new Promise((resolve) => {
    const d = $("#recovery-dialog");
    $("#rc-code").textContent = code;
    $("#rc-saved").checked = false; $("#rc-continue").disabled = true;
    $("#rc-saved").onchange = () => ($("#rc-continue").disabled = !$("#rc-saved").checked);
    $("#rc-copy").onclick = async () => { try { await navigator.clipboard.writeText(code); $("#rc-copy").textContent = "Copied ✓"; } catch (_) { $("#rc-copy").textContent = "Select & copy"; } };
    $("#rc-download").onclick = () => {
      const blob = new Blob([`VoiceAssist recovery code\r\nUsername: ${username}\r\nRecovery code: ${code}\r\n\r\nUse it on the sign-in page: Log in -> Forgot password?\r\n`], { type: "text/plain" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `VoiceAssist-recovery-${username}.txt`; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    };
    d.addEventListener("cancel", (e) => e.preventDefault(), { once: true }); // must confirm saving it
    $("#rc-continue").onclick = () => { d.close(); $("#rc-copy").textContent = "Copy"; resolve(); };
    d.showModal();
  });
}

async function submitAuth(form, url, body, { needCode = false } = {}) {
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const r = await api(url, { method: "POST", json: body });
    if (r.recovery_code) await showRecoveryCode(r.recovery_code, r.user.username);
    form.reset();
    startApp(r.user, r.claimed_conversations);
  } catch (e) { formError(form, e); }
  finally { btn.disabled = false; }
}

function wireAuth() {
  $$("[data-auth-tab]").forEach((b) => (b.onclick = () => authTab(b.dataset.authTab)));
  $$("[data-goto]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); authTab(a.dataset.goto); }));
  $("#link-forgot").onclick = (e) => { e.preventDefault(); authTab("recover"); };
  $$(".pw-eye").forEach((b) => (b.onclick = () => {
    const i = b.parentElement.querySelector("input"); const show = i.type === "password";
    i.type = show ? "text" : "password"; b.textContent = show ? "Hide" : "Show";
  }));
  const pw = $("#form-signup").elements.password;
  pw.addEventListener("input", () => { $("#form-signup .pw-meter").dataset.score = passwordScore(pw.value); });

  $("#form-login").onsubmit = (e) => {
    e.preventDefault(); const f = e.target, d = formData(f);
    if (!d.username.trim()) return formError(f, { message: "Enter your username.", field: "username" });
    if (!d.password) return formError(f, { message: "Enter your password.", field: "password" });
    submitAuth(f, "/api/auth/login", { username: d.username.trim(), password: d.password, remember: d.remember });
  };
  $("#form-signup").onsubmit = (e) => {
    e.preventDefault(); const f = e.target, d = formData(f);
    if (!d.username.trim()) return formError(f, { message: "Choose a username.", field: "username" });
    if (d.password !== d.confirm) return formError(f, { message: "The two passwords don't match.", field: "confirm" });
    submitAuth(f, "/api/auth/signup", { username: d.username.trim(), password: d.password, display_name: d.display_name, email: d.email, remember: d.remember });
  };
  $("#form-recover").onsubmit = (e) => {
    e.preventDefault(); const f = e.target, d = formData(f);
    if (d.new_password !== d.confirm) return formError(f, { message: "The two passwords don't match.", field: "confirm" });
    submitAuth(f, "/api/auth/recover", { username: d.username.trim(), code: d.code, new_password: d.new_password, remember: true });
  };
}

// ------------------------------------------------------------------ one-click offline setup
let setupStatus = null;
async function refreshSetup() {
  try { setupStatus = await api("/api/setup/status"); } catch (_) { return null; }
  const st = setupStatus, card = $("#setup-card"), txt = $("#setup-card-status");
  card.classList.toggle("ready", st.ready);
  txt.textContent = st.ready ? `Ready — ${st.model} works without internet` :
    !st.engine_installed ? "Not installed yet — download it once to use VoiceAssist offline" :
    !st.model_installed ? `AI model ${st.model} not downloaded yet` :
    !st.speech_installed ? "Speech models missing" : !st.engine_running ? "Installed but not running" : "";
  $("#btn-open-setup").textContent = st.ready ? "Change model" : "Set up offline AI";
  return st;
}

let setupJobTs = 0;
function renderSetupJob(job) {
  if (job && job.updated) { if (job.updated < setupJobTs) return; setupJobTs = job.updated; }  // ignore out-of-order updates
  const busy = job && !["idle", "done", "error"].includes(job.state);
  $("#setup-progress").hidden = !job || job.state === "idle";
  $("#setup-bar").style.width = Math.round((job?.progress || 0) * 100) + "%";
  $("#setup-step").textContent = job?.step || "";
  $("#setup-go").disabled = busy;
  $("#setup-model").disabled = busy;
  $("#setup-go").textContent = job?.state === "error" ? "Try again" : busy ? "Working…" : "Download & install";
  $("#setup-later").textContent = job?.state === "done" ? "Close" : busy ? "Hide (continues in background)" : "Later";
  if (job?.state === "done") { $("#setup-go").hidden = true; removeBanner("local"); refreshSetup(); refreshState().catch(() => {}); }
  else $("#setup-go").hidden = false;
}

async function openSetup() {
  const st = setupStatus || (await refreshSetup());
  if (!st) return;
  const sel = $("#setup-model");
  sel.innerHTML = st.choices.map((c) => `<option value="${esc(c.tag)}">${esc(c.tag)} — ${c.size_gb} GB · ${esc(c.note)}${st.installed_models.includes(c.tag) ? " ✓ downloaded" : ""}</option>`).join("");
  sel.value = st.model_installed ? st.model : st.recommended;
  $("#setup-intro").textContent = st.ready
    ? "Offline mode is ready. You can switch to a different on-device model here."
    : `Download the AI that runs on this ${devLong()}, so VoiceAssist keeps working without internet, even in airplane mode. One-time download${st.ram_gb ? ` (this ${dev()} has ${Math.round(st.ram_gb)} GB RAM)` : ""}; stay online${dev() === "phone" ? ", ideally on Wi-Fi, and keep the app open" : ""} until it finishes.`;
  renderSetupJob(st.job);
  const d = $("#setup-dialog");
  if (!d.open) d.showModal();
}

function wireSetup() {
  $("#btn-open-setup").onclick = openSetup;
  $("#setup-later").onclick = () => $("#setup-dialog").close();
  $("#setup-go").onclick = async () => {
    try { renderSetupJob(await api("/api/setup/start", { method: "POST", json: { model: $("#setup-model").value } })); }
    catch (e) { renderSetupJob({ state: "error", step: e.message, progress: 0 }); }
  };
}

// ------------------------------------------------------------------ accounts: inside the app
function renderUser() {
  const u = S.user; if (!u) return;
  const initials = (u.display_name || u.username).split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  $("#user-avatar").textContent = initials;
  $("#user-name").textContent = u.display_name;
  $("#user-handle").textContent = "@" + u.username;
  const fp = $("#form-profile");
  fp.elements.display_name.value = u.display_name; fp.elements.email.value = u.email || "";
  const fmt = (t) => (t ? new Date(t * 1000).toLocaleString() : "—");
  $("#account-info").innerHTML = `<dt>Username</dt><dd>${esc(u.username)}</dd><dt>Created</dt><dd>${fmt(u.created)}</dd><dt>Last login</dt><dd>${fmt(u.last_login)}</dd><dt>Stored</dt><dd>only on this ${devLong()}</dd>`;
}

async function logout() {
  if (S.leaving) return;
  S.leaving = true; S.user = null;  // ignore "not logged in" replies from requests still in flight
  interrupt("logout"); mic.stop();
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  location.reload();
}

function acctMsg(form, text, ok = true) {
  const m = form.querySelector(".acct-msg"); m.textContent = text; m.className = "acct-msg " + (ok ? "ok" : "bad");
}

function wireAccount() {
  const chip = $("#user-chip"), menu = $("#user-menu");
  chip.onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; chip.setAttribute("aria-expanded", String(!menu.hidden)); };
  document.addEventListener("click", () => { menu.hidden = true; chip.setAttribute("aria-expanded", "false"); });
  $("#menu-account").onclick = () => openSettings("account");
  $("#menu-logout").onclick = logout;
  $("#btn-logout").onclick = logout;

  $("#form-profile").onsubmit = async (e) => {
    e.preventDefault(); const f = e.target, d = formData(f);
    try { S.user = (await api("/api/account/profile", { method: "POST", json: { display_name: d.display_name, email: d.email } })).user; renderUser(); acctMsg(f, "Profile saved"); }
    catch (err) { formError(f, err); }
  };
  $("#form-password").onsubmit = async (e) => {
    e.preventDefault(); const f = e.target, d = formData(f);
    if (d.new_password !== d.confirm) return formError(f, { message: "The two new passwords don't match.", field: "confirm" });
    try { await api("/api/account/password", { method: "POST", json: { current_password: d.current_password, new_password: d.new_password } }); f.reset(); acctMsg(f, "Password changed. Other signed-in windows were logged out."); }
    catch (err) { formError(f, err, err.field === "password" ? "new_password" : undefined); }
  };
  $("#form-recovery").onsubmit = async (e) => {
    e.preventDefault(); const f = e.target, d = formData(f);
    try { const r = await api("/api/account/recovery", { method: "POST", json: { password: d.password } }); f.reset(); await showRecoveryCode(r.recovery_code, S.user.username); acctMsg(f, "New recovery code created — the old one no longer works."); }
    catch (err) { formError(f, err, "password"); }
  };
  $("#form-delete").onsubmit = async (e) => {
    e.preventDefault(); const f = e.target, d = formData(f);
    if (!d.sure) return formError(f, { message: "Tick the box to confirm.", field: "sure" });
    try { await api("/api/account/delete", { method: "POST", json: { password: d.password } }); S.leaving = true; S.user = null; location.reload(); }
    catch (err) { formError(f, err, "password"); }
  };
}

let appStarted = false;
async function startApp(user, claimed) {
  S.user = user;
  $("#auth-view").hidden = true;
  $("#app").hidden = false;
  renderUser();
  if (claimed) toast(`Your ${claimed} earlier conversation${claimed > 1 ? "s were" : " was"} moved into this account`, 4000);
  if (appStarted) return;
  appStarted = true;
  wire();
  wireAccount();
  wireSetup();
  bindSettingsForm();
  drawLoop();
  try {
    await refreshState();
  } catch (e) {
    banner("boot", "error", "Cannot reach the VoiceAssist engine.", { hint: "Start it with run.bat (or python -m voiceassist)." });
    return;
  }
  player.setSink(S.settings.ui.speaker_device);
  setVoicePanel(!!S.settings.ui.voice_panel_open, false);
  if (WEB() && window.speechSynthesis) {   // website: list the browser's own voices
    const fill = () => {
      const sel = $("#web-voice-select"), vs = speechSynthesis.getVoices();
      sel.innerHTML = `<option value="">Default voice</option>` + vs.map((v) => `<option value="${esc(v.name)}">${esc(v.name)} (${esc(v.lang)})${v.localService ? "" : " · online"}</option>`).join("");
      sel.value = S.settings.web?.browser_voice || "";
    };
    fill(); speechSynthesis.onvoiceschanged = fill;
  }
  connectEvents();
  loadConversations();
  setStatus("off");
  const st = await refreshSetup();
  if (st && !st.ready && st.job.state === "idle" && !sessionStorage.getItem("va_setup_offered")) {
    sessionStorage.setItem("va_setup_offered", "1");
    openSetup();
  }
}

(async function init() {
  // keep-alive for the backend + tell it when the window closes (also on the sign-in page)
  setInterval(() => api("/api/ping", { method: "POST" }).catch(() => {}), 20000);
  addEventListener("pagehide", () => navigator.sendBeacon("/api/bye?token=" + encodeURIComponent(TOKEN)));
  api("/api/ping", { method: "POST" }).catch(() => {});
  wireAuth();
  let me;
  try {
    me = await api("/api/auth/me");
  } catch (e) {
    const msg = e.status === 404
      ? "An older VoiceAssist engine is still running. Close this window, wait a few seconds and start VoiceAssist again — the new version replaces the old one automatically."
      : "Cannot reach the VoiceAssist engine. Start it from the Start menu, or with run.bat.";
    document.body.innerHTML = `<div style="padding:40px;font:15px system-ui;color:#e8eaf4;max-width:720px">${esc(msg)}</div>`;
    return;
  }
  setPlatform(me.platform);
  if (me.user) startApp(me.user); else showAuth(me.has_accounts);
})();
