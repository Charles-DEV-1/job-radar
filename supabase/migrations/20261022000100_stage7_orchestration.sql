begin;

create table public.heartbeats (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null unique,
    completed_at timestamptz not null default now(),
    steps jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint heartbeats_steps_object check (jsonb_typeof(steps) = 'object')
);
create index heartbeats_completed_at_idx on public.heartbeats(completed_at desc);
alter table public.heartbeats enable row level security;
revoke all on public.heartbeats from public, anon, authenticated;
grant select, insert on public.heartbeats to service_role;

commit;
