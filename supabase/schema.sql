-- TwinGate schema. Paste into Supabase -> SQL Editor -> Run. Safe to re-run.

create table if not exists public.missions (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  author      text not null default 'anon',
  prompt      text not null check (char_length(prompt) between 1 and 400),
  -- queued -> planning -> rehearsing -> awaiting_approval -> approved -> executing -> done
  -- (or rejected / blocked / failed)
  status      text not null default 'queued',
  plan        jsonb,          -- recorded steps from the sim rehearsal
  safety      jsonb,          -- {passed, score, checks:[{name, ok, detail}]}
  summary     text,
  error       text,
  skill_used  text,
  updated_at  timestamptz not null default now()
);

create table if not exists public.events (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  mission_id  bigint references public.missions(id) on delete cascade,
  stage       text not null,  -- think | tool | sim | safety | live | info | error | frame
  message     text,
  data        jsonb
);

create table if not exists public.skills (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  name        text unique not null,
  description text,
  steps       jsonb not null,
  author      text,
  uses        int not null default 0
);

create index if not exists events_mission_idx on public.events (mission_id, id);
create index if not exists missions_status_idx on public.missions (status, id);

create or replace function public.touch_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;
drop trigger if exists missions_touch on public.missions;
create trigger missions_touch before update on public.missions
  for each row execute function public.touch_updated_at();

-- Realtime
do $$ begin
  begin alter publication supabase_realtime add table public.missions; exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.events;   exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.skills;   exception when duplicate_object then null; end;
end $$;

-- RLS: the crowd (anon key) can read everything, submit missions, and the
-- operator console can approve/reject. The worker uses the service role key.
alter table public.missions enable row level security;
alter table public.events   enable row level security;
alter table public.skills   enable row level security;

drop policy if exists "read missions" on public.missions;
drop policy if exists "submit missions" on public.missions;
drop policy if exists "operator decides" on public.missions;
drop policy if exists "read events" on public.events;
drop policy if exists "read skills" on public.skills;

create policy "read missions"  on public.missions for select using (true);
create policy "submit missions" on public.missions for insert
  with check (status = 'queued' and plan is null and safety is null);
create policy "operator decides" on public.missions for update
  using (status = 'awaiting_approval')
  with check (status in ('approved', 'rejected'));
create policy "read events" on public.events for select using (true);
create policy "read skills" on public.skills for select using (true);

-- Seed skills so the library is not empty on stage (SO-101 style joint names;
-- the worker remaps by joint order if your twin uses other names).
insert into public.skills (name, description, steps, author) values
  ('home', 'Return to a safe neutral pose',
   '[{"type":"joints","targets_deg":{"0":0,"1":0,"2":0,"3":0,"4":0},"duration":1.2}]', 'seed'),
  ('wave', 'Friendly wave hello',
   '[{"type":"joints","targets_deg":{"0":0,"1":-20,"2":-60,"3":0,"4":0},"duration":1.0},
     {"type":"joints","targets_deg":{"3":35},"duration":0.4},
     {"type":"joints","targets_deg":{"3":-35},"duration":0.4},
     {"type":"joints","targets_deg":{"3":35},"duration":0.4},
     {"type":"joints","targets_deg":{"3":0},"duration":0.4}]', 'seed'),
  ('nod', 'Nod yes',
   '[{"type":"joints","targets_deg":{"3":25},"duration":0.4},
     {"type":"joints","targets_deg":{"3":-10},"duration":0.4},
     {"type":"joints","targets_deg":{"3":25},"duration":0.4},
     {"type":"joints","targets_deg":{"3":0},"duration":0.4}]', 'seed')
on conflict (name) do nothing;

-- Robot eyes: any phone can publish the latest camera frame to frames/eyes/latest.jpg
insert into storage.buckets (id, name, public) values ('frames','frames',true) on conflict (id) do update set public = true;
drop policy if exists "eyes upload" on storage.objects;
drop policy if exists "eyes update" on storage.objects;
drop policy if exists "eyes read" on storage.objects;
create policy "eyes upload" on storage.objects for insert to anon, authenticated with check (bucket_id = 'frames' and name = 'eyes/latest.jpg');
create policy "eyes update" on storage.objects for update to anon, authenticated using (bucket_id = 'frames' and name = 'eyes/latest.jpg') with check (bucket_id = 'frames' and name = 'eyes/latest.jpg');
create policy "eyes read" on storage.objects for select to anon, authenticated using (bucket_id = 'frames');
