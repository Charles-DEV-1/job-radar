begin;

-- Keep check-fit jobs in the notification dashboard deep link without ever
-- including them in email content or setting their notified_at timestamp.
alter table public.notification_attempts
    add column check_fit_job_ids uuid[] not null default '{}'::uuid[],
    add column check_fit_count integer not null default 0
        check (check_fit_count >= 0);

-- If the prior version left an unsent claim, split its persisted mixed payload
-- so retrying that attempt obeys the include-only rule as well.
with pending as (
    select a.id,
        coalesce(array(
            select (item->>'id')::uuid
            from jsonb_array_elements(a.jobs) item
            where item->>'filter_result' = 'include'
        ), '{}'::uuid[]) as include_ids,
        coalesce(array(
            select (item->>'id')::uuid
            from jsonb_array_elements(a.jobs) item
            where item->>'filter_result' = 'maybe'
        ), '{}'::uuid[]) as maybe_ids,
        coalesce((
            select jsonb_agg(item)
            from jsonb_array_elements(a.jobs) item
            where item->>'filter_result' = 'include'
        ), '[]'::jsonb) as include_jobs
    from public.notification_attempts a
    where a.sent_at is null
)
update public.notification_attempts a
set job_ids = p.include_ids,
    jobs = p.include_jobs,
    check_fit_job_ids = p.maybe_ids,
    check_fit_count = cardinality(p.maybe_ids)
from pending p
where a.id = p.id;
delete from public.notification_attempts
    where sent_at is null and cardinality(job_ids) = 0;

create or replace function public.radar_claim_notification(
    p_resume_grace_minutes integer default 30
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    v_attempt public.notification_attempts%rowtype;
    v_jobs jsonb;
    v_ids uuid[];
    v_check_fit_ids uuid[];
    v_check_fit_count integer;
begin
    if p_resume_grace_minutes is null
        or p_resume_grace_minutes < 0 or p_resume_grace_minutes > 1440 then
        raise exception 'Invalid resume grace period';
    end if;
    perform pg_advisory_xact_lock(19087622);
    select * into v_attempt from public.notification_attempts
        where sent_at is null for update;
    if found then
        return jsonb_build_object(
            'id', v_attempt.id,
            'idempotency_key', v_attempt.idempotency_key,
            'jobs', v_attempt.jobs,
            'check_fit_count', v_attempt.check_fit_count
        );
    end if;

    with eligible as (
        select j.id, j.company, j.title, j.location_raw, j.filter_result
        from public.jobs j
        where j.notified_at is null and not j.backfill
            and j.filter_result = 'include' and j.closed_at is null
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

    select coalesce(array_agg(j.id), '{}'::uuid[]), count(*)::integer
        into v_check_fit_ids, v_check_fit_count
        from public.jobs j
        where j.notified_at is null and not j.backfill
            and j.filter_result = 'maybe' and j.closed_at is null;

    insert into public.notification_attempts(
        idempotency_key, job_ids, jobs, check_fit_job_ids, check_fit_count
    ) values (
        'job-notification/' || gen_random_uuid()::text,
        v_ids, v_jobs, v_check_fit_ids, v_check_fit_count
    ) returning * into v_attempt;
    return jsonb_build_object(
        'id', v_attempt.id,
        'idempotency_key', v_attempt.idempotency_key,
        'jobs', v_attempt.jobs,
        'check_fit_count', v_attempt.check_fit_count
    );
end;
$$;

create or replace function public.radar_finish_notification(p_attempt_id uuid)
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
        select v_batch, jobs.job_id
        from (
            select unnest(v_attempt.job_ids) as job_id
            union
            select unnest(v_attempt.check_fit_job_ids) as job_id
        ) as jobs
        on conflict do nothing;
    update public.jobs set notified_at = clock_timestamp()
        where id = any(v_attempt.job_ids)
            and filter_result = 'include' and notified_at is null;
    update public.notification_attempts set sent_at = clock_timestamp(),
        notification_id = v_batch
        where id = p_attempt_id;
    return v_batch;
end;
$$;

revoke all on function public.radar_claim_notification(integer)
    from public, anon, authenticated;
revoke all on function public.radar_finish_notification(uuid)
    from public, anon, authenticated;
grant execute on function public.radar_claim_notification(integer) to service_role;
grant execute on function public.radar_finish_notification(uuid) to service_role;

commit;
