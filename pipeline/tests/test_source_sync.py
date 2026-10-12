from dataclasses import replace

from postgres_helpers import PostgresStore

from pipeline.models import SourceConfig


def test_sync_skips_already_disabled_rows_and_preserves_mixed_sources(
    pg_store: PostgresStore,
) -> None:
    sources = [
        SourceConfig("ATS company", "workable", "board", "https://example.com/ats"),
        SourceConfig(
            "Careers company",
            "careers_page",
            "page",
            "https://example.com/jobs",
            company_domain="example.com",
            source_type="careers_page",
        ),
        SourceConfig(
            "Feed company",
            "feed",
            "feed",
            "https://example.com/feed",
            company_domain="example.com",
            source_type="feed",
        ),
        SourceConfig(
            "Disabled company",
            "lever",
            "disabled",
            "https://example.com/disabled",
            enabled=False,
        ),
    ]
    ids = pg_store.sync_sources(sources)
    page_id = ids[("careers_page", "page")]
    pg_store.db.query(
        "insert into public.source_monitor_state(source_id,state,updated_at) "
        'values ($1::uuid,\'{"hash":"baseline"}\'::jsonb,now())',
        [page_id],
    )
    # PGlite lacks the hosted safeupdate extension. This trigger enforces the
    # intended narrower update behavior: existing disabled rows stay untouched.
    # The previous unconditional UPDATE fails this regression on its second sync.
    pg_store.db.query("reset role")
    pg_store.db.query("""
        create function public.test_guard_disabled_source_update() returns trigger
        language plpgsql as $$
        begin
            if old.enabled = false and new.enabled = false then
                raise exception 'Already-disabled source was updated';
            end if;
            return new;
        end;
        $$
    """)
    pg_store.db.query("""
        create trigger test_guard_disabled_source_update
        before update on public.sources for each row
        execute function public.test_guard_disabled_source_update()
    """)
    pg_store.db.query("set role service_role")
    try:
        configured = [
            replace(sources[0], company_name="Renamed ATS company"),
            sources[1],
        ]
        expected = {
            key: ids[key] for key in (("workable", "board"), ("careers_page", "page"))
        }
        assert pg_store.sync_sources(configured) == expected
        assert pg_store.sync_sources(configured) == expected
        rows = pg_store.db.query(
            "select id,company_name,source_type,ats,enabled from public.sources"
        )
        assert len(rows) == 4
        by_id = {row["id"]: row for row in rows}
        assert (
            by_id[ids[("workable", "board")]]["company_name"] == "Renamed ATS company"
        )
        assert by_id[page_id]["enabled"] and by_id[page_id]["ats"] is None
        assert not by_id[ids[("feed", "feed")]]["enabled"]
        assert not by_id[ids[("lever", "disabled")]]["enabled"]
        assert pg_store.monitor_state(page_id) == {"hash": "baseline"}
        assert pg_store.sync_sources([]) == {}
        assert not any(
            row["enabled"]
            for row in pg_store.db.query("select enabled from public.sources")
        )
        assert pg_store.sync_sources([]) == {}
        assert pg_store.monitor_state(page_id) == {"hash": "baseline"}
        privileges = pg_store.db.query("""
            select
                has_function_privilege('service_role',
                    'public.radar_sync_sources(jsonb)','execute') as service,
                has_function_privilege('anon',
                    'public.radar_sync_sources(jsonb)','execute') as anon,
                prosecdef as definer
            from pg_proc where oid='public.radar_sync_sources(jsonb)'::regprocedure
        """)[0]
        assert privileges == {"service": True, "anon": False, "definer": False}
    finally:
        pg_store.db.query("reset role")
        pg_store.db.query(
            "drop trigger test_guard_disabled_source_update on public.sources"
        )
        pg_store.db.query("drop function public.test_guard_disabled_source_update()")
        pg_store.db.query("set role service_role")
