/* VoiceAssist — website edition engine (online only).
 *
 * The desktop and phone apps run a local engine on 127.0.0.1 that the UI (app.js) talks to over /api/*.
 * On the website there is no local engine: this file answers the same /api/* calls inside the browser.
 *   - accounts are VoiceAssist cloud accounts (Supabase Auth) — the same login as the laptop and phone apps
 *   - answers come from the VoiceAssist cloud AI (the product's Supabase "chat" function); users need no AI key
 *   - voice input: transcribed by the VoiceAssist cloud, or Chrome's own speech recognition
 *   - voice output: the browser's built-in voices
 *   - settings and conversation history stay in this browser (localStorage), per account
 * There is deliberately NO offline mode here: without internet the page says so and asks to reconnect.
 */
"use strict";
(function () {
  const PLATFORM = "web";
  const VERSION = "1.2.0";
  const LS = "va_web_";
  const nativeFetch = window.fetch.bind(window);
  const CLOUD = window.VA_CLOUD || {};   // { url, key, site_url } — written by packaging/web/build_site.py

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
    online: { max_output_tokens: 1024, request_timeout_s: 60 },
    local: { model: "—" },
    web: { stt: "auto", tts: "browser", browser_voice: "" },
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

  // ---------------------------------------------------------------- accounts: VoiceAssist cloud (Supabase Auth)
  const acctErr = (message, field = "", status = 400) => new HttpError(status, message, { field });
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  async function sb(method, path, body, token) {
    if (!CLOUD.url || !CLOUD.key) throw acctErr("This website isn't connected to the VoiceAssist cloud yet.", "", 503);
    let r;
    try {
      r = await nativeFetch(CLOUD.url + path, { method, headers: { apikey: CLOUD.key, Authorization: "Bearer " + (token || CLOUD.key), "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw acctErr(navigator.onLine ? "Couldn't reach the VoiceAssist cloud — please try again." : "You're offline — reconnect to sign in.", "", 503);
    }
    const txt = await r.text(); let j = {}; try { j = txt ? JSON.parse(txt) : {}; } catch (_) {}
    if (!r.ok) throw mapAuthError(r.status, j);
    return j;
  }
  function mapAuthError(status, j) {
    let msg = j.msg || j.error_description || j.message || (typeof j.error === "string" ? j.error : j.error?.message) || "";
    const code = j.error_code || j.code || "", m = String(msg).toLowerCase();
    if (code === "invalid_credentials" || m.includes("invalid login")) return acctErr("Incorrect email or password.", "password", 401);
    if (code === "email_not_confirmed" || m.includes("not confirmed")) return acctErr("Please confirm your email first — open the link we sent you, then log in.", "email", 401);
    if (code === "user_already_exists" || code === "email_exists" || m.includes("already registered")) return acctErr("An account with this email already exists. Log in instead.", "email", 409);
    if (code === "weak_password" || (m.includes("password") && (m.includes("least") || m.includes("weak") || m.includes("should")))) return acctErr(msg || "Choose a stronger password.", "password");
    if (status === 429 || String(code).startsWith("over_")) return acctErr("Too many attempts — please wait a minute and try again.", "", 429);
    if (m.includes("email") && m.includes("valid")) return acctErr("That email address doesn't look right.", "email");
    return new HttpError(status >= 400 ? status : 400, msg || `VoiceAssist cloud error (${status}).`, { hint: j.hint || "" });
  }
  const pub = (u) => ({ id: u.id, username: u.email, email: u.email, display_name: u.user_metadata?.display_name || (u.email || "").split("@")[0],
    created: u.created_at, last_login: u.last_sign_in_at });
  function validate(email, pw) {
    email = String(email || "").trim();
    if (!EMAIL_RE.test(email)) throw acctErr("Enter a valid email address.", "email");
    if (String(pw || "").length < 8) throw acctErr("Password must be at least 8 characters.", "password");
    if (!(/\p{L}/u.test(pw) && /[^\p{L}]/u.test(pw))) throw acctErr("Use at least one letter and one number or symbol.", "password");
    return email;
  }
  const accounts = {
    sess: null,   // { access, refresh, expires_at, user, remember }
    load() {
      for (const st of [sessionStorage, localStorage]) {
        try { const v = st.getItem(LS + "cloud_session"); if (v) { this.sess = JSON.parse(v); return; } } catch (_) {}
      }
    },
    save(remember) {
      try {
        localStorage.removeItem(LS + "cloud_session"); sessionStorage.removeItem(LS + "cloud_session");
        if (this.sess) (remember ?? this.sess.remember ? localStorage : sessionStorage).setItem(LS + "cloud_session", JSON.stringify(this.sess));
      } catch (_) {}
    },
    store(r, remember) {
      this.sess = { access: r.access_token, refresh: r.refresh_token, expires_at: now() + (r.expires_in || 3600) - 60, user: pub(r.user), remember: remember !== false };
      this.save(remember !== false);
      return this.sess.user;
    },
    user() { return this.sess ? this.sess.user : null; },
    async token() {
      if (!this.sess) throw new HttpError(401, "Please log in.", { auth: "required" });
      if (this.sess.expires_at > now()) return this.sess.access;
      try { this.store(await sb("POST", "/auth/v1/token?grant_type=refresh_token", { refresh_token: this.sess.refresh }), this.sess.remember); }
      catch (e) { if (e.status === 503) throw e; this.sess = null; this.save(); throw new HttpError(401, "Your sign-in has expired — please log in again.", { auth: "required" }); }
      return this.sess.access;
    },
    async signup(email, pw, name, remember) {
      email = validate(email, pw);
      const r = await sb("POST", "/auth/v1/signup", { email, password: pw, data: { display_name: String(name || "").trim().slice(0, 60) } });
      if (r.access_token) return { user: this.store(r, remember) };
      if (Array.isArray(r.identities) && r.identities.length === 0) throw acctErr("An account with this email already exists. Log in instead.", "email", 409);
      return { needs_confirmation: true };
    },
    async login(email, pw, remember) {
      email = String(email || "").trim();
      if (!email) throw acctErr("Enter your email.", "email");
      return this.store(await sb("POST", "/auth/v1/token?grant_type=password", { email, password: pw }), remember);
    },
    async verify(pw) {
      try { await sb("POST", "/auth/v1/token?grant_type=password", { email: this.sess.user.email, password: pw }); }
      catch (e) { if (e.status === 401) throw acctErr("Current password is incorrect.", "current_password", 401); throw e; }
    },
    async logout() {
      const s = this.sess; this.sess = null; this.save();
      if (s) sb("POST", "/auth/v1/logout", {}, s.access).catch(() => {});
    },
  };
  accounts.load();
  // opened from a password-reset email: Supabase puts a short-lived session in the URL fragment
  let recovery = null;
  (function readRecoveryLink() {
    const h = new URLSearchParams(location.hash.slice(1));
    if (h.get("type") === "recovery" && h.get("access_token")) {
      recovery = { access_token: h.get("access_token"), refresh_token: h.get("refresh_token"), expires_in: Number(h.get("expires_in") || 3600) };
      window.history.replaceState(null, "", location.pathname + location.search);
    } else if (h.get("error_description")) {
      recovery = { error: h.get("error_description") };
      window.history.replaceState(null, "", location.pathname + location.search);
    }
  })();

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
      if (role === "assistant") c.unread = true; else if (role === "user") c.archived = false;
      if (role === "user" && c.title === "New conversation") { const t = content.trim().replace(/\n/g, " "); c.title = t.length > 60 ? t.slice(0, 60) + "…" : t; }
      this.save(); return id;
    },
    list(userId) {
      return Object.values(this.db.convs).filter((c) => c.user_id === userId)
        .sort((a, b) => (!!b.pinned - !!a.pinned) || b.updated - a.updated).slice(0, 200)
        .map((c) => ({ id: c.id, title: c.title, created: c.created, updated: c.updated, n: c.messages.length,
          pinned: !!c.pinned, archived: !!c.archived, unread: !!c.unread }));
    },
    update(cid, b) {
      const c = this.db.convs[cid];
      if (b.title != null) {
        const t = String(b.title).split(/\s+/).filter(Boolean).join(" ").slice(0, 80);
        if (!t) throw new Error("The name can't be empty.");
        c.title = t;
      }
      for (const f of ["pinned", "archived", "unread"]) if (b[f] != null) c[f] = !!b[f];
      this.save();
      return { id: c.id, title: c.title, pinned: !!c.pinned, archived: !!c.archived, unread: !!c.unread };
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
    if (!CLOUD.url) return { route: "online", reason: "This website isn't connected to the VoiceAssist cloud yet" };
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

  // ---------------------------------------------------------------- the VoiceAssist cloud AI (one shared service, no user keys)
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
  async function cloudCall(body, signal) {
    const token = await accounts.token();
    let res;
    try {
      res = await nativeFetch(CLOUD.url + "/functions/v1/chat", { method: "POST", signal,
        headers: { apikey: CLOUD.key, Authorization: "Bearer " + token, "content-type": "application/json" }, body: JSON.stringify(body) });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      if (!navigator.onLine) setNet("OFFLINE", "No internet connection — reconnect to keep talking.");
      throw new HttpError(502, "Couldn't reach the VoiceAssist cloud.", { hint: navigator.onLine ? "Check your internet connection and try again." : "You're offline — reconnect, or use the installed app for offline use." });
    }
    if (!res.ok) {
      let j = {}; try { j = await res.json(); } catch (_) {}
      if (res.status === 401) throw new HttpError(401, "Your sign-in has expired — please log out and log in again.");
      throw new HttpError(res.status, j.error || `VoiceAssist cloud error ${res.status}.`, { hint: j.code === "limit" ? "Your questions reset at midnight (UTC). The installed apps also answer offline." : "" });
    }
    return res;
  }
  async function cloudAnswer(system, messages, maxTokens, signal, onText) {
    const res = await cloudCall({ system, messages, max_tokens: maxTokens }, signal);
    const left = res.headers.get("x-questions-left"), limit = res.headers.get("x-daily-limit");
    let done = false;
    for await (const line of sseLines(res, signal)) {
      if (!line.startsWith("data:")) continue;
      let ev; try { ev = JSON.parse(line.slice(5)); } catch (_) { continue; }
      if (ev.type === "content_block_delta" && ev.delta?.type === "text_delta") onText(ev.delta.text);
      else if (ev.type === "message_stop") done = true;
      else if (ev.type === "error") throw new HttpError(502, ev.error?.message || "The cloud AI returned an error.");
    }
    if (!done && !signal?.aborted) throw new HttpError(502, "The answer stopped before it was complete.", { hint: "Check your internet connection." });
    return { left: left == null ? null : Number(left), limit: limit == null ? null : Number(limit) };
  }

  // ---------------------------------------------------------------- speech to text
  async function transcribe(wav, signal) {
    const lang = (settings.data.stt.language || "en").slice(0, 2);
    if (settings.data.web.stt !== "browser") {
      const b64wav = await new Promise((resolve) => { const r = new FileReader(); r.onload = () => resolve(String(r.result).split(",")[1]); r.readAsDataURL(new Blob([wav], { type: "audio/wav" })); });
      const res = await cloudCall({ transcribe: b64wav }, signal);
      return { text: String((await res.json()).text || "").trim(), engine: "VoiceAssist cloud speech recognition" };
    }
    return { text: await chromeRecognize(wav, lang, signal), engine: "Chrome speech recognition" };
  }
  /** Chrome (135+) can recognise speech from an audio track: replay the recording into it. */
  async function chromeRecognize(wav, lang, signal) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const noVoice = new HttpError(400, "This browser can't recognise speech by itself.",
      { hint: "Switch Settings → Voice → \"Recognise my speech with\" back to VoiceAssist cloud, or type your question." });
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
        if (!CLOUD.url) { put("error", { message: "This website isn't connected to the VoiceAssist cloud yet." }); return; }
        let text = (typed || "").trim(), sttName = "typed", sttS = null;
        if (wav) {
          const t0 = performance.now();
          const tr = await transcribe(wav, ctrl.signal);
          text = tr.text; sttName = tr.engine; sttS = (performance.now() - t0) / 1000;
          put("transcript", { text, engine: sttName, seconds: sttS });
          if (!text) { put("no_speech", { reason: "nothing recognised" }); put("done", { spoken: false }); return; }
        }
        if (!text) { put("done", { spoken: false }); return; }
        let cid = convId;
        if (!history.exists(cid, userId)) cid = history.create(userId);
        const mid = history.add(cid, "user", text, { input: wav ? "voice" : "text", stt: sttName });
        put("user_saved", { conversation_id: cid, message_id: mid });
        const info = { kind: "cloud", provider: "voiceassist", label: "Cloud AI", model: "VoiceAssist" };
        put("llm_start", info);
        const ctx = buildContext(history.db.convs[cid].messages.map((m) => ({ role: m.role, content: m.content })), s.assistant.max_context_chars_online || 48000);
        const chunker = new SentenceChunker();
        let seq = 0, firstAudio = null, firstTok = null, answer = "";
        const speak = (chunk) => {
          const t = speakable(chunk); if (!t || s.web.tts === "none") return;
          if (firstAudio == null) firstAudio = performance.now();
          put("say", { seq: seq++, text: t, voice: s.web.browser_voice, rate: s.tts.speed || 1 });
        };
        const usage = await cloudAnswer(s.assistant.system_prompt, ctx, s.online.max_output_tokens || 1024, ctrl.signal, (d) => {
          if (firstTok == null) firstTok = performance.now();
          answer += d; put("token", { text: d }); chunker.feed(d).forEach(speak);
        });
        chunker.flush().forEach(speak);
        answer = answer.trim();
        if (ctrl.signal.aborted) { if (answer) history.add(cid, "assistant", answer, { ...info, interrupted: true }); put("cancelled", { stage: "answer", partial: answer }); return; }
        if (!answer) { put("error", { message: "The cloud AI returned an empty answer." }); return; }
        const amid = history.add(cid, "assistant", answer, info);
        const sec = (t) => (t == null ? null : Math.round((t - turn.started) / 10) / 100);
        put("done", { spoken: seq > 0, message_id: amid, conversation_id: cid,
          provider: { ...info, questions_left: usage.left, daily_limit: usage.limit },
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
  const PUBLIC = new Set(["/api/version", "/api/quit", "/api/auth/me", "/api/auth/signup", "/api/auth/login", "/api/auth/logout", "/api/auth/recover",
    "/api/auth/new_password", "/api/ping", "/api/bye"]);

  function state(user) {
    const r = route(user?.id);
    return { version: VERSION, platform: PLATFORM, settings: settings.data, network: snapshot(), route: r,
      cloud: { configured: !!CLOUD.url, label: "VoiceAssist cloud AI" },
      stt: { model: "browser", installed: true, loaded: true, error: null, available: ["browser"], vad: true, engine_ok: true },
      tts: { engine: settings.data.web.tts, active: settings.data.web.tts === "none" ? "none (text only)" : "Browser voice", voice: "", installed: true, voices: [], system_available: true, error: null },
      local: { state: "unknown", running: false, installed: [], recommended: [] },
      catalog: { stt: { browser: { note: "in the browser" } }, voices: {} },
      data_dir: "this browser (local storage)", models_dir: "— (website edition: no offline models)", user };
  }

  async function handle(path, init) {
    const method = (init?.method || "GET").toUpperCase();
    const url = new URL(path, location.origin);
    const p = url.pathname;
    const user = accounts.user();
    currentUserId = user?.id ?? null;
    if (!user && !PUBLIC.has(p)) return json({ error: "Please log in.", auth: "required" }, 401);
    const isAudio = (p === "/api/turn" || p === "/api/transcribe") && !(init?.headers?.["Content-Type"] || "").startsWith("application/json");
    const b = method === "POST" && !isAudio ? await bodyJson(init) : {};
    switch (`${method} ${p}`) {
      case "GET /api/version": return json({ version: VERSION });
      case "POST /api/ping": case "POST /api/bye": case "POST /api/quit": return json({ ok: true });
      case "GET /api/state": return json(state(user));
      case "GET /api/auth/me": return json({ user, has_accounts: true, platform: PLATFORM, cloud: !!CLOUD.url,
        recovery: !!(recovery && recovery.access_token), recovery_error: recovery?.error || null });
      case "POST /api/auth/signup": {
        const r = await accounts.signup(b.email, b.password, b.display_name, b.remember);
        return r.needs_confirmation ? json({ needs_confirmation: true, email: String(b.email).trim() }, 202) : json({ user: r.user, claimed_conversations: 0 }, 201);
      }
      case "POST /api/auth/login": return json({ user: await accounts.login(b.email, b.password, b.remember) });
      case "POST /api/auth/logout": if (current) current.ctrl.abort(); await accounts.logout(); return json({ ok: true });
      case "POST /api/auth/recover": {
        const email = String(b.email || "").trim();
        if (!EMAIL_RE.test(email)) throw acctErr("Enter the email address of your account.", "email");
        const redirect = CLOUD.site_url || (location.origin + location.pathname);
        await sb("POST", "/auth/v1/recover?redirect_to=" + encodeURIComponent(redirect), { email });
        return json({ ok: true });
      }
      case "POST /api/auth/new_password": {
        if (!recovery?.access_token) throw acctErr("This reset link has expired — ask for a new one.", "", 400);
        validate("x@y.zz", b.new_password);
        const u = await sb("PUT", "/auth/v1/user", { password: b.new_password }, recovery.access_token);
        const signed = accounts.store({ access_token: recovery.access_token, refresh_token: recovery.refresh_token, expires_in: recovery.expires_in, user: u }, true);
        recovery = null;
        return json({ user: signed });
      }
      case "POST /api/account/profile": {
        const d = String(b.display_name ?? "").trim().slice(0, 60);
        if (!d) throw acctErr("Name can't be empty.", "display_name");
        const u = await sb("PUT", "/auth/v1/user", { data: { display_name: d } }, await accounts.token());
        accounts.sess.user = pub(u); accounts.save();
        return json({ user: accounts.sess.user });
      }
      case "POST /api/account/password": {
        validate(user.email, b.new_password);
        await accounts.verify(b.current_password);
        await sb("PUT", "/auth/v1/user", { password: b.new_password }, await accounts.token());
        return json({ ok: true });
      }
      case "POST /api/account/delete": {
        await accounts.verify(b.password);
        await sb("POST", "/functions/v1/delete-account", {}, await accounts.token());
        history.clear(user.id); accounts.sess = null; accounts.save();
        return json({ ok: true });
      }
      case "GET /api/cloud/usage": {
        const r = await nativeFetch(CLOUD.url + "/functions/v1/chat", { headers: { apikey: CLOUD.key, Authorization: "Bearer " + (await accounts.token()) } });
        if (!r.ok) throw new HttpError(r.status, "Couldn't check the cloud usage.");
        return json(await r.json());
      }
      case "GET /api/settings": return json(settings.data);
      case "POST /api/settings": { const s = settings.update(b); publish({ type: "settings", settings: s }); return json({ ok: true, settings: s }); }
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
        if (route(user.id).route === "offline") throw new HttpError(503, "You're offline.", { hint: "Reconnect to the internet to keep talking." });
        const ctrl = new AbortController();
        const r = await transcribe(await new Response(init.body).arrayBuffer(), ctrl.signal);
        return json({ text: r.text, engine: r.engine });
      }
      case "POST /api/turn": {
        const ctype = (init?.headers?.["Content-Type"] || "").split(";")[0];
        const conv = url.searchParams.get("conversation_id");
        const stream = ctype === "application/json" ? runTurn(user.id, b.conversation_id || conv, null, b.text) : runTurn(user.id, conv, await new Response(init.body).arrayBuffer(), null);
        return new Response(stream, { status: 200, headers: { "content-type": "application/x-ndjson" } });
      }
      case "POST /api/tts/test": return json({ say: b.text || "Hello! This is how I sound." });
    }
    if (p.startsWith("/api/conversations/")) {
      const cid = p.slice(19);
      if (!history.exists(cid, user.id)) return json({ error: "conversation not found" }, 404);
      if (method === "DELETE") { history.del(cid); return json({ ok: true }); }
      if (method === "POST") {
        try { return json({ ok: true, conversation: history.update(cid, b) }); }
        catch (e) { return json({ error: e.message }, 400); }
      }
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
    catch (e) { return json({ error: e.message || String(e), hint: e.hint || "", field: e.field || "", ...(e.auth ? { auth: e.auth } : {}) }, e.status || 500); }
  };
  const NativeES = window.EventSource;
  window.EventSource = function (url, opts) { return String(url).startsWith("/api/events") ? new FakeEventSource(url) : new NativeES(url, opts); };
  const nativeBeacon = navigator.sendBeacon?.bind(navigator);
  navigator.sendBeacon = (url, data) => (String(url).startsWith("/api/") ? true : nativeBeacon ? nativeBeacon(url, data) : false);
  window.VA_WEB = { PLATFORM, speakable };
})();
