begin;

-- Stage 3 is already applied. Keep registry sync compatible with safeupdate,
-- which rejects an UPDATE without a WHERE clause.
create or replace function public.radar_sync_sources(p_sources jsonb) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare v_entry jsonb; v_ids jsonb := '[]'::jsonb; v_id uuid; v_type text; v_ats text;
begin
    if jsonb_typeof(p_sources) <> 'array' then raise exception 'Expected sources array'; end if;
    perform pg_advisory_xact_lock(19087621);
    update public.sources set enabled = false where enabled = true;
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

commit;
