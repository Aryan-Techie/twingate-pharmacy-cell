-- TwinGate v2: crowd vote, operator PIN, E-STOP, fuzzy skill memory. Safe to re-run.
create extension if not exists pg_trgm;

create table if not exists public.secrets (key text primary key, value text not null);
alter table public.secrets enable row level security;           -- no policies: invisible to anon

create table if not exists public.control (
  id int primary key default 1 check (id = 1),
  estop boolean not null default false,
  crowd_quorum int not null default 3 check (crowd_quorum between 1 and 50),
  updated_at timestamptz not null default now()
);
insert into public.control (id) values (1) on conflict (id) do nothing;

create table if not exists public.votes (
  mission_id bigint not null references public.missions(id) on delete cascade,
  voter      text not null check (char_length(voter) between 8 and 64),
  approve    boolean not null,
  created_at timestamptz not null default now(),
  primary key (mission_id, voter)
);

do $$ begin
  begin alter publication supabase_realtime add table public.control; exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.votes;   exception when duplicate_object then null; end;
end $$;

alter table public.control enable row level security;
alter table public.votes   enable row level security;
drop policy if exists "read control" on public.control;
drop policy if exists "read votes" on public.votes;
drop policy if exists "vote while pending" on public.votes;
create policy "read control" on public.control for select using (true);
create policy "read votes" on public.votes for select using (true);
create policy "vote while pending" on public.votes for insert with check (
  exists (select 1 from public.missions m where m.id = mission_id and m.status = 'awaiting_approval'));

-- The anon key can no longer approve directly: decisions go through the PIN-checked RPC or the crowd vote.
drop policy if exists "operator decides" on public.missions;

create or replace function public.operator_check(p_pin text) returns boolean
language sql security definer set search_path = public stable as $$
  select exists (select 1 from secrets where key = 'operator_pin' and value = p_pin)
$$;

create or replace function public.operator_decide(p_mission bigint, p_status text, p_pin text) returns text
language plpgsql security definer set search_path = public as $$
begin
  if not operator_check(p_pin) then raise exception 'bad operator pin'; end if;
  if p_status not in ('approved', 'rejected') then raise exception 'bad status'; end if;
  update missions set status = p_status where id = p_mission and status = 'awaiting_approval';
  return p_status;
end $$;

create or replace function public.operator_estop(p_on boolean, p_pin text) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  if not operator_check(p_pin) then raise exception 'bad operator pin'; end if;
  update control set estop = p_on, updated_at = now() where id = 1;
  if p_on then
    update missions set status = 'rejected', error = 'E-STOP pressed'
      where status in ('awaiting_approval', 'approved', 'queued');
  end if;
  return p_on;
end $$;

create or replace function public.operator_quorum(p_quorum int, p_pin text) returns int
language plpgsql security definer set search_path = public as $$
begin
  if not operator_check(p_pin) then raise exception 'bad operator pin'; end if;
  update control set crowd_quorum = p_quorum, updated_at = now() where id = 1;
  return p_quorum;
end $$;

-- Robot memory in Postgres: closest learned skill for a loosely worded request.
create or replace function public.match_skill(q text) returns table (name text, score real)
language sql stable set search_path = public, extensions as $$
  select s.name,
         least(similarity(replace(s.name, '_', ' '), lower(q)) * 1.6,
               word_similarity(replace(s.name, '_', ' '), lower(q)))::real as score
  from skills s
  order by score desc
  limit 1
$$;

revoke all on function public.operator_check(text) from public;
grant execute on function public.operator_check(text) to anon, authenticated;
grant execute on function public.operator_decide(bigint, text, text) to anon, authenticated;
grant execute on function public.operator_estop(boolean, text) to anon, authenticated;
grant execute on function public.operator_quorum(int, text) to anon, authenticated;
grant execute on function public.match_skill(text) to anon, authenticated, service_role;
