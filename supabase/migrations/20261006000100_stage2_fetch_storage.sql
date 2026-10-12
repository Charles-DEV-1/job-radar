begin;

create table public.sources (
    id uuid primary key default gen_random_uuid(),
    company_name text not null,
    source_type text not null check (source_type in ('ats', 'careers_page', 'feed')),
    ats text check (ats in ('greenhouse', 'lever', 'ashby', 'workable', 'recruitee', 'smartrecruiters')),
    slug text,
    url text not null,
    enabled boolean not null default true,
    last_run_at timestamptz,
    last_success_at timestamptz,
    last_job_count integer check (last_job_count >= 0),
    consecutive_failures integer not null default 0 check (consecutive_failures >= 0),
    last_error text,
    unique (ats, slug),
    check (source_type <> 'ats' or (ats is not null and nullif(slug, '') is not null))
);

create table public.source_runs (
    id uuid primary key,
    source_id uuid not null references public.sources(id),
    started_at timestamptz not null,
    duration_ms bigint not null default 0 check (duration_ms >= 0),
    status text not null default 'error' check (status in ('ok', 'error')),
    jobs_found integer not null default 0 check (jobs_found >= 0),
    new_jobs integer not null default 0 check (new_jobs >= 0),
    error_message text,
    completed_at timestamptz,
    result jsonb
);
create index source_runs_source_started_idx on public.source_runs(source_id, started_at desc);

create table public.jobs (
    id uuid primary key default gen_random_uuid(),
    source_id uuid not null references public.sources(id),
    external_id text not null check (external_id <> ''),
    source text not null,
    source_company_slug text not null,
    company text not null,
    title text not null check (title <> ''),
    location_raw text not null default '',
    location_tags text[] not null default '{}',
    remote_flag boolean not null default false,
    department text,
    employment_type text,
    description_text text not null default '',
    description_html text not null default '',
    apply_url text not null,
    posted_at timestamptz,
    first_seen_at timestamptz not null,
    last_seen_at timestamptz not null,
    closed_at timestamptz,
    content_hash text not null check (length(content_hash) = 64),
    canonical_key text not null check (length(canonical_key) = 64),
    filter_result text not null check (filter_result in ('include', 'maybe', 'exclude')),
    filter_reason text not null check (filter_reason <> ''),
    status text not null default 'new' check (status in ('new', 'applied', 'interview', 'rejected', 'silent', 'offer', 'skipped')),
    notified_at timestamptz,
    resume_path text,
    resume_status text not null default 'pending' check (resume_status in ('pending', 'ready', 'failed')),
    notes text,
    backfill boolean not null default false,
    unique (source_id, external_id)
);
create index jobs_canonical_key_idx on public.jobs(canonical_key);
create index jobs_status_idx on public.jobs(status);

-- A cross-source duplicate keeps its external identity and alternate apply URL here.
-- Closing one listing must not close the job while another listing remains active.
create table public.job_sources (
    source_id uuid not null references public.sources(id),
    external_id text not null,
    job_id uuid not null references public.jobs(id),
    apply_url text not null,
    content_hash text not null,
    first_seen_at timestamptz not null,
    last_seen_at timestamptz not null,
    closed_at timestamptz,
    primary key (source_id, external_id)
);
create index job_sources_job_idx on public.job_sources(job_id);

alter table public.sources enable row level security;
alter table public.source_runs enable row level security;
alter table public.jobs enable row level security;
alter table public.job_sources enable row level security;
revoke all on public.sources, public.source_runs, public.jobs, public.job_sources from public, anon, authenticated;
grant select, insert, update, delete on public.sources, public.source_runs, public.jobs, public.job_sources to service_role;

-- All RPCs are invoker functions: only the service role has table access.
create function public.radar_sync_sources(p_sources jsonb) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare v_entry jsonb; v_ids jsonb := '[]'::jsonb; v_id uuid;
begin
    if jsonb_typeof(p_sources) <> 'array' then raise exception 'Expected sources array'; end if;
    perform pg_advisory_xact_lock(19087621);
    update public.sources set enabled = false where source_type = 'ats';
    for v_entry in select value from jsonb_array_elements(p_sources) loop
        insert into public.sources (company_name, source_type, ats, slug, url, enabled)
        values (v_entry->>'company_name', 'ats', v_entry->>'ats', v_entry->>'slug',
                v_entry->>'url', (v_entry->>'enabled')::boolean)
        on conflict (ats, slug) do update set company_name = excluded.company_name,
            url = excluded.url, enabled = excluded.enabled
        returning id into v_id;
        v_ids := v_ids || jsonb_build_array(jsonb_build_object('id', v_id,
            'ats', v_entry->>'ats', 'slug', v_entry->>'slug'));
    end loop;
    return v_ids;
end;
$$;

create function public.radar_start_run(p_source_id uuid, p_run_id uuid, p_started_at timestamptz)
returns void language plpgsql security invoker set search_path = '' as $$
begin
    insert into public.source_runs (id, source_id, started_at, error_message)
    values (p_run_id, p_source_id, p_started_at, 'Run did not complete')
    on conflict (id) do nothing;
    update public.sources set last_run_at = greatest(last_run_at, p_started_at)
    where id = p_source_id;
end;
$$;

create function public.radar_commit_snapshot(
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
        if v_entry->>'source' is distinct from v_source.ats
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
                    source_id, external_id, source, source_company_slug, company, title,
                    location_raw, location_tags, remote_flag, department, employment_type,
                    description_text, description_html, apply_url, posted_at,
                    first_seen_at, last_seen_at, content_hash, canonical_key,
                    filter_result, filter_reason, notified_at, backfill
                ) values (
                    v_source.id, v_entry->>'external_id', v_source.ats, v_source.slug,
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

create function public.radar_fail_run(
    p_run_id uuid, p_error text, p_duration_ms bigint, p_completed_at timestamptz,
    p_jobs_found integer default 0
) returns void language plpgsql security invoker set search_path = '' as $$
declare v_run public.source_runs%rowtype;
begin
    perform pg_advisory_xact_lock(19087621);
    select * into strict v_run from public.source_runs where id = p_run_id for update;
    if v_run.completed_at is not null then return; end if;
    update public.source_runs set status = 'error', error_message = p_error,
        duration_ms = p_duration_ms, jobs_found = p_jobs_found,
        completed_at = p_completed_at where id = p_run_id;
    update public.sources set consecutive_failures = consecutive_failures + 1,
        last_error = p_error where id = v_run.source_id
        and (last_success_at is null or last_success_at <= v_run.started_at);
end;
$$;

revoke all on function public.radar_sync_sources(jsonb) from public, anon, authenticated;
revoke all on function public.radar_start_run(uuid, uuid, timestamptz) from public, anon, authenticated;
revoke all on function public.radar_commit_snapshot(uuid, jsonb, timestamptz, bigint) from public, anon, authenticated;
revoke all on function public.radar_fail_run(uuid, text, bigint, timestamptz, integer) from public, anon, authenticated;
grant execute on function public.radar_sync_sources(jsonb) to service_role;
grant execute on function public.radar_start_run(uuid, uuid, timestamptz) to service_role;
grant execute on function public.radar_commit_snapshot(uuid, jsonb, timestamptz, bigint) to service_role;
grant execute on function public.radar_fail_run(uuid, text, bigint, timestamptz, integer) to service_role;

commit;
