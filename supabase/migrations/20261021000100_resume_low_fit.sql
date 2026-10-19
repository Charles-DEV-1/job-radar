begin;

alter table public.jobs drop constraint jobs_resume_status_check;
alter table public.jobs add constraint jobs_resume_status_check
    check (resume_status in ('pending','ready','failed','archived','low_fit'));
alter table public.resume_runs drop constraint resume_runs_status_check;
alter table public.resume_runs add constraint resume_runs_status_check
    check (status in ('processing','ready','failed','low_fit'));

create or replace function public.radar_finish_tailor(
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
    if p_status is null or p_status not in ('ready', 'failed', 'low_fit')
        or p_metadata is null or jsonb_typeof(p_metadata) <> 'object'
        or (p_status in ('ready','low_fit') and (p_resume_path is null or p_error is not null))
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

revoke all on function public.radar_finish_tailor(uuid,uuid,text,text,text,text,jsonb,text,jsonb)
    from public, anon, authenticated;
grant execute on function public.radar_finish_tailor(uuid,uuid,text,text,text,text,jsonb,text,jsonb)
    to service_role;

commit;
