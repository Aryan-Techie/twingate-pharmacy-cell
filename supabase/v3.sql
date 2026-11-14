-- TwinGate v3: teach mode, leaderboard, abuse protection. Safe to re-run.

-- Teach mode: one shared "teach slot" a phone drives live, plus keyframe submissions.
create table if not exists public.teach (
  id int primary key default 1 check (id = 1),
  owner text,
  owner_name text,
  joints_deg jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
insert into public.teach (id) values (1) on conflict (id) do nothing;

create table if not exists public.teach_submissions (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  author text not null default 'anon' check (char_length(author) <= 24),
  name text not null check (name ~ '^[a-z0-9_]{2,24}$'),
  keyframes jsonb not null check (jsonb_typeof(keyframes) = 'array' and jsonb_array_length(keyframes) between 1 and 12),
  status text not null default 'new',
  mission_id bigint
);

alter table public.teach enable row level security;
alter table public.teach_submissions enable row level security;
drop policy if exists "read teach" on public.teach;
drop policy if exists "drive teach" on public.teach;
drop policy if exists "read teach subs" on public.teach_submissions;
drop policy if exists "submit teach" on public.teach_submissions;
create policy "read teach" on public.teach for select using (true);
create policy "drive teach" on public.teach for update using (id = 1)
  with check (id = 1 and char_length(coalesce(owner, '')) <= 64 and jsonb_typeof(joints_deg) = 'object');
create policy "read teach subs" on public.teach_submissions for select using (true);
create policy "submit teach" on public.teach_submissions for insert with check (status = 'new' and mission_id is null);

do $$ begin
  begin alter publication supabase_realtime add table public.teach; exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.teach_submissions; exception when duplicate_object then null; end;
end $$;

-- Abuse protection: per-device and global rate limits on mission intake.
alter table public.missions add column if not exists client_id text;

create or replace function public.enforce_mission_limits() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  role text := coalesce(current_setting('request.jwt.claims', true)::json ->> 'role', 'service_role');
  n_client int; n_all int; n_pending int;
begin
  if role = 'service_role' then return new; end if;
  select count(*) into n_pending from missions where status in ('queued', 'planning', 'rehearsing', 'awaiting_approval');
  if n_pending >= 12 then raise exception 'The robot is busy: % missions waiting. Try again in a minute.', n_pending; end if;
  select count(*) into n_all from missions where created_at > now() - interval '60 seconds';
  if n_all >= 40 then raise exception 'Too many requests right now. Try again in a minute.'; end if;
  if new.client_id is not null then
    select count(*) into n_client from missions where client_id = new.client_id and created_at > now() - interval '60 seconds';
    if n_client >= 3 then raise exception 'Slow down: max 3 missions per minute per phone.'; end if;
  end if;
  return new;
end $$;
drop trigger if exists missions_limits on public.missions;
create trigger missions_limits before insert on public.missions for each row execute function public.enforce_mission_limits();

-- Leaderboard: computed live from missions, skills and votes.
create or replace view public.leaderboard with (security_invoker = true) as
with m as (
  select author,
         count(*) as missions,
         count(*) filter (where status in ('awaiting_approval', 'approved', 'executing', 'done')) as passed,
         count(*) filter (where status = 'done') as real_runs
  from missions where author not in ('sentry', 'anon') group by author
), s as (
  select author, count(*) as skills, coalesce(sum(uses), 0) as skill_uses
  from skills where author is not null and author not in ('seed', 'sentry') group by author
)
select coalesce(m.author, s.author) as author,
       coalesce(m.missions, 0) as missions, coalesce(m.passed, 0) as passed, coalesce(m.real_runs, 0) as real_runs,
       coalesce(s.skills, 0) as skills, coalesce(s.skill_uses, 0) as skill_uses,
       (coalesce(m.passed, 0) * 5 + coalesce(m.real_runs, 0) * 10 + coalesce(s.skills, 0) * 15 + coalesce(s.skill_uses, 0) * 5)::int as points
from m full outer join s on s.author = m.author
order by points desc, missions desc;
grant select on public.leaderboard to anon, authenticated;
