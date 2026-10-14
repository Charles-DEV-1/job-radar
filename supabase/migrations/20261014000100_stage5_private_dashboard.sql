begin;

-- This starts deny-all. Configure the sole owner from ALLOWED_EMAIL using the
-- service-role-only RPC; application environment variables are not SQL settings.
create table public.dashboard_settings (
    singleton boolean primary key default true check (singleton),
    allowed_email text not null check (allowed_email = lower(btrim(allowed_email)) and allowed_email like '%@%')
);
alter table public.dashboard_settings enable row level security;
revoke all on public.dashboard_settings from public, anon, authenticated;
grant select, insert, update on public.dashboard_settings to service_role;

create function public.radar_configure_dashboard(p_allowed_email text)
returns void language plpgsql security invoker set search_path = '' as $$
begin
    if p_allowed_email is null or length(btrim(p_allowed_email)) < 3
        or p_allowed_email not like '%@%' then
        raise exception 'Invalid dashboard owner email';
    end if;
    insert into public.dashboard_settings(singleton, allowed_email)
    values (true, lower(btrim(p_allowed_email)))
    on conflict(singleton) do update set allowed_email = excluded.allowed_email;
end;
$$;
revoke all on function public.radar_configure_dashboard(text) from public, anon, authenticated;
grant execute on function public.radar_configure_dashboard(text) to service_role;

create function public.radar_dashboard_is_owner()
returns boolean language sql stable security definer set search_path = '' as $$
    select exists (
        select 1 from public.dashboard_settings s
        join auth.users u on lower(u.email) = s.allowed_email
        where u.id = auth.uid() and u.email_confirmed_at is not null
            and lower(coalesce(auth.jwt()->>'email', '')) = s.allowed_email
    );
$$;
revoke all on function public.radar_dashboard_is_owner() from public, anon;
grant execute on function public.radar_dashboard_is_owner() to authenticated;

grant select on public.jobs, public.source_runs to authenticated;
grant update(status, notes) on public.jobs to authenticated;
create policy radar_owner_read_jobs on public.jobs for select to authenticated
    using (public.radar_dashboard_is_owner());
create policy radar_owner_update_jobs on public.jobs for update to authenticated
    using (public.radar_dashboard_is_owner()) with check (public.radar_dashboard_is_owner());
create policy radar_owner_read_runs on public.source_runs for select to authenticated
    using (public.radar_dashboard_is_owner());

alter table public.jobs add column status_changed_at timestamptz;
create table public.status_history (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    status text not null check (status in ('new','applied','interview','rejected','silent','offer','skipped')),
    changed_at timestamptz not null
);
create index status_history_job_time_idx on public.status_history(job_id, changed_at desc);
alter table public.status_history enable row level security;
revoke all on public.status_history from public, anon, authenticated;
grant select on public.status_history to authenticated;
grant select, insert, update, delete on public.status_history to service_role;
create policy radar_owner_read_history on public.status_history for select to authenticated
    using (public.radar_dashboard_is_owner());

create function public.radar_status_timestamp()
returns trigger language plpgsql set search_path = '' as $$
begin
    if TG_OP = 'INSERT' then
        NEW.status_changed_at := coalesce(NEW.status_changed_at, NEW.first_seen_at);
    elsif NEW.status is distinct from OLD.status then
        NEW.status_changed_at := clock_timestamp();
    end if;
    return NEW;
end;
$$;
create function public.radar_record_status()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
    if TG_OP = 'INSERT' or NEW.status is distinct from OLD.status then
        insert into public.status_history(job_id,status,changed_at)
        values(NEW.id,NEW.status,NEW.status_changed_at);
    end if;
    return NEW;
end;
$$;
revoke all on function public.radar_status_timestamp() from public, anon, authenticated;
revoke all on function public.radar_record_status() from public, anon, authenticated;
create trigger radar_job_status_timestamp before insert or update of status on public.jobs
    for each row execute function public.radar_status_timestamp();
create trigger radar_job_status_history after insert or update of status on public.jobs
    for each row execute function public.radar_record_status();
-- Historic transition times are unknown; do not invent Applied dates or histories.

-- Stage 6 will record membership only after an actual successful notification.
-- Suppressed/backfill notified_at timestamps are NOT proof that email was sent.
create table public.notification_batches (
    id uuid primary key default gen_random_uuid(),
    sent_at timestamptz not null
);
create table public.notification_jobs (
    notification_id uuid not null references public.notification_batches(id) on delete cascade,
    job_id uuid not null references public.jobs(id) on delete cascade,
    primary key(notification_id,job_id)
);
create index notification_batches_latest_idx on public.notification_batches(sent_at desc);
alter table public.notification_batches enable row level security;
alter table public.notification_jobs enable row level security;
revoke all on public.notification_batches,public.notification_jobs from public,anon,authenticated;
grant select on public.notification_batches,public.notification_jobs to authenticated;
grant select,insert,update,delete on public.notification_batches,public.notification_jobs to service_role;
create policy radar_owner_read_notifications on public.notification_batches for select to authenticated
    using (public.radar_dashboard_is_owner());
create policy radar_owner_read_notification_jobs on public.notification_jobs for select to authenticated
    using (public.radar_dashboard_is_owner());

-- Stage 4's restrictive Storage policy remains: authenticated users still cannot
-- read resume objects directly. The authorized server signs downloads for 60s.
commit;
