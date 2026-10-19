begin;

-- Archived jobs no longer have a downloadable tailored resume. Descriptions are
-- intentionally nullable once their retention window expires.
alter table public.jobs drop constraint jobs_resume_status_check;
alter table public.jobs add constraint jobs_resume_status_check
    check (resume_status in ('pending','ready','failed','archived'));
alter table public.jobs alter column description_text drop not null;
alter table public.jobs alter column description_html drop not null;
grant delete on public.resume_runs to service_role;

-- Tombstones retain only source identity/hash metadata, never descriptions.
create table public.job_cleanup_tombstones (
    source_id uuid not null references public.sources(id),
    external_id text not null,
    canonical_key text not null check (length(canonical_key) = 64),
    deleted_at timestamptz not null default now(),
    resume_path text,
    primary key (source_id, external_id)
);
alter table public.job_cleanup_tombstones enable row level security;
revoke all on public.job_cleanup_tombstones from public, anon, authenticated;
grant select, insert, update, delete on public.job_cleanup_tombstones to service_role;

-- Remember description retention without changing the job's other fields. The
-- ingest RPC checks this marker so the next source poll cannot restore old text.
create table public.job_description_archives (
    job_id uuid primary key references public.jobs(id) on delete cascade,
    archived_at timestamptz not null default now()
);
alter table public.job_description_archives enable row level security;
revoke all on public.job_description_archives from public, anon, authenticated;
grant select, insert, update, delete on public.job_description_archives to service_role;

create function public.radar_cleanup_archive_job(
    p_job_id uuid, p_expected_status text, p_changed_before timestamptz
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare v_job public.jobs%rowtype;
begin
    perform pg_advisory_xact_lock(19087621);
    select * into v_job from public.jobs where id = p_job_id for update;
    if not found or v_job.status is distinct from p_expected_status
        or v_job.status not in ('applied','rejected','silent')
        or v_job.status_changed_at is null
        or v_job.status_changed_at >= p_changed_before
        or v_job.resume_status = 'archived' then
        return false;
    end if;
    update public.jobs set description_text = null, description_html = null,
        resume_status = 'archived' where id = v_job.id;
    insert into public.job_description_archives(job_id)
        values(v_job.id) on conflict(job_id) do nothing;
    return true;
end;
$$;

create function public.radar_cleanup_clear_open_skipped(
    p_job_id uuid, p_changed_before timestamptz
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare v_job public.jobs%rowtype;
begin
    perform pg_advisory_xact_lock(19087621);
    select * into v_job from public.jobs where id = p_job_id for update;
    if not found or v_job.status <> 'skipped' or v_job.closed_at is not null
        or v_job.status_changed_at is null
        or v_job.status_changed_at >= p_changed_before
        or (v_job.description_text is null and v_job.description_html is null) then
        return false;
    end if;
    update public.jobs set description_text = null, description_html = null
        where id = v_job.id;
    insert into public.job_description_archives(job_id)
        values(v_job.id) on conflict(job_id) do nothing;
    return true;
end;
$$;

create function public.radar_cleanup_delete_closed_job(
    p_job_id uuid, p_closed_before timestamptz
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_job public.jobs%rowtype;
begin
    if p_closed_before is null then raise exception 'Invalid cleanup cutoff'; end if;
    perform pg_advisory_xact_lock(19087621);
    select * into v_job from public.jobs where id = p_job_id for update;
    if not found or v_job.closed_at is null or v_job.closed_at >= p_closed_before
        or v_job.status not in ('new','skipped') then
        return jsonb_build_object('deleted', false);
    end if;

    insert into public.job_cleanup_tombstones(
        source_id, external_id, canonical_key, deleted_at
    )
    select js.source_id, js.external_id, v_job.canonical_key, clock_timestamp()
    from public.job_sources js where js.job_id = v_job.id
    on conflict (source_id, external_id) do update set
        canonical_key = excluded.canonical_key;
    insert into public.job_cleanup_tombstones(
        source_id, external_id, canonical_key, deleted_at, resume_path
    ) values (
        v_job.source_id, v_job.external_id, v_job.canonical_key,
        clock_timestamp(), v_job.resume_path
    ) on conflict (source_id, external_id) do update set
        canonical_key = excluded.canonical_key,
        resume_path = coalesce(public.job_cleanup_tombstones.resume_path, excluded.resume_path);

    delete from public.resume_runs where job_id = v_job.id;
    delete from public.job_sources where job_id = v_job.id;
    delete from public.jobs where id = v_job.id;
    return jsonb_build_object('deleted', true, 'resume_path', v_job.resume_path);
end;
$$;

create function public.radar_cleanup_finish_deleted_resume(
    p_source_id uuid, p_external_id text, p_resume_path text
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare v_count integer;
begin
    update public.job_cleanup_tombstones set resume_path = null
    where source_id = p_source_id and external_id = p_external_id
        and resume_path = p_resume_path;
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

revoke all on function public.radar_cleanup_delete_closed_job(uuid,timestamptz)
    from public, anon, authenticated;
revoke all on function public.radar_cleanup_finish_deleted_resume(uuid,text,text)
    from public, anon, authenticated;
revoke all on function public.radar_cleanup_archive_job(uuid,text,timestamptz)
    from public, anon, authenticated;
revoke all on function public.radar_cleanup_clear_open_skipped(uuid,timestamptz)
    from public, anon, authenticated;
grant execute on function public.radar_cleanup_delete_closed_job(uuid,timestamptz)
    to service_role;
grant execute on function public.radar_cleanup_finish_deleted_resume(uuid,text,text)
    to service_role;
grant execute on function public.radar_cleanup_archive_job(uuid,text,timestamptz)
    to service_role;
grant execute on function public.radar_cleanup_clear_open_skipped(uuid,timestamptz)
    to service_role;

create or replace function public.radar_commit_snapshot(
    p_run_id uuid, p_jobs jsonb, p_observed_at timestamptz, p_duration_ms bigint
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
    v_run public.source_runs%rowtype;
    v_source public.sources%rowtype;
    v_job public.jobs%rowtype;
    v_listing public.job_sources%rowtype;
    v_entry jsonb;
    v_id uuid;
    v_first boolean;
    v_is_existing boolean;
    v_inserted integer := 0;
    v_new integer := 0;
    v_updated integer := 0;
    v_duplicates integer := 0;
    v_closed integer := 0;
    v_result jsonb;
begin
    if jsonb_typeof(p_jobs) <> 'array' then raise exception 'Expected jobs array'; end if;
    -- Serialize snapshot writes, including canonical lookups across different sources.
    -- Fetches remain concurrent; no network request runs inside this transaction.
    perform pg_advisory_xact_lock(19087621);
    select * into strict v_run from public.source_runs where id = p_run_id for update;
    if v_run.completed_at is not null then
        if v_run.status = 'ok' then return v_run.result; end if;
        raise exception 'Run was already completed with an error';
    end if;
    select * into strict v_source from public.sources where id = v_run.source_id for update;
    if v_source.last_success_at > v_run.started_at then
        raise exception 'A newer snapshot has already committed';
    end if;
    v_first := v_source.last_success_at is null;
    for v_entry in select value from jsonb_array_elements(p_jobs) loop
        if v_entry->>'source' is distinct from coalesce(v_source.ats, v_source.source_type)
            or v_entry->>'source_company_slug' is distinct from v_source.slug then
            raise exception 'Job source identity mismatch';
        end if;
        -- Permanently suppress listings that were purged after their retention period.
        if exists (
            select 1 from public.job_cleanup_tombstones t
            where t.source_id = v_source.id
                and t.external_id = v_entry->>'external_id'
        ) then
            continue;
        end if;
        select * into v_listing from public.job_sources
        where source_id = v_source.id and external_id = v_entry->>'external_id';
        v_is_existing := found;
        v_id := v_listing.job_id;
        if not v_is_existing then
            select j.id into v_id from public.jobs j
            where j.canonical_key = v_entry->>'canonical_key'
                and exists (select 1 from public.job_sources js
                    where js.job_id = j.id and js.source_id <> v_source.id)
            order by j.first_seen_at, j.id limit 1;
            if found then
                v_duplicates := v_duplicates + 1;
            else
                insert into public.jobs (
                    source_id, external_id, source, source_company_slug, source_type, company, title,
                    location_raw, location_tags, remote_flag, department, employment_type,
                    description_text, description_html, apply_url, posted_at,
                    first_seen_at, last_seen_at, content_hash, canonical_key,
                    filter_result, filter_reason, notified_at, backfill
                ) values (
                    v_source.id, v_entry->>'external_id', coalesce(v_source.ats, v_source.source_type), v_source.slug, v_source.source_type,
                    v_entry->>'company', v_entry->>'title', v_entry->>'location_raw',
                    array(select jsonb_array_elements_text(v_entry->'location_tags')),
                    (v_entry->>'remote_flag')::boolean, v_entry->>'department',
                    v_entry->>'employment_type', v_entry->>'description_text',
                    v_entry->>'description_html', v_entry->>'apply_url',
                    (v_entry->>'posted_at')::timestamptz, p_observed_at, p_observed_at,
                    v_entry->>'content_hash', v_entry->>'canonical_key',
                    v_entry->>'filter_result', v_entry->>'filter_reason',
                    case when v_first or v_entry->>'filter_result' = 'exclude'
                        then p_observed_at else null end, v_first
                ) returning id into v_id;
                v_inserted := v_inserted + 1;
                if not v_first and v_entry->>'filter_result' <> 'exclude' then
                    v_new := v_new + 1;
                end if;
            end if;
        elsif v_listing.content_hash <> v_entry->>'content_hash' then
            v_updated := v_updated + 1;
        end if;
        select * into strict v_job from public.jobs where id = v_id;
        -- Keep the primary listing's content while it remains present. Alternate
        -- descriptions may differ in formatting and must not cause hash oscillation.
        if v_job.source_id = v_source.id or not exists (
            select 1 from public.job_sources js where js.job_id = v_id
                and js.source_id = v_job.source_id and js.external_id = v_job.external_id
                and js.closed_at is null
        ) then
            update public.jobs set company = v_entry->>'company', title = v_entry->>'title',
                location_raw = v_entry->>'location_raw',
                location_tags = array(select jsonb_array_elements_text(v_entry->'location_tags')),
                remote_flag = (v_entry->>'remote_flag')::boolean,
                department = v_entry->>'department', employment_type = v_entry->>'employment_type',
                description_text = case when v_job.resume_status = 'archived'
                    or exists (select 1 from public.job_description_archives da
                        where da.job_id = v_job.id)
                    then null else v_entry->>'description_text' end,
                description_html = case when v_job.resume_status = 'archived'
                    or exists (select 1 from public.job_description_archives da
                        where da.job_id = v_job.id)
                    then null else v_entry->>'description_html' end,
                apply_url = v_entry->>'apply_url', posted_at = (v_entry->>'posted_at')::timestamptz,
                content_hash = v_entry->>'content_hash', canonical_key = v_entry->>'canonical_key',
                filter_result = v_entry->>'filter_result', filter_reason = v_entry->>'filter_reason',
                notified_at = case when v_entry->>'filter_result' = 'exclude'
                    then coalesce(notified_at, p_observed_at) else notified_at end
            where id = v_id;
        end if;
        update public.jobs set last_seen_at = greatest(last_seen_at, p_observed_at), closed_at = null
        where id = v_id;
        insert into public.job_sources (source_id, external_id, job_id, apply_url,
            content_hash, first_seen_at, last_seen_at, closed_at)
        values (v_source.id, v_entry->>'external_id', v_id, v_entry->>'apply_url',
            v_entry->>'content_hash', p_observed_at, p_observed_at, null)
        on conflict (source_id, external_id) do update set
            apply_url = excluded.apply_url, content_hash = excluded.content_hash,
            last_seen_at = excluded.last_seen_at, closed_at = null;
    end loop;
    update public.job_sources js set closed_at = p_observed_at
    where js.source_id = v_source.id and js.closed_at is null
        and not exists (select 1 from jsonb_array_elements(p_jobs) e
            where e->>'external_id' = js.external_id);
    update public.jobs j set closed_at = p_observed_at
    where j.closed_at is null
        and exists (select 1 from public.job_sources js where js.job_id = j.id and js.source_id = v_source.id)
        and not exists (select 1 from public.job_sources js where js.job_id = j.id and js.closed_at is null);
    get diagnostics v_closed = row_count;
    -- If the primary URL disappeared, keep the dashboard link pointed at a live alternate.
    update public.jobs j set apply_url = (
        select js.apply_url from public.job_sources js where js.job_id = j.id and js.closed_at is null
        order by js.first_seen_at, js.source_id limit 1
    ) where j.closed_at is null
        and exists (select 1 from public.job_sources js where js.job_id = j.id and js.source_id = v_source.id)
        and not exists (select 1 from public.job_sources js where js.job_id = j.id
            and js.source_id = j.source_id and js.external_id = j.external_id and js.closed_at is null);
    v_result := jsonb_build_object('jobs_found', jsonb_array_length(p_jobs),
        'inserted_jobs', v_inserted, 'new_jobs', v_new, 'updated_jobs', v_updated,
        'duplicates', v_duplicates, 'closed_jobs', v_closed, 'backfill', v_first);
    update public.source_runs set status = 'ok', jobs_found = jsonb_array_length(p_jobs),
        new_jobs = v_new, duration_ms = p_duration_ms, error_message = null,
        completed_at = p_observed_at, result = v_result where id = p_run_id;
    update public.sources set last_success_at = p_observed_at,
        last_job_count = jsonb_array_length(p_jobs), consecutive_failures = 0, last_error = null
    where id = v_source.id;
    return v_result;
end;
$$;

commit;
