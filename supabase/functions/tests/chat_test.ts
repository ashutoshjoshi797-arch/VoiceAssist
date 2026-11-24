// Functional test of the "chat" Edge Function with a stand-in Supabase (auth + PostgREST) and a stand-in Gemini.
//   deno test --allow-net --allow-env --allow-read supabase/functions/tests/chat_test.ts
function assert(c: unknown, msg = "assertion failed"): asserts c { if (!c) throw new Error(msg); }
function assertEquals(a: unknown, b: unknown) {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

const PORT = 54401;
let used = 0;
const LIMIT = 3;
const geminiBodies: any[] = [];
let geminiMode: "ok" | "429" | "busy-first" = "ok";
const modelsTried: string[] = [];

Deno.serve({ port: PORT, onListen() {} }, async (req) => {
  const u = new URL(req.url);
  if (u.pathname === "/auth/v1/user") {
    return (req.headers.get("authorization") === "Bearer good-token")
      ? Response.json({ id: "11111111-1111-1111-1111-111111111111", email: "t@example.com", aud: "authenticated", role: "authenticated" })
      : Response.json({ msg: "invalid JWT" }, { status: 401 });
  }
  if (u.pathname === "/rest/v1/app_config") return Response.json([{ key: "daily_limit", value: String(LIMIT) }, { key: "model", value: "gemini-test" }]);
  if (u.pathname === "/rest/v1/usage_daily") return Response.json(used ? { count: used } : null);
  if (u.pathname === "/rest/v1/rpc/take_question") { used++; return Response.json(used > LIMIT ? (used--, -1) : LIMIT - used); }
  if (u.pathname === "/rest/v1/rpc/refund_question") { used = Math.max(0, used - 1); return new Response(null, { status: 204 }); }
  if (u.pathname.includes(":streamGenerateContent")) modelsTried.push(u.pathname.split("/").pop()!.split(":")[0]);
  if (u.pathname.includes(":streamGenerateContent") && geminiMode === "429") return Response.json({ error: { code: 429 } }, { status: 429 });
  if (u.pathname.includes(":streamGenerateContent") && geminiMode === "busy-first" && u.pathname.includes("gemini-test")) {
    return Response.json({ error: { code: 503, message: "high demand" } }, { status: 503 });
  }
  if (u.pathname.includes(":streamGenerateContent") && (u.pathname.includes("gemini-test") || geminiMode === "busy-first")) {
    geminiBodies.push({ key: req.headers.get("x-goog-api-key"), body: await req.json() });
    if (geminiMode === "429") return Response.json({ error: { code: 429 } }, { status: 429 });
    const ev = (o: unknown) => `data: ${JSON.stringify(o)}\r\n\r\n`;
    return new Response(ev({ candidates: [{ content: { parts: [{ text: "Paris is the", thought: false }, { text: "(thinking)", thought: true }] } }] }) +
      ev({ candidates: [{ content: { parts: [{ text: " capital." }] }, finishReason: "STOP" }] }), { headers: { "content-type": "text/event-stream" } });
  }
  if (u.pathname.startsWith("/v1beta/models/gemini-test:generateContent")) {
    return Response.json({ candidates: [{ content: { parts: [{ text: "What is the capital of France?" }] } }] });
  }
  return Response.json({ error: "not found " + u.pathname }, { status: 404 });
});

Deno.env.set("SUPABASE_URL", `http://127.0.0.1:${PORT}`);
Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-key");
Deno.env.set("GEMINI_API_KEY", "gemini-secret");
Deno.env.set("GEMINI_BASE", `http://127.0.0.1:${PORT}`);
await import("../chat/index.ts");   // Deno.serve on :8000
const FN = "http://127.0.0.1:8000";
const call = (init: RequestInit = {}, token = "good-token") =>
  fetch(FN, { ...init, headers: { authorization: `Bearer ${token}`, "content-type": "application/json", ...(init.headers ?? {}) } });
const ask = (q: string) => call({ method: "POST", body: JSON.stringify({ system: "Be brief.", messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "Hello!" }, { role: "user", content: q }], max_tokens: 99999 }) });

Deno.test({ name: "chat function", sanitizeOps: false, sanitizeResources: false, async fn(t) {
  await t.step("rejects a request without a valid sign-in", async () => {
    const r = await call({ method: "POST", body: "{}" }, "bad-token");
    assertEquals(r.status, 401); await r.body?.cancel();
  });
  await t.step("streams the answer in the shared format, with questions left", async () => {
    const r = await ask("Capital of France?");
    assertEquals(r.status, 200);
    assertEquals(r.headers.get("x-questions-left"), String(LIMIT - 1));
    const txt = await r.text();
    const evs = txt.split("\n").filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)));
    assertEquals(evs.filter((e) => e.type === "content_block_delta").map((e) => e.delta.text).join(""), "Paris is the capital.");
    assertEquals(evs.at(-1).type, "message_stop");
    const g = geminiBodies.at(-1);
    assertEquals(g.key, "gemini-secret");
    assertEquals(g.body.contents.map((c: any) => c.role), ["user", "model", "user"]);
    assertEquals(g.body.systemInstruction.parts[0].text, "Be brief.");
    assert(g.body.generationConfig.maxOutputTokens <= 1024, "max tokens capped by app_config");
  });
  await t.step("usage", async () => {
    const r = await call();
    assertEquals(await r.json(), { used: 1, limit: LIMIT, left: LIMIT - 1, model: "gemini-test" });
  });
  await t.step("Gemini rate limit -> friendly 503, question refunded", async () => {
    geminiMode = "429";
    const r = await ask("again");
    assertEquals(r.status, 503);
    assertEquals((await r.json()).code, "busy");
    assertEquals(used, 1);
    geminiMode = "ok";
  });
  await t.step("busy model -> falls back to the next free model", async () => {
    geminiMode = "busy-first"; modelsTried.length = 0;
    const r = await ask("fallback please");
    assertEquals(r.status, 200);
    const txt = await r.text();
    assert(txt.includes("Paris is the"), txt);
    assertEquals(modelsTried, ["gemini-test", "gemini-3.6-flash"]);
    geminiMode = "ok";
  });
  await t.step("daily limit", async () => {
    const r1 = await ask("two"); assertEquals(r1.status, 200); await r1.text();
    const r2 = await ask("three");
    assertEquals(r2.status, 429);
    const j = await r2.json();
    assertEquals(j.code, "limit");
    assert(j.error.includes("for today"));
  });
  await t.step("transcription (website voice input)", async () => {
    const r = await call({ method: "POST", body: JSON.stringify({ transcribe: btoa("RIFF....WAVE") }) });
    assertEquals((await r.json()).text, "What is the capital of France?");
  });
  await t.step("CORS preflight", async () => {
    const r = await fetch(FN, { method: "OPTIONS" });
    assert(r.headers.get("access-control-expose-headers")?.includes("x-questions-left")); await r.body?.cancel();
  });
}});
