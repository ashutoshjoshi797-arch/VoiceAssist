/* VoiceAssist — website edition engine (online only).
 *
 * The desktop and phone apps run a local engine on 127.0.0.1 that the UI (app.js) talks to over /api/*.
 * On the website there is no local engine: this file answers the same /api/* calls inside the browser.
 *   - accounts, settings, history and API keys are kept in this browser (localStorage), per account
 *   - answers come from the cloud AI the user picks (Claude, OpenAI or Gemini) with their own API key
 *   - voice input: OpenAI or Gemini transcription (with a key), otherwise Chrome's speech recognition
 *   - voice output: the browser's built-in voices, or OpenAI's voice
 * There is deliberately NO offline mode here: without internet the page says so and asks to reconnect.
 */
"use strict";
(function () {
  const PLATFORM = "web";
  const VERSION = "1.1.1";
  const LS = "va_web_";
  const nativeFetch = window.fetch.bind(window);

  // ---------------------------------------------------------------- storage
  const store = {
    get(k, d) { try { const v = localStorage.getItem(LS + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(LS + k, JSON.stringify(v)); } catch (e) { console.warn("storage full?", e); } },
    del(k) { try { localStorage.removeItem(LS + k); } catch (_) {} },
  };
  const now = () => Date.now() / 1000;
  const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const rand = (n) => crypto.getRandomValues(new Uint8Array(n));
  const hex = (buf) => [...new Uint8Array(buf)].map((x) => x.toString(16).padStart(2, "0")).join("");
  const uid = () => hex(rand(6));

  class HttpError extends Error { constructor(status, message, extra = {}) { super(message); this.status = status; Object.assign(this, extra); } }

  // ---------------------------------------------------------------- settings
  const SYSTEM_PROMPT = "You are a helpful voice assistant. Your replies are read aloud, so answer conversationally in short, clear sentences. " +
    "Do not use markdown, tables, bullet symbols, emoji or code blocks unless the user explicitly asks for them. Keep most answers under 80 words unless the user asks for detail.";
  const DEFAULTS = {
    mode: "auto", auto_switch_back: true, strict_offline: false,
    online: {
      provider: "anthropic",
      models: { anthropic: "claude-sonnet-5-5", openai: "gpt-5.5", gemini: "gemini-3.8-flash" },
      model_choices: {
        anthropic: ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
        openai: ["gpt-5.5", "gpt-5.6-sol", "chat-latest"],
        gemini: ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash"],
      },
      max_output_tokens: 1024, request_timeout_s: 60, cloud_stt: false, cloud_tts: false,
      openai_stt_model: "gpt-4o-mini-transcribe", openai_tts_model: "gpt-4o-mini-tts", openai_tts_voice: "alloy",
    },
    local: { model: "—" },
    web: { stt: "auto", tts: "browser", browser_voice: "", gemini_stt_model: "" },
    stt: { model: "browser", language: "en" },
    tts: { engine: "browser", voice: "", speed: 1.0, speaker_id: 0 },
    assistant: { system_prompt: SYSTEM_PROMPT, max_context_chars_online: 48000 },
    privacy: { save_history: true, save_recordings: false },
    ui: { conversation_style: "handsfree", review_voice: true, mic_device: "", speaker_device: "", vad_sensitivity: 0.5, silence_ms: 800, barge_in: true, theme: "dark", voice_panel_open: false },
  };
  const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
  function merge(base, over) {
    const out = structuredClone(base);
    for (const [k, v] of Object.entries(over || {})) out[k] = isObj(v) && isObj(out[k]) ? merge(out[k], v) : structuredClone(v);
    return out;
  }
  const settings = {
    data: merge(DEFAULTS, store.get("settings", {})),
    update(patch) {
      delete patch.strict_offline; if (patch.mode) patch.mode = "auto";   // online-only edition
      this.data = merge(this.data, patch);
      store.set("settings", this.data);
      return this.data;
    },
  };

  // ---------------------------------------------------------------- accounts (PBKDF2-SHA256, same rules as the apps)
  const ITER = 150000;
  async function pbkdf2(secret, salt, iter = ITER) {
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), "PBKDF2", false, ["deriveBits"]);
    return new Uint8Array(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: iter }, key, 256));
  }
  async function hashSecret(secret) { const salt = rand(16); return `pbkdf2_sha256$${ITER}$${b64(salt)}$${b64(await pbkdf2(secret, salt))}`; }
  async function verify(secret, stored) {
    try {
      const [algo, iter, salt, dk] = stored.split("$");
      if (algo !== "pbkdf2_sha256") return false;
      const a = await pbkdf2(secret, unb64(salt), Number(iter)), b = unb64(dk);
      return a.length === b.length && a.every((x, i) => x === b[i]);
    } catch (_) { return false; }
  }
  const sha256 = async (s) => hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const normCode = (c) => String(c || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  function newRecoveryCode() {
    const A = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    const raw = [...rand(20)].map((x) => A[x % A.length]).join("");
    return raw.match(/.{4}/g).join("-");
  }
  const acctErr = (message, field = "", status = 400) => new HttpError(status, message, { field });
  const MAX_FAILS = 5, LOCK_BASE = 30, LOCK_MAX = 3600, REMEMBER_S = 30 * 86400, SHORT_S = 12 * 3600;

  const accounts = {
    db: store.get("accounts", { users: [], sessions: {}, next_id: 1 }),
    save() { store.set("accounts", this.db); },
    count() { return this.db.users.length; },
    byName(n) { return this.db.users.find((u) => u.username.toLowerCase() === String(n || "").trim().toLowerCase()); },
    byId(id) { return this.db.users.find((u) => u.id === id); },
    pub(u) { return { id: u.id, username: u.username, display_name: u.display_name, email: u.email || "", created: u.created, last_login: u.last_login ?? null }; },
    vUser(u) {
      const t = String(u || "").trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/.test(t)) throw acctErr("Username must be 3–32 characters: letters, numbers, dot, dash or underscore, starting with a letter or number.", "username");
      return t;
    },
    vPass(pw, username = "") {
      pw = String(pw || "");
      if (pw.length < 8) throw acctErr("Password must be at least 8 characters.", "password");
      if (pw.length > 256) throw acctErr("Password is too long (max 256 characters).", "password");
      if (username && pw.toLowerCase() === username.toLowerCase()) throw acctErr("Password must not be the same as your username.", "password");
      if (!(/\p{L}/u.test(pw) && /[^\p{L}]/u.test(pw))) throw acctErr("Use at least one letter and one number or symbol.", "password");
    },
    vEmail(e) { if (e && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)) throw acctErr("That email address doesn't look right.", "email"); },
    async signup(username, password, display, email) {
      const name = this.vUser(username); this.vPass(password, name); email = String(email || "").trim(); this.vEmail(email);
      if (this.byName(name)) throw acctErr("That username is already taken.", "username", 409);
      const code = newRecoveryCode();
      const u = { id: this.db.next_id++, username: name, display_name: (String(display || "").trim().slice(0, 60) || name), email: email.slice(0, 254),
        pw_hash: await hashSecret(password), recovery_hash: await hashSecret(normCode(code)), created: now(), failed: 0, locked_until: 0 };
      this.db.users.push(u); this.save();
      return [this.pub(u), code];
    },
    async login(username, password) {
      const u = this.byName(username);
      if (!u) { await hashSecret("timing-equaliser"); throw acctErr("Incorrect username or password.", "password", 401); }
      const t = now();
      if ((u.locked_until || 0) > t) throw acctErr(`Too many wrong attempts. Try again in ${Math.floor(u.locked_until - t) + 1} seconds.`, "password", 429);
      if (!(await verify(password, u.pw_hash))) {
        u.failed = (u.failed || 0) + 1;
        u.locked_until = u.failed >= MAX_FAILS ? t + Math.min(LOCK_MAX, LOCK_BASE * 2 ** Math.min(10, u.failed - MAX_FAILS)) : 0;
        this.save();
        if (u.locked_until) throw acctErr(`Too many wrong attempts. Try again in ${Math.floor(u.locked_until - t)} seconds.`, "password", 429);
        throw acctErr("Incorrect username or password.", "password", 401);
      }
      u.failed = 0; u.locked_until = 0; u.last_login = t; this.save();
      return this.pub(u);
    },
    async createSession(userId, remember) {
      const token = b64(rand(32)).replace(/[+/=]/g, "");
      this.db.sessions[await sha256(token)] = { user_id: userId, expires: now() + (remember ? REMEMBER_S : SHORT_S), remember: !!remember };
      this.save();
      try { (remember ? localStorage : sessionStorage).setItem(LS + "session", token); (remember ? sessionStorage : localStorage).removeItem(LS + "session"); } catch (_) {}
      return token;
    },
    token() { try { return sessionStorage.getItem(LS + "session") || localStorage.getItem(LS + "session"); } catch (_) { return null; } },
    async sessionUser() {
      const tok = this.token(); if (!tok) return null;
      const h = await sha256(tok), s = this.db.sessions[h];
      if (!s) return null;
      if (s.expires < now()) { delete this.db.sessions[h]; this.save(); return null; }
      const u = this.byId(s.user_id);
      return u ? this.pub(u) : null;
    },
    async endSession() {
      const tok = this.token();
      if (tok) { delete this.db.sessions[await sha256(tok)]; this.save(); }
      try { localStorage.removeItem(LS + "session"); sessionStorage.removeItem(LS + "session"); } catch (_) {}
    },
    async endOthers(userId) {
      const keep = this.token() ? await sha256(this.token()) : "";
      for (const [k, s] of Object.entries(this.db.sessions)) if (s.user_id === userId && k !== keep) delete this.db.sessions[k];
      this.save();
    },
    async check(userId, password) {
      const u = this.byId(userId); if (!u) throw acctErr("Account not found.", "", 404);
      if (!(await verify(password, u.pw_hash))) throw acctErr("Current password is incorrect.", "current_password", 401);
      return u;
    },
  };

  // ---------------------------------------------------------------- per-account API keys (kept in this browser only)
  const PROVIDERS = ["anthropic", "openai", "gemini"];
  const keys = {
    get(userId, p) { return (store.get("keys_" + userId, {}) || {})[p] || ""; },
    set(userId, p, k) { const all = store.get("keys_" + userId, {}); if (k) all[p] = k; else delete all[p]; store.set("keys_" + userId, all); },
    status(userId) {
      const o = {};
      for (const p of PROVIDERS) { const has = !!this.get(userId, p); o[p] = { configured: has, source: has ? "this browser" : null }; }
      return o;
    },
  };

  // ---------------------------------------------------------------- history (per account)
  const history = {
    db: store.get("history", { convs: {}, next_msg: 1 }),
    save() { if (settings.data.privacy.save_history) store.set("history", this.db); },
    create(userId) {
      const id = uid(); this.db.convs[id] = { id, title: "New conversation", created: now(), updated: now(), user_id: userId, messages: [] };
      this.save(); return id;
    },
    exists(cid, userId) { const c = cid && this.db.convs[cid]; return !!c && c.user_id === userId; },
    add(cid, role, content, meta = {}) {
      const c = this.db.convs[cid], id = this.db.next_msg++;
      c.messages.push({ id, role, content, created: now(), meta }); c.updated = now();
      if (role === "user" && c.title === "New conversation") { const t = content.trim().replace(/\n/g, " "); c.title = t.length > 60 ? t.slice(0, 60) + "…" : t; }
      this.save(); return id;
    },
    list(userId) {
      return Object.values(this.db.convs).filter((c) => c.user_id === userId).sort((a, b) => b.updated - a.updated).slice(0, 200)
        .map((c) => ({ id: c.id, title: c.title, created: c.created, updated: c.updated, n: c.messages.length }));
    },
    del(cid) { delete this.db.convs[cid]; this.save(); },
    clear(userId) { for (const c of Object.values(this.db.convs)) if (c.user_id === userId) delete this.db.convs[c.id]; this.save(); },
  };

  // ---------------------------------------------------------------- events (replaces the engine's server-sent events)
  const listeners = new Set();
  function publish(ev) { const data = JSON.stringify(ev); for (const es of listeners) setTimeout(() => es.onmessage && es.onmessage({ data }), 0); }
  class FakeEventSource {
    constructor() { this.readyState = 1; listeners.add(this); setTimeout(() => this.onopen && this.onopen({}), 0); }
    close() { this.readyState = 2; listeners.delete(this); }
    addEventListener(type, fn) { if (type === "message") this.onmessage = fn; else if (type === "open") this.onopen = fn; else if (type === "error") this.onerror = fn; }
    removeEventListener() {}
  }

  // ---------------------------------------------------------------- network status (no offline mode)
  const net = { state: navigator.onLine ? "ONLINE" : "OFFLINE", detail: "", internet: navigator.onLine, last_check: now() };
  function setNet(state, detail) {
    const changed = net.state !== state;
    net.state = state; net.detail = detail; net.internet = state === "ONLINE"; net.last_check = now();
    if (changed) { const r = route(currentUserId); publish({ type: "network", kind: "state", ...snapshot(), route: r.route, route_reason: r.reason }); }
  }
  const snapshot = () => ({ state: net.state, detail: net.detail || (net.state === "ONLINE" ? "Internet reachable" : "No internet connection"),
    airplane: null, adapters_up: null, internet: net.internet, last_check: net.last_check, pinned_local: false, switch_back_offered: false, strict_offline: false });
  addEventListener("online", () => setNet("ONLINE", "Internet reachable"));
  addEventListener("offline", () => setNet("OFFLINE", "No internet connection — reconnect to keep talking."));
  let currentUserId = null;
  function route(userId) {
    if (net.state !== "ONLINE") return { route: "offline", reason: "No internet — reconnect, or use the installed app for offline use" };
    const p = settings.data.online.provider;
    if (!userId || !keys.get(userId, p)) return { route: "online", reason: "Add an API key in Settings → Online AI" };
    return { route: "online", reason: "Internet available" };
  }

  // ---------------------------------------------------------------- text helpers
  function speakable(s) {
    return String(s).replace(/```[\s\S]*?```/g, " ").replace(/`([^`]*)`/g, "$1").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/[*_#>|~]+/g, " ").replace(/^\s*[-•]\s+/gm, "").replace(/https?:\/\/\S+/g, "link").replace(/\s+/g, " ").trim();
  }
  class SentenceChunker {
    constructor() { this.buf = ""; this.first = true; }
    feed(d) {
      this.buf += d; const out = [];
      for (;;) {
        const re = this.first ? /^([\s\S]{12,}?[.!?;:])(\s+)/ : /^([\s\S]{30,}?[.!?])(\s+)/;
        const m = this.buf.match(re) || this.buf.match(/^([\s\S]+?)\n\n+/);
        if (!m) break;
        out.push(m[1].trim()); this.buf = this.buf.slice(m[0].length); this.first = false;
      }
      return out.filter(Boolean);
    }
    flush() { const t = this.buf.trim(); this.buf = ""; return t ? [t] : []; }
  }
  function mergeRoles(msgs) {
    const out = [];
    for (const m of msgs) { if (out.length && out.at(-1).role === m.role) out.at(-1).content += "\n\n" + m.content; else out.push({ role: m.role, content: m.content }); }
    while (out.length && out[0].role !== "user") out.shift();
    return out;
  }
  function buildContext(msgs, budget) {
    const out = []; let used = 0;
    for (let i = msgs.length - 1; i >= 0; i--) { used += msgs[i].content.length; if (used > budget && out.length) break; out.unshift(msgs[i]); }
    return mergeRoles(out);
  }

  // ---------------------------------------------------------------- cloud AI
  const LABELS = { anthropic: "Claude", openai: "OpenAI", gemini: "Gemini" };
  async function* sseLines(res, signal) {
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
    try {
      for (;;) {
        if (signal?.aborted) return;
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) { const line = buf.slice(0, i).replace(/\r$/, ""); buf = buf.slice(i + 1); yield line; }
      }
      if (buf) yield buf;
    } finally { try { reader.cancel(); } catch (_) {} }
  }
  async function cloudError(res, label) {
    let msg = ""; try { const j = await res.json(); msg = j.error?.message || j.error?.status || j.message || ""; } catch (_) {}
    if (res.status === 401 || res.status === 403) return new HttpError(502, `${label} rejected the API key.`, { hint: "Check the key in Settings → Online AI." });
    if (res.status === 429) return new HttpError(502, `${label} is rate-limiting or out of credit.`, { hint: msg });
    if (res.status === 404) return new HttpError(502, `${label} doesn't know that model.`, { hint: msg || "Pick another model in Settings → Online AI." });
    return new HttpError(502, `${label} error ${res.status}${msg ? ": " + msg : ""}`);
  }
  async function cloudFetch(url, opts, label) {
    try { return await nativeFetch(url, opts); }
    catch (e) {
      if (e.name === "AbortError") throw e;
      if (!navigator.onLine) setNet("OFFLINE", "No internet connection — reconnect to keep talking.");
      throw new HttpError(502, `Could not reach ${label}.`, { hint: navigator.onLine ? "Check your internet connection and try again." : "You're offline — reconnect, or use the installed app for offline use.", network: true });
    }
  }
  async function streamAnswer(provider, model, key, system, messages, maxTokens, signal, onText) {
    const label = LABELS[provider];
    let res, done = false;
    if (provider === "anthropic") {
      res = await cloudFetch("https://api.anthropic.com/v1/messages", { method: "POST", signal, headers: {
        "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01", "anthropic-dangerous-direct-browser-access": "true" },
        body: JSON.stringify({ model, max_tokens: maxTokens, system, messages, stream: true }) }, label);
      if (!res.ok) throw await cloudError(res, label);
      for await (const line of sseLines(res, signal)) {
        if (!line.startsWith("data:")) continue;
        let ev; try { ev = JSON.parse(line.slice(5)); } catch (_) { continue; }
        if (ev.type === "content_block_delta" && ev.delta?.type === "text_delta") onText(ev.delta.text);
        else if (ev.type === "message_stop") done = true;
        else if (ev.type === "error") throw new HttpError(502, `${label}: ${ev.error?.message || "error"}`);
      }
    } else if (provider === "openai") {
      res = await cloudFetch("https://api.openai.com/v1/chat/completions", { method: "POST", signal, headers: { "content-type": "application/json", authorization: "Bearer " + key },
        body: JSON.stringify({ model, messages: [{ role: "system", content: system }, ...messages], stream: true, max_completion_tokens: maxTokens }) }, label);
      if (!res.ok) throw await cloudError(res, label);
      for await (const line of sseLines(res, signal)) {
        if (!line.startsWith("data:")) continue;
        const d = line.slice(5).trim();
        if (d === "[DONE]") { done = true; break; }
        let ev; try { ev = JSON.parse(d); } catch (_) { continue; }
        const t = ev.choices?.[0]?.delta?.content; if (t) onText(t);
        if (ev.choices?.[0]?.finish_reason) done = true;
      }
    } else {
      res = await cloudFetch(`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:streamGenerateContent?alt=sse`, {
        method: "POST", signal, headers: { "content-type": "application/json", "x-goog-api-key": key },
        body: JSON.stringify({ contents: messages.map((m) => ({ role: m.role === "assistant" ? "model" : "user", parts: [{ text: m.content }] })),
          systemInstruction: { parts: [{ text: system }] }, generationConfig: { maxOutputTokens: maxTokens } }) }, label);
      if (!res.ok) throw await cloudError(res, label);
      for await (const line of sseLines(res, signal)) {
        if (!line.startsWith("data:")) continue;
        let ev; try { ev = JSON.parse(line.slice(5)); } catch (_) { continue; }
        for (const part of ev.candidates?.[0]?.content?.parts || []) if (part.text && !part.thought) onText(part.text);
        if (ev.candidates?.[0]?.finishReason) done = true;
      }
    }
    if (!done && !signal?.aborted) throw new HttpError(502, `The connection to ${label} dropped before the answer finished.`, { hint: "Check your internet connection.", network: true });
  }

  // ---------------------------------------------------------------- speech to text
  async function wavToBuffer(wav) {
    const ctx = new OfflineAudioContext(1, 16000, 16000);
    return await ctx.decodeAudioData(wav.slice(0));
  }
  async function transcribe(userId, wav, signal) {
    const s = settings.data, lang = (s.stt.language || "en").slice(0, 2);
    const oa = keys.get(userId, "openai"), gm = keys.get(userId, "gemini");
    const pref = s.web.stt;
    if (oa && (pref === "auto" || pref === "openai")) {
      const fd = new FormData();
      fd.append("file", new Blob([wav], { type: "audio/wav" }), "speech.wav");
      fd.append("model", s.online.openai_stt_model || "gpt-4o-mini-transcribe");
      if (lang) fd.append("language", lang);
      const res = await cloudFetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", signal, headers: { authorization: "Bearer " + oa }, body: fd }, "OpenAI");
      if (!res.ok) throw await cloudError(res, "OpenAI speech recognition");
      return { text: ((await res.json()).text || "").trim(), engine: "OpenAI speech recognition" };
    }
    if (gm && (pref === "auto" || pref === "gemini")) {
      const model = s.web.gemini_stt_model || s.online.models.gemini;
      const body = { contents: [{ role: "user", parts: [
        { inline_data: { mime_type: "audio/wav", data: b64(wav) } },
        { text: "Transcribe this speech exactly as spoken. Reply with only the transcript and nothing else. If there is no speech, reply with an empty string." }] }],
        generationConfig: { temperature: 0 } };
      const res = await cloudFetch(`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`, {
        method: "POST", signal, headers: { "content-type": "application/json", "x-goog-api-key": gm }, body: JSON.stringify(body) }, "Gemini");
      if (!res.ok) throw await cloudError(res, "Gemini speech recognition");
      const j = await res.json();
      const text = (j.candidates?.[0]?.content?.parts || []).map((p) => p.text || "").join("").trim().replace(/^"|"$/g, "");
      return { text, engine: "Gemini speech recognition" };
    }
    return { text: await chromeRecognize(wav, lang, signal), engine: "Chrome speech recognition" };
  }
  /** Chrome (135+) can recognise speech from an audio track: replay the recording into it. */
  async function chromeRecognize(wav, lang, signal) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const noVoice = new HttpError(400, "Voice input needs an OpenAI or Gemini key on the website (Claude can't listen to audio), or a recent Chrome.",
      { hint: "Add an OpenAI or Gemini key in Settings → Online AI, update Chrome, or type your question." });
    if (!SR) throw noVoice;
    const ctx = new AudioContext();
    try {
      const buf = await ctx.decodeAudioData(wav.slice(0));
      const dest = ctx.createMediaStreamDestination(), src = ctx.createBufferSource();
      src.buffer = buf; src.connect(dest);
      const rec = new SR(); rec.lang = lang === "en" ? "en-US" : lang; rec.interimResults = false; rec.continuous = true; rec.maxAlternatives = 1;
      let text = "";
      const result = new Promise((resolve, reject) => {
        rec.onresult = (e) => { for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) text += e.results[i][0].transcript + " "; };
        rec.onerror = (e) => (e.error === "no-speech" ? resolve("") : reject(e.error === "not-allowed" || e.error === "service-not-allowed" ? noVoice : new HttpError(502, "Chrome speech recognition failed: " + e.error)));
        rec.onend = () => resolve(text.trim());
        signal?.addEventListener("abort", () => { try { rec.abort(); } catch (_) {} reject(new DOMException("aborted", "AbortError")); });
      });
      try { rec.start(dest.stream.getAudioTracks()[0]); } catch (e) { throw noVoice; }
      src.start(); src.onended = () => setTimeout(() => { try { rec.stop(); } catch (_) {} }, 700);
      return await result;
    } finally { ctx.close().catch(() => {}); }
  }

  // ---------------------------------------------------------------- text to speech (browser voices or OpenAI)
  const audioStore = new Map();
  async function openaiSpeech(userId, text, signal) {
    const s = settings.data;
    const res = await cloudFetch("https://api.openai.com/v1/audio/speech", { method: "POST", signal,
      headers: { "content-type": "application/json", authorization: "Bearer " + keys.get(userId, "openai") },
      body: JSON.stringify({ model: s.online.openai_tts_model, voice: s.online.openai_tts_voice || "alloy", input: text, response_format: "mp3", speed: s.tts.speed || 1 }) }, "OpenAI");
    if (!res.ok) throw await cloudError(res, "OpenAI voice");
    const id = uid(); audioStore.set(id, await res.blob()); return id;
  }

  // ---------------------------------------------------------------- turns
  let current = null;
  function runTurn(userId, convId, wav, typed) {
    const ctrl = new AbortController();
    const turn = { id: uid(), ctrl, started: performance.now() };
    if (current) current.ctrl.abort();
    current = turn;
    let controller;
    const stream = new ReadableStream({ start(c) { controller = c; }, cancel() { ctrl.abort(); } });
    const put = (type, o = {}) => { try { controller.enqueue(new TextEncoder().encode(JSON.stringify({ type, ...o }) + "\n")); } catch (_) {} };
    (async () => {
      put("accepted", { turn_id: turn.id });
      const s = settings.data, r = route(userId);
      put("route", { route: r.route, reason: r.reason, network: net.state });
      try {
        if (r.route === "offline") { put("error", { message: "You're offline.", hint: "Reconnect to the internet to keep talking. For offline use, install the VoiceAssist app on your computer or phone." }); return; }
        const p = s.online.provider, key = keys.get(userId, p);
        let text = (typed || "").trim(), sttName = "typed", sttS = null;
        if (wav) {
          const t0 = performance.now();
          const tr = await transcribe(userId, wav, ctrl.signal);
          text = tr.text; sttName = tr.engine; sttS = (performance.now() - t0) / 1000;
          put("transcript", { text, engine: sttName, seconds: sttS });
          if (!text) { put("no_speech", { reason: "nothing recognised" }); put("done", { spoken: false }); return; }
        }
        if (!text) { put("done", { spoken: false }); return; }
        if (!key) { put("error", { message: `Add your ${LABELS[p]} API key to get answers.`, hint: "Settings → Online AI → API keys. The key is kept only in this browser." }); return; }
        let cid = convId;
        if (!history.exists(cid, userId)) cid = history.create(userId);
        const mid = history.add(cid, "user", text, { input: wav ? "voice" : "text", stt: sttName });
        put("user_saved", { conversation_id: cid, message_id: mid });
        const model = s.online.models[p];
        const info = { kind: "cloud", provider: p, label: LABELS[p], model };
        put("llm_start", info);
        const ctx = buildContext(history.db.convs[cid].messages.map((m) => ({ role: m.role, content: m.content })), s.assistant.max_context_chars_online || 48000);
        const chunker = new SentenceChunker();
        let seq = 0, firstAudio = null, firstTok = null, answer = "";
        const ttsMode = s.web.tts === "openai" && keys.get(userId, "openai") ? "openai" : s.web.tts === "none" ? "none" : "browser";
        let ttsChain = Promise.resolve(), ttsFailed = false;
        const speak = (chunk) => {
          const t = speakable(chunk); if (!t || ttsMode === "none") return;
          const n = seq++;
          if (ttsMode === "browser") { if (firstAudio == null) firstAudio = performance.now(); put("say", { seq: n, text: t, voice: s.web.browser_voice, rate: s.tts.speed || 1 }); return; }
          ttsChain = ttsChain.then(async () => {
            if (ttsFailed || ctrl.signal.aborted) return;
            try { const id = await openaiSpeech(userId, t, ctrl.signal); if (firstAudio == null) firstAudio = performance.now(); put("audio", { id, seq: n, text: chunk, voice: "OpenAI " + s.online.openai_tts_voice }); }
            catch (e) { if (e.name !== "AbortError") { ttsFailed = true; put("tts_error", { message: "OpenAI voice failed — showing text only.", hint: e.message }); } }
          });
        };
        await streamAnswer(p, model, key, s.assistant.system_prompt, ctx, s.online.max_output_tokens || 1024, ctrl.signal, (d) => {
          if (firstTok == null) firstTok = performance.now();
          answer += d; put("token", { text: d }); chunker.feed(d).forEach(speak);
        });
        chunker.flush().forEach(speak);
        await ttsChain;
        answer = answer.trim();
        if (ctrl.signal.aborted) { if (answer) history.add(cid, "assistant", answer, { ...info, interrupted: true }); put("cancelled", { stage: "answer", partial: answer }); return; }
        if (!answer) { put("error", { message: `${LABELS[p]} returned an empty answer.` }); return; }
        const amid = history.add(cid, "assistant", answer, info);
        const sec = (t) => (t == null ? null : Math.round((t - turn.started) / 10) / 100);
        put("done", { spoken: seq > 0, message_id: amid, conversation_id: cid, provider: info,
          timings: { stt_s: sttS, first_token_s: sec(firstTok), first_audio_s: sec(firstAudio), total_s: sec(performance.now()) } });
      } catch (e) {
        if (e.name === "AbortError") put("cancelled", { stage: "answer" });
        else put("error", { message: e.message, hint: e.hint || "" });
      } finally {
        if (current === turn) current = null;
        try { controller.close(); } catch (_) {}
      }
    })();
    return stream;
  }

  // ---------------------------------------------------------------- the /api router
  const json = (o, status = 200, headers = {}) => new Response(JSON.stringify(o), { status, headers: { "content-type": "application/json", ...headers } });
  async function bodyJson(init) { try { return init?.body ? JSON.parse(typeof init.body === "string" ? init.body : await new Response(init.body).text()) : {}; } catch (_) { return {}; } }
  const PUBLIC = new Set(["/api/version", "/api/quit", "/api/auth/me", "/api/auth/signup", "/api/auth/login", "/api/auth/logout", "/api/auth/recover", "/api/ping", "/api/bye"]);

  function state(user) {
    const r = route(user?.id);
    return { version: VERSION, platform: PLATFORM, settings: settings.data, network: snapshot(), route: r, keys: keys.status(user?.id), credential_store: true,
      stt: { model: "browser", installed: true, loaded: true, error: null, available: ["browser"], vad: true, engine_ok: true },
      tts: { engine: settings.data.web.tts, active: settings.data.web.tts === "openai" ? "OpenAI voice" : settings.data.web.tts === "none" ? "none (text only)" : "Browser voice", voice: "", installed: true, voices: [], system_available: true, error: null },
      local: { state: "unknown", running: false, installed: [], recommended: [] },
      catalog: { stt: { browser: { note: "in the browser" } }, voices: {} },
      data_dir: "this browser (local storage)", models_dir: "— (website edition: no offline models)", user };
  }

  async function handle(path, init) {
    const method = (init?.method || "GET").toUpperCase();
    const url = new URL(path, location.origin);
    const p = url.pathname;
    const user = await accounts.sessionUser();
    currentUserId = user?.id ?? null;
    if (!user && !PUBLIC.has(p)) return json({ error: "Please log in.", auth: "required" }, 401);
    const isAudio = (p === "/api/turn" || p === "/api/transcribe") && !(init?.headers?.["Content-Type"] || "").startsWith("application/json");
    const b = method === "POST" && !isAudio ? await bodyJson(init) : {};
    switch (`${method} ${p}`) {
      case "GET /api/version": return json({ version: VERSION });
      case "POST /api/ping": case "POST /api/bye": case "POST /api/quit": return json({ ok: true });
      case "GET /api/state": return json(state(user));
      case "GET /api/auth/me": return json({ user, has_accounts: accounts.count() > 0, platform: PLATFORM });
      case "POST /api/auth/signup": {
        const [u, code] = await accounts.signup(b.username, b.password, b.display_name, b.email);
        await accounts.createSession(u.id, b.remember !== false);
        return json({ user: u, recovery_code: code, claimed_conversations: 0 }, 201);
      }
      case "POST /api/auth/login": { const u = await accounts.login(b.username, b.password); await accounts.createSession(u.id, b.remember !== false); return json({ user: u }); }
      case "POST /api/auth/logout": if (current) current.ctrl.abort(); await accounts.endSession(); return json({ ok: true });
      case "POST /api/auth/recover": {
        const u = accounts.byName(b.username);
        if (!u || !(await verify(normCode(b.code), u.recovery_hash))) throw acctErr("That username and recovery code don't match.", "code", 401);
        accounts.vPass(b.new_password, u.username);
        const code = newRecoveryCode();
        u.pw_hash = await hashSecret(b.new_password); u.recovery_hash = await hashSecret(normCode(code)); u.failed = 0; u.locked_until = 0;
        for (const [k, s] of Object.entries(accounts.db.sessions)) if (s.user_id === u.id) delete accounts.db.sessions[k];
        accounts.save(); await accounts.createSession(u.id, b.remember !== false);
        return json({ user: accounts.pub(u), recovery_code: code });
      }
      case "POST /api/account/profile": {
        const u = accounts.byId(user.id);
        if (b.display_name != null) { const d = String(b.display_name).trim().slice(0, 60); if (!d) throw acctErr("Name can't be empty.", "display_name"); u.display_name = d; }
        if (b.email != null) { const e = String(b.email).trim(); accounts.vEmail(e); u.email = e.slice(0, 254); }
        accounts.save(); return json({ user: accounts.pub(u) });
      }
      case "POST /api/account/password": {
        const u = await accounts.check(user.id, b.current_password); accounts.vPass(b.new_password, u.username);
        u.pw_hash = await hashSecret(b.new_password); accounts.save(); await accounts.endOthers(user.id); return json({ ok: true });
      }
      case "POST /api/account/recovery": {
        const u = await accounts.check(user.id, b.password); const code = newRecoveryCode();
        u.recovery_hash = await hashSecret(normCode(code)); accounts.save(); return json({ recovery_code: code });
      }
      case "POST /api/account/delete": {
        await accounts.check(user.id, b.password);
        accounts.db.users = accounts.db.users.filter((x) => x.id !== user.id);
        for (const [k, s] of Object.entries(accounts.db.sessions)) if (s.user_id === user.id) delete accounts.db.sessions[k];
        accounts.save(); history.clear(user.id); store.del("keys_" + user.id); await accounts.endSession();
        return json({ ok: true });
      }
      case "GET /api/settings": return json(settings.data);
      case "POST /api/settings": { const s = settings.update(b); publish({ type: "settings", settings: s }); return json({ ok: true, settings: s }); }
      case "POST /api/keys": {
        if (!PROVIDERS.includes(b.provider)) throw new HttpError(400, "unknown provider");
        keys.set(user.id, b.provider, String(b.key || "").trim()); return json({ ok: true, keys: keys.status(user.id) });
      }
      case "POST /api/network/recheck": setNet(navigator.onLine ? "ONLINE" : "OFFLINE", navigator.onLine ? "Internet reachable" : "No internet connection"); return json(snapshot());
      case "POST /api/switch_back": return json({ ok: true, route: route(user.id) });
      case "GET /api/conversations": return json({ conversations: history.list(user.id), persistent: settings.data.privacy.save_history });
      case "POST /api/conversations": return json({ id: history.create(user.id) });
      case "POST /api/history/clear": history.clear(user.id); return json({ ok: true });
      case "POST /api/cancel": { const ok = !!current && (!b.turn_id || current.id === b.turn_id); if (ok) current.ctrl.abort(); return json({ cancelled: ok }); }
      case "GET /api/setup/status": return json({ platform: PLATFORM, ready: true, online_only: true, engine_installed: false, engine_running: false, model: "", model_installed: false, installed_models: [], speech_installed: true, choices: [], job: { state: "idle" } });
      case "POST /api/setup/start": throw new HttpError(400, "The website works online only. Install the VoiceAssist app for offline use.");
      case "GET /api/local/models": return json({ running: false, installed: [], recommended: [] });
      case "POST /api/local/preload": return json({ ok: true });
      case "GET /api/voices": return json({ piper: [], system: [], catalog: {} });
      case "POST /api/selftest": throw new HttpError(400, "The offline self-test is only in the installed apps.");
      case "POST /api/transcribe": {
        if (!navigator.onLine) throw new HttpError(503, "You're offline.", { hint: "Reconnect to the internet to keep talking." });
        const r = await transcribe(user.id, await new Response(init.body).arrayBuffer(), new AbortController().signal);
        return json({ text: r.text, engine: r.engine });
      }
      case "POST /api/turn": {
        const ctype = (init?.headers?.["Content-Type"] || "").split(";")[0];
        const conv = url.searchParams.get("conversation_id");
        const stream = ctype === "application/json" ? runTurn(user.id, b.conversation_id || conv, null, b.text) : runTurn(user.id, conv, await new Response(init.body).arrayBuffer(), null);
        return new Response(stream, { status: 200, headers: { "content-type": "application/x-ndjson" } });
      }
      case "POST /api/tts/test": {
        const text = b.text || "Hello! This is how I sound.";
        if (settings.data.web.tts === "openai" && keys.get(user.id, "openai")) { const id = await openaiSpeech(user.id, text); return new Response(audioStore.get(id), { headers: { "content-type": "audio/mpeg" } }); }
        return json({ say: text });
      }
    }
    if (method === "GET" && p.startsWith("/api/audio/")) {
      const id = p.slice(11), blob = audioStore.get(id);
      audioStore.delete(id);
      return blob ? new Response(blob, { headers: { "content-type": blob.type || "audio/mpeg" } }) : json({ error: "audio expired" }, 404);
    }
    if (p.startsWith("/api/conversations/")) {
      const cid = p.slice(19);
      if (!history.exists(cid, user.id)) return json({ error: "conversation not found" }, 404);
      if (method === "DELETE") { history.del(cid); return json({ ok: true }); }
      return json({ id: cid, messages: history.db.convs[cid].messages });
    }
    return json({ error: "not found" }, 404);
  }

  // ---------------------------------------------------------------- install the shims
  window.fetch = async function (input, init) {
    const path = typeof input === "string" ? input : input instanceof Request ? new URL(input.url).pathname + new URL(input.url).search : String(input);
    if (!path.startsWith("/api/")) return nativeFetch(input, init);
    if (init?.headers instanceof Headers) init = { ...init, headers: Object.fromEntries(init.headers.entries()) };
    try { return await handle(path, init); }
    catch (e) { return json({ error: e.message || String(e), hint: e.hint || "", field: e.field || "" }, e.status || 500); }
  };
  const NativeES = window.EventSource;
  window.EventSource = function (url, opts) { return String(url).startsWith("/api/events") ? new FakeEventSource(url) : new NativeES(url, opts); };
  const nativeBeacon = navigator.sendBeacon?.bind(navigator);
  navigator.sendBeacon = (url, data) => (String(url).startsWith("/api/") ? true : nativeBeacon ? nativeBeacon(url, data) : false);
  window.VA_WEB = { PLATFORM, speakable };
})();
