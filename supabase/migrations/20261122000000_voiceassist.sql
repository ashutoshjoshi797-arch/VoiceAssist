-- VoiceAssist shared cloud backend.
-- Users sign in with Supabase Auth. Every question goes through the "chat" Edge Function, which holds the
-- product's single Gemini key (secret GEMINI_API_KEY, free tier) and enforces a per-user daily limit.

-- settings the owner can change in the Table Editor (no redeploy needed)
create table if not exists public.app_config (
  key   text primary key,
  value text not null
);
insert into public.app_config (key, value) values
  ('daily_limit', '50'),                 -- questions per user per day (UTC)
  ('model', 'gemini-3.6-flash'),         -- Gemini model used for every answer (must be a free-tier model)
  ('fallback_models', 'gemini-3.5-flash-lite,gemini-3.8-flash,gemini-3.1-flash-lite'),   -- tried when it's busy
  ('start_timeout_s', '15'),             -- seconds to wait for a model to start answering before trying the next
  ('max_tokens', '1024'),                -- longest answer
  ('max_context_chars', '48000')         -- how much conversation history is sent along
on conflict (key) do nothing;
alter table public.app_config enable row level security;   -- no policies: only the server (service role) reads it

-- questions asked per user per day
create table if not exists public.usage_daily (
  user_id uuid not null references auth.users (id) on delete cascade,
  day     date not null default ((now() at time zone 'utc')::date),
  count   integer not null default 0,
  primary key (user_id, day)
);
alter table public.usage_daily enable row level security;
drop policy if exists "users read their own usage" on public.usage_daily;
create policy "users read their own usage" on public.usage_daily for select to authenticated using ((select auth.uid()) = user_id);

-- atomically take one question from today's allowance; returns questions left, or -1 when the limit is reached
create or replace function public.take_question(p_user uuid, p_limit integer)
returns integer language plpgsql security definer set search_path = public as $$
declare c integer;
begin
  insert into usage_daily (user_id, day, count) values (p_user, (now() at time zone 'utc')::date, 1)
  on conflict (user_id, day) do update set count = usage_daily.count + 1
  returning count into c;
  if c > p_limit then
    update usage_daily set count = count - 1 where user_id = p_user and day = (now() at time zone 'utc')::date;
    return -1;
  end if;
  return p_limit - c;
end $$;

-- give a question back when the AI could not answer it
create or replace function public.refund_question(p_user uuid)
returns void language sql security definer set search_path = public as $$
  update usage_daily set count = greatest(count - 1, 0)
  where user_id = p_user and day = (now() at time zone 'utc')::date;
$$;

revoke all on function public.take_question(uuid, integer) from public, anon, authenticated;
revoke all on function public.refund_question(uuid) from public, anon, authenticated;
grant execute on function public.take_question(uuid, integer) to service_role;
grant execute on function public.refund_question(uuid) to service_role;
grant select on public.app_config to service_role;
grant select, insert, update on public.usage_daily to service_role;
