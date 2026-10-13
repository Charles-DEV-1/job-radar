begin;

alter table public.jobs
    add column resume_filename text,
    add column resume_error text,
    add column match_notes jsonb,
    add column resume_claim_id uuid,
    add column resume_claim_until timestamptz;

create table public.resume_runs (
    id uuid primary key,
    job_id uuid not null references public.jobs(id),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    status text not null default 'processing' check (status in ('processing', 'ready', 'failed')),
    error_message text,
    metadata jsonb not null default '{}'::jsonb
);
create index resume_runs_job_started_idx on public.resume_runs(job_id, started_at desc);
alter table public.resume_runs enable row level security;
revoke all on public.resume_runs from public, anon, authenticated;
grant select, insert, update on public.resume_runs to service_role;

-- A dedicated private bucket with no direct client access. Future dashboard
-- downloads use short-lived URLs issued by the authorized server only.
insert into storage.buckets(id, name, public, file_size_limit, allowed_mime_types)
values ('radar-resumes', 'radar-resumes', false, 8388608,
    array['application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/pdf'])
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;
create policy radar_resumes_block_client_access on storage.objects
    as restrictive for all to anon, authenticated
    using (bucket_id <> 'radar-resumes') with check (bucket_id <> 'radar-resumes');

create function public.radar_claim_tailor(p_claim_id uuid, p_include_maybe boolean default false)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_job public.jobs%rowtype;
begin
    if exists (select 1 from public.resume_runs where id = p_claim_id) then
        -- A transport retry can replay a claim after its transaction committed.
        select * into v_job from public.jobs
        where resume_claim_id = p_claim_id and resume_claim_until > now()
            and resume_status = 'pending' and closed_at is null
            and (filter_result = 'include' or (p_include_maybe and filter_result = 'maybe'));
        if not found then return null; end if;
        return jsonb_build_object('id', v_job.id, 'title', v_job.title,
            'company', v_job.company, 'description_text', v_job.description_text,
            'content_hash', v_job.content_hash);
    end if;
    select * into v_job from public.jobs
    where resume_status = 'pending' and closed_at is null
        and (filter_result = 'include' or (p_include_maybe and filter_result = 'maybe'))
        and (resume_claim_until is null or resume_claim_until < now())
    order by first_seen_at, id for update skip locked limit 1;
    if not found then return null; end if;
    if v_job.resume_claim_id is not null then
        update public.resume_runs set status = 'failed', completed_at = now(),
            error_message = 'Tailoring claim expired before completion'
        where id = v_job.resume_claim_id and completed_at is null;
    end if;
    update public.jobs set resume_claim_id = p_claim_id,
        resume_claim_until = now() + interval '15 minutes' where id = v_job.id;
    insert into public.resume_runs(id, job_id, metadata)
    values(p_claim_id, v_job.id, jsonb_build_object('include_maybe', p_include_maybe));
    return jsonb_build_object('id', v_job.id, 'title', v_job.title, 'company', v_job.company,
        'description_text', v_job.description_text, 'content_hash', v_job.content_hash);
end;
$$;

create function public.radar_finish_tailor(
    p_job_id uuid, p_claim_id uuid, p_content_hash text, p_status text,
    p_resume_path text, p_resume_filename text, p_match_notes jsonb,
    p_error text, p_metadata jsonb
) returns boolean language plpgsql security invoker set search_path = '' as $$
declare v_job public.jobs%rowtype; v_run public.resume_runs%rowtype;
begin
    select * into strict v_run from public.resume_runs where id = p_claim_id for update;
    if v_run.job_id <> p_job_id then raise exception 'Claim/job mismatch'; end if;
    if v_run.completed_at is not null then
        return coalesce((v_run.metadata->>'accepted')::boolean, false);
    end if;
    if p_status is null or p_status not in ('ready', 'failed')
        or p_metadata is null or jsonb_typeof(p_metadata) <> 'object'
        or (p_status = 'ready' and (p_resume_path is null or p_error is not null))
        or (p_status = 'failed' and nullif(p_error, '') is null)
        or (p_match_notes is not null and jsonb_typeof(p_match_notes) <> 'object') then
        raise exception 'Invalid tailoring result';
    end if;
    if p_resume_path is not null and (
        p_resume_path not like p_job_id::text || '/' || p_claim_id::text || '/%'
        or p_resume_filename is null or p_resume_filename !~ '^[A-Za-z0-9_.-]+\.docx$'
        or p_resume_path <> p_job_id::text || '/' || p_claim_id::text || '/' || p_resume_filename
    ) then raise exception 'Invalid private resume path'; end if;
    select * into strict v_job from public.jobs where id = p_job_id for update;
    if v_job.resume_claim_id is distinct from p_claim_id
        or v_job.resume_claim_until is null or v_job.resume_claim_until < now()
        or v_job.closed_at is not null
        or v_job.filter_result = 'exclude' or v_job.content_hash <> p_content_hash
        or (v_job.filter_result = 'maybe'
            and not coalesce((v_run.metadata->>'include_maybe')::boolean, false))
        or v_job.resume_status <> 'pending' then
        update public.resume_runs set status = 'failed', completed_at = now(),
            error_message = 'Job changed or tailoring claim is stale',
            metadata = v_run.metadata || p_metadata || jsonb_build_object('accepted', false)
        where id = p_claim_id;
        update public.jobs set resume_claim_id = null, resume_claim_until = null
        where id = p_job_id and resume_claim_id = p_claim_id;
        return false;
    end if;
    update public.jobs set resume_status = p_status, resume_path = p_resume_path,
        resume_filename = p_resume_filename, match_notes = p_match_notes,
        resume_error = p_error, resume_claim_id = null, resume_claim_until = null
    where id = p_job_id;
    update public.resume_runs set status = p_status, completed_at = now(),
        error_message = p_error,
        metadata = v_run.metadata || p_metadata || jsonb_build_object('accepted', true)
    where id = p_claim_id;
    return true;
end;
$$;

revoke all on function public.radar_claim_tailor(uuid,boolean) from public, anon, authenticated;
revoke all on function public.radar_finish_tailor(uuid,uuid,text,text,text,text,jsonb,text,jsonb)
    from public, anon, authenticated;
grant execute on function public.radar_claim_tailor(uuid,boolean) to service_role;
grant execute on function public.radar_finish_tailor(uuid,uuid,text,text,text,text,jsonb,text,jsonb)
    to service_role;

commit;
