-- GuardianHome incidents. Safe to re-run.
create table if not exists public.incidents (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  source      text not null default 'camera',        -- camera | simulated
  status      text not null default 'detected',      -- detected, responding, awaiting_ack, resolved, help_requested, escalated
  title       text,
  detail      text,
  image_url   text,
  mission_id  bigint,
  escalate_at timestamptz,
  resolved_by text,
  updated_at  timestamptz not null default now()
);
drop trigger if exists incidents_touch on public.incidents;
create trigger incidents_touch before update on public.incidents for each row execute function public.touch_updated_at();
alter table public.incidents enable row level security;
drop policy if exists "read incidents" on public.incidents;
drop policy if exists "simulate fall" on public.incidents;
drop policy if exists "caregiver answers" on public.incidents;
create policy "read incidents" on public.incidents for select using (true);
create policy "simulate fall" on public.incidents for insert with check (status = 'detected' and source = 'simulated' and mission_id is null);
create policy "caregiver answers" on public.incidents for update
  using (status in ('responding', 'awaiting_ack'))
  with check (status in ('resolved', 'help_requested'));
do $$ begin
  begin alter publication supabase_realtime add table public.incidents; exception when duplicate_object then null; end;
end $$;
