// Deletes the signed-in user's VoiceAssist account (their usage rows go with it).
import { createClient } from "npm:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false, autoRefreshToken: false },
});

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  const token = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
  const { data } = token ? await admin.auth.getUser(token) : { data: { user: null } };
  if (!data?.user) return new Response(JSON.stringify({ error: "Please sign in again." }), { status: 401, headers: { ...CORS, "content-type": "application/json" } });
  const { error } = await admin.auth.admin.deleteUser(data.user.id);
  return new Response(JSON.stringify(error ? { error: error.message } : { ok: true }),
    { status: error ? 500 : 200, headers: { ...CORS, "content-type": "application/json" } });
});
