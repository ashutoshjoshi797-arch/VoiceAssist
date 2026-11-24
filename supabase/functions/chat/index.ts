// VoiceAssist "chat" Edge Function — the one AI endpoint for every user of the product.
//   POST { system, messages: [{role, content}], max_tokens? }  -> streamed answer (server-sent events, see below)
//   POST { transcribe: "<base64 WAV>" }                        -> { text }  (speech to text, for the website)
//   GET                                                         -> { used, limit, left } for today
// Requires a signed-in user (Authorization: Bearer <access token>). The AI key (Gemini, free tier) never leaves
// this server. The stream uses the same simple event format as Anthropic's API so every app parses it the same
// way:  data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}  …  data: {"type":"message_stop"}
import { createClient } from "npm:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Expose-Headers": "x-questions-left, x-daily-limit",
};
const json = (o: unknown, status = 200, extra: Record<string, string> = {}) =>
  new Response(JSON.stringify(o), { status, headers: { ...CORS, "content-type": "application/json", ...extra } });

const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false, autoRefreshToken: false },
});
const GEMINI = (Deno.env.get("GEMINI_BASE") ?? "https://generativelanguage.googleapis.com") + "/v1beta/models/";   // GEMINI_BASE: tests only

let cfgCache: { at: number; cfg: Record<string, string> } | null = null;
async function config() {
  if (cfgCache && Date.now() - cfgCache.at < 60_000) return cfgCache.cfg;
  const { data } = await admin.from("app_config").select("key,value");
  const cfg: Record<string, string> = { daily_limit: "50", model: "gemini-3.6-flash", max_tokens: "1024", max_context_chars: "48000" };
  for (const r of data ?? []) cfg[r.key] = r.value;
  cfgCache = { at: Date.now(), cfg };
  return cfg;
}
const today = () => new Date().toISOString().slice(0, 10);

function geminiError(status: number, detail: string) {
  console.error("gemini error", status, detail.slice(0, 500));
  if (status === 429 || status === 503) return json({ error: "The free AI has reached its limit for the moment — please try again in a minute (or later today).", code: "busy" }, 503);
  if (status === 400 || status === 403) return json({ error: "The VoiceAssist cloud AI key isn't working — the app owner needs to check it.", code: "key" }, 502);
  return json({ error: `The cloud AI had a problem (${status}). Please try again.` }, 502);
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  const token = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  const { data: auth } = token ? await admin.auth.getUser(token) : { data: { user: null } };
  const user = auth?.user;
  if (!user) return json({ error: "Please sign in to VoiceAssist again.", code: "auth" }, 401);

  const cfg = await config();
  const limit = Math.max(0, parseInt(cfg.daily_limit, 10) || 50);
  if (req.method === "GET") {
    const { data } = await admin.from("usage_daily").select("count").eq("user_id", user.id).eq("day", today()).maybeSingle();
    const used = data?.count ?? 0;
    return json({ used, limit, left: Math.max(0, limit - used), model: cfg.model });
  }
  if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

  const key = Deno.env.get("GEMINI_API_KEY");
  if (!key) return json({ error: "The VoiceAssist cloud isn't set up yet (no AI key on the server).", code: "not_configured" }, 503);
  let body: { system?: unknown; messages?: unknown; max_tokens?: unknown; transcribe?: unknown };
  try { body = await req.json(); } catch { return json({ error: "bad request" }, 400); }

  // ---- speech to text (website voice input); not counted as a question
  if (typeof body.transcribe === "string") {
    if (body.transcribe.length > 4_000_000) return json({ error: "Recording too long." }, 413);
    const tModels = [cfg.transcribe_model || cfg.model, ...(cfg.fallback_models ?? "gemini-3.6-flash,gemini-3.5-flash-lite").split(",")]
      .map((m) => m.trim()).filter((m, i, a) => m && a.indexOf(m) === i);
    const tBody = JSON.stringify({ contents: [{ role: "user", parts: [
      { inline_data: { mime_type: "audio/wav", data: body.transcribe } },
      { text: "Transcribe this speech exactly as spoken. Reply with only the transcript. If there is no speech, reply with nothing." }] }],
      generationConfig: { temperature: 0 } });
    let r: Response | null = null;
    for (const model of tModels) {
      r = await fetch(`${GEMINI}${encodeURIComponent(model)}:generateContent`, {
        method: "POST", headers: { "x-goog-api-key": key, "content-type": "application/json" }, body: tBody,
      }).catch(() => null);
      if (r && (r.ok || ![429, 500, 503, 504].includes(r.status))) break;
    }
    if (!r) return geminiError(503, "no response");
    if (!r.ok) return geminiError(r.status, await r.text().catch(() => ""));
    const j = await r.json();
    const text = (j.candidates?.[0]?.content?.parts ?? []).filter((p: { thought?: boolean }) => !p.thought)
      .map((p: { text?: string }) => p.text ?? "").join("").trim().replace(/^"|"$/g, "");
    return json({ text });
  }

  // ---- answer a question: clean up the conversation (alternating turns, newest kept within the size budget)
  const budget = parseInt(cfg.max_context_chars, 10) || 48000;
  const msgs: { role: "user" | "assistant"; content: string }[] = [];
  for (const m of (Array.isArray(body.messages) ? body.messages : []) as { role?: string; content?: unknown }[]) {
    const role = m?.role === "assistant" ? "assistant" : "user";
    const content = typeof m?.content === "string" ? m.content.trim() : "";
    if (!content) continue;
    if (msgs.length && msgs[msgs.length - 1].role === role) msgs[msgs.length - 1].content += "\n\n" + content;
    else msgs.push({ role, content });
  }
  let used = 0, start = msgs.length;
  while (start > 0 && used + msgs[start - 1].content.length <= budget) used += msgs[--start].content.length;
  const ctx = msgs.slice(Math.min(start, Math.max(0, msgs.length - 1)));
  while (ctx.length && ctx[0].role !== "user") ctx.shift();
  if (!ctx.length || ctx[ctx.length - 1].role !== "user") return json({ error: "No question to answer." }, 400);
  const system = typeof body.system === "string" ? body.system.slice(0, 4000) : "";
  const maxTokens = Math.min(parseInt(cfg.max_tokens, 10) || 1024, Number(body.max_tokens) || 1024);

  const { data: left, error: qErr } = await admin.rpc("take_question", { p_user: user.id, p_limit: limit });
  if (qErr) return json({ error: "Usage check failed: " + qErr.message }, 500);
  if (left < 0) {
    return json({ error: `You've used all ${limit} cloud questions for today. They reset at midnight UTC — the offline AI keeps working meanwhile.`, code: "limit" },
      429, { "x-questions-left": "0", "x-daily-limit": String(limit) });
  }

  // Free Gemini models are sometimes "experiencing high demand" (503) or rate-limited (429): try the configured
  // model first, then the fallbacks, each with a time limit for the answer to start.
  const models = [cfg.model, ...(cfg.fallback_models ?? "gemini-3.6-flash,gemini-3.5-flash-lite").split(",")]
    .map((m) => m.trim()).filter((m, i, a) => m && a.indexOf(m) === i);
  const payload = JSON.stringify({
    contents: ctx.map((m) => ({ role: m.role === "assistant" ? "model" : "user", parts: [{ text: m.content }] })),
    systemInstruction: system ? { parts: [{ text: system }] } : undefined,
    generationConfig: { maxOutputTokens: maxTokens },
  });
  let upstream: Response | null = null, lastStatus = 0, lastDetail = "";
  for (const model of models) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), (parseInt(cfg.start_timeout_s, 10) || 25) * 1000);
    try {
      const r = await fetch(`${GEMINI}${encodeURIComponent(model)}:streamGenerateContent?alt=sse`, {
        method: "POST", headers: { "x-goog-api-key": key, "content-type": "application/json" }, body: payload, signal: ctrl.signal,
      });
      clearTimeout(timer);
      if (r.ok && r.body) { upstream = r; break; }
      lastStatus = r.status; lastDetail = await r.text().catch(() => "");
      console.error("gemini", model, r.status, lastDetail.slice(0, 300));
      if (![429, 500, 503, 504].includes(r.status)) break;   // a key/request problem: another model won't help
    } catch (e) {
      clearTimeout(timer);
      lastStatus = 504; lastDetail = String(e);
      console.error("gemini", model, "no response", String(e));
    }
  }
  if (!upstream || !upstream.body) {
    await admin.rpc("refund_question", { p_user: user.id });
    return geminiError(lastStatus === 504 ? 503 : lastStatus, lastDetail);
  }

  // re-emit Gemini's stream in the simple shared format
  const enc = new TextEncoder(), dec = new TextDecoder();
  const send = (c: ReadableStreamDefaultController, o: unknown) => c.enqueue(enc.encode(`data: ${JSON.stringify(o)}\n\n`));
  const reader = upstream.body.getReader();
  const out = new ReadableStream({
    async start(c) {
      let buf = "", finished = false, gotText = false;
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
            if (!line.startsWith("data:")) continue;
            let ev; try { ev = JSON.parse(line.slice(5)); } catch { continue; }
            const cand = ev.candidates?.[0];
            for (const p of cand?.content?.parts ?? []) {
              if (p.text && !p.thought) { gotText = true; send(c, { type: "content_block_delta", delta: { type: "text_delta", text: p.text } }); }
            }
            if (cand?.finishReason) finished = true;
          }
        }
        if (finished || gotText) send(c, { type: "message_stop" });
        else send(c, { type: "error", error: { message: "The cloud AI returned no answer." } });
      } catch (e) {
        send(c, { type: "error", error: { message: "The answer was interrupted: " + String(e) } });
      }
      c.close();
    },
    cancel() { reader.cancel().catch(() => {}); },
  });
  return new Response(out, {
    headers: { ...CORS, "content-type": "text/event-stream", "cache-control": "no-cache",
      "x-questions-left": String(left), "x-daily-limit": String(limit) },
  });
});
