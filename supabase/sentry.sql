-- Sentry mode: vision watch rule + reports. Safe to re-run.
create table if not exists public.watch (
  id          int primary key default 1 check (id = 1),
  enabled     boolean not null default false,
  rule        text not null default 'Report anything interesting. Alert if a person waves at the robot.',
  interval_s  int not null default 8 check (interval_s between 4 and 120),
  updated_at  timestamptz not null default now()
);
insert into public.watch (id) values (1) on conflict (id) do nothing;

create table if not exists public.reports (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  image_url   text,
  alert       boolean not null default false,
  title       text,
  detail      text,
  rule        text,
  mission_id  bigint references public.missions(id) on delete set null
);

do $$ begin
  begin alter publication supabase_realtime add table public.watch;   exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.reports; exception when duplicate_object then null; end;
end $$;

alter table public.watch   enable row level security;
alter table public.reports enable row level security;
drop policy if exists "read watch" on public.watch;
drop policy if exists "operator sets watch" on public.watch;
drop policy if exists "read reports" on public.reports;
create policy "read watch" on public.watch for select using (true);
create policy "operator sets watch" on public.watch for update using (id = 1)
  with check (id = 1 and char_length(rule) between 3 and 300);
create policy "read reports" on public.reports for select using (true);
