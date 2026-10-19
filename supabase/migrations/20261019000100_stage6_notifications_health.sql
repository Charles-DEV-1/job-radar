begin;

-- Pending attempts persist the exact payload and Resend key so interrupted
-- sends can safely retry without selecting a different set of jobs.
create table public.notification_attempts (
    id uuid primary key default gen_random_uuid(),
    idempotency_key text not null unique,
    job_ids uuid[] not null,
    jobs jsonb not null,
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    notification_id uuid references public.notification_batches(id)
);
create unique index notification_one_pending_attempt_idx
    on public.notification_attempts ((true)) where sent_at is null;
alter table public.notification_attempts enable row level security;
revoke all on public.notification_attempts from public, anon, authenticated;
grant select, insert, update on public.notification_attempts to service_role;

create function public.radar_claim_notification(p_resume_grace_minutes integer default 30)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_attempt public.notification_attempts%rowtype; v_jobs jsonb; v_ids uuid[];
begin
    if p_resume_grace_minutes is null
        or p_resume_grace_minutes < 0 or p_resume_grace_minutes > 1440 then
        raise exception 'Invalid resume grace period';
    end if;
    perform pg_advisory_xact_lock(19087622);
    select * into v_attempt from public.notification_attempts
        where sent_at is null for update;
    if found then
        return jsonb_build_object('id', v_attempt.id,
            'idempotency_key', v_attempt.idempotency_key, 'jobs', v_attempt.jobs);
    end if;
    with eligible as (
        select j.id, j.company, j.title, j.location_raw, j.filter_result
        from public.jobs j
        where j.notified_at is null and not j.backfill
            and j.filter_result in ('include','maybe') and j.closed_at is null
            and (j.resume_status in ('ready','failed')
                or j.first_seen_at <= now() - make_interval(mins => p_resume_grace_minutes))
        order by j.first_seen_at desc, j.id
        for update of j skip locked
    )
    select array_agg(id), jsonb_agg(jsonb_build_object(
        'id', id, 'company', company, 'title', title,
        'location', location_raw, 'filter_result', filter_result
    )) into v_ids, v_jobs from eligible;
    if coalesce(cardinality(v_ids), 0) = 0 then return null; end if;
    insert into public.notification_attempts(idempotency_key, job_ids, jobs)
        values ('job-notification/' || gen_random_uuid()::text, v_ids, v_jobs)
        returning * into v_attempt;
    return jsonb_build_object('id', v_attempt.id,
        'idempotency_key', v_attempt.idempotency_key, 'jobs', v_attempt.jobs);
end;
$$;

create function public.radar_finish_notification(p_attempt_id uuid)
returns uuid language plpgsql security invoker set search_path = '' as $$
declare v_attempt public.notification_attempts%rowtype; v_batch uuid;
begin
    select * into strict v_attempt from public.notification_attempts
        where id = p_attempt_id for update;
    if v_attempt.sent_at is not null then
        return v_attempt.notification_id;
    end if;
    insert into public.notification_batches(sent_at) values(clock_timestamp())
        returning id into v_batch;
    insert into public.notification_jobs(notification_id, job_id)
        select v_batch, unnest(v_attempt.job_ids)
        on conflict do nothing;
    update public.jobs set notified_at = clock_timestamp()
        where id = any(v_attempt.job_ids) and notified_at is null;
    update public.notification_attempts set sent_at = clock_timestamp(),
        notification_id = v_batch
        where id = p_attempt_id;
    return v_batch;
end;
$$;
revoke all on function public.radar_claim_notification(integer) from public, anon, authenticated;
revoke all on function public.radar_finish_notification(uuid) from public, anon, authenticated;
grant execute on function public.radar_claim_notification(integer) to service_role;
grant execute on function public.radar_finish_notification(uuid) to service_role;

create table public.health_events (
    issue_key text primary key,
    issue_type text not null,
    source_id uuid references public.sources(id) on delete cascade,
    details jsonb not null default '{}'::jsonb,
    opened_at timestamptz not null default now(),
    last_alerted_at timestamptz,
    resolved_at timestamptz,
    recovered_notice_sent_at timestamptz
);
create index health_events_active_idx on public.health_events(opened_at desc)
    where resolved_at is null;
alter table public.health_events enable row level security;
revoke all on public.health_events from public, anon, authenticated;
grant select, insert, update on public.health_events to service_role;

create function public.radar_mark_health_alerted(
    p_active_keys text[], p_recovered_keys text[], p_alerted_at timestamptz
) returns void language plpgsql security invoker set search_path = '' as $$
begin
    update public.health_events set last_alerted_at = p_alerted_at
        where issue_key = any(coalesce(p_active_keys, '{}'::text[]));
    update public.health_events set recovered_notice_sent_at = p_alerted_at
        where issue_key = any(coalesce(p_recovered_keys, '{}'::text[]));
end;
$$;
revoke all on function public.radar_mark_health_alerted(text[],text[],timestamptz)
    from public, anon, authenticated;
grant execute on function public.radar_mark_health_alerted(text[],text[],timestamptz)
    to service_role;

commit;
