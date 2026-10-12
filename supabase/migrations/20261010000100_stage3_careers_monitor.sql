begin;

-- Forward-only extension; Stage 2's applied migration remains unchanged.
alter table public.jobs add column source_type text not null default 'ats'
    check (source_type in ('ats', 'careers_page', 'feed'));
alter table public.sources add column company_domain text not null default '';
alter table public.source_runs add column metadata jsonb not null default '{}'::jsonb;
create unique index sources_monitor_identity_idx on public.sources(source_type, slug)
    where ats is null;
create table public.source_monitor_state (
    source_id uuid primary key references public.sources(id) on delete cascade,
    state jsonb not null,
    updated_at timestamptz not null
);
alter table public.source_monitor_state enable row level security;
revoke all on public.source_monitor_state from public, anon, authenticated;
grant select, insert, update, delete on public.source_monitor_state to service_role;

create or replace function public.radar_sync_sources(p_sources jsonb) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare v_entry jsonb; v_ids jsonb := '[]'::jsonb; v_id uuid; v_type text; v_ats text;
begin
    if jsonb_typeof(p_sources) <> 'array' then raise exception 'Expected sources array'; end if;
    perform pg_advisory_xact_lock(19087621);
    update public.sources set enabled = false;
    for v_entry in select value from jsonb_array_elements(p_sources) loop
        v_type := coalesce(v_entry->>'source_type', 'ats');
        v_ats := case when v_type = 'ats' then v_entry->>'ats' else null end;
        if v_type not in ('ats', 'careers_page', 'feed')
            or nullif(v_entry->>'slug', '') is null then raise exception 'Invalid source identity'; end if;
        if v_type = 'ats' then
            insert into public.sources (company_name, source_type, ats, slug, url, enabled, company_domain)
            values (v_entry->>'company_name', v_type, v_ats, v_entry->>'slug',
                v_entry->>'url', (v_entry->>'enabled')::boolean, coalesce(v_entry->>'company_domain',''))
            on conflict (ats, slug) do update set company_name = excluded.company_name,
                url = excluded.url, enabled = excluded.enabled, company_domain = excluded.company_domain
            returning id into v_id;
        else
            if v_entry->>'ats' is distinct from v_type then raise exception 'Invalid monitor identity'; end if;
            insert into public.sources (company_name, source_type, ats, slug, url, enabled, company_domain)
            values (v_entry->>'company_name', v_type, null, v_entry->>'slug',
                v_entry->>'url', (v_entry->>'enabled')::boolean, coalesce(v_entry->>'company_domain',''))
            on conflict (source_type, slug) where ats is null do update
                set company_name = excluded.company_name, url = excluded.url,
                    enabled = excluded.enabled, company_domain = excluded.company_domain
            returning id into v_id;
        end if;
        v_ids := v_ids || jsonb_build_array(jsonb_build_object('id', v_id,
            'ats', v_entry->>'ats', 'slug', v_entry->>'slug'));
    end loop;
    return v_ids;
end;
$$;

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
                description_text = v_entry->>'description_text', description_html = v_entry->>'description_html',
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

create function public.radar_monitor_state(p_source_id uuid) returns jsonb
language sql security invoker set search_path = '' as $$
    select coalesce((select state from public.source_monitor_state where source_id = p_source_id), '{}'::jsonb);
$$;

create function public.radar_commit_monitor(
    p_run_id uuid, p_jobs jsonb, p_observed_at timestamptz, p_duration_ms bigint,
    p_state jsonb, p_expected_hash text, p_metadata jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_run public.source_runs%rowtype; v_hash text; v_result jsonb; v_type text;
begin
    perform pg_advisory_xact_lock(19087621);
    select * into strict v_run from public.source_runs where id = p_run_id for update;
    if v_run.completed_at is not null then
        if v_run.status = 'ok' then return v_run.result; end if;
        raise exception 'Run was already completed with an error';
    end if;
    select source_type into strict v_type from public.sources where id = v_run.source_id;
    if v_type not in ('careers_page', 'feed') then raise exception 'Expected monitor source'; end if;
    if jsonb_typeof(p_state) <> 'object' then raise exception 'Invalid monitor state'; end if;
    if v_type = 'careers_page' and (
        coalesce(p_state->>'hash','') !~ '^[a-f0-9]{64}$'
        or jsonb_typeof(p_state->'lines') is distinct from 'array'
        or jsonb_typeof(p_state->'links') is distinct from 'array'
        or coalesce(p_state->>'method','') not in ('http','playwright')
    ) then raise exception 'Invalid careers snapshot'; end if;
    select state->>'hash' into v_hash from public.source_monitor_state where source_id = v_run.source_id;
    if v_hash is distinct from p_expected_hash then raise exception 'Monitor baseline changed; retry next run'; end if;
    -- Both job ingestion and the new baseline roll back if either operation fails.
    v_result := public.radar_commit_snapshot(p_run_id, p_jobs, p_observed_at, p_duration_ms);
    insert into public.source_monitor_state(source_id, state, updated_at)
    values(v_run.source_id, jsonb_set(p_state, '{jobs}', p_jobs), p_observed_at)
    on conflict(source_id) do update set state = excluded.state, updated_at = excluded.updated_at;
    update public.source_runs set metadata = p_metadata where id = p_run_id;
    return v_result;
end;
$$;

create function public.radar_run_metadata(p_run_id uuid, p_metadata jsonb) returns void
language sql security invoker set search_path = '' as $$
    update public.source_runs set metadata = p_metadata where id = p_run_id;
$$;

revoke all on function public.radar_monitor_state(uuid) from public, anon, authenticated;
revoke all on function public.radar_commit_monitor(uuid,jsonb,timestamptz,bigint,jsonb,text,jsonb) from public, anon, authenticated;
revoke all on function public.radar_run_metadata(uuid,jsonb) from public, anon, authenticated;
grant execute on function public.radar_monitor_state(uuid) to service_role;
grant execute on function public.radar_commit_monitor(uuid,jsonb,timestamptz,bigint,jsonb,text,jsonb) to service_role;
grant execute on function public.radar_run_metadata(uuid,jsonb) to service_role;
commit;
