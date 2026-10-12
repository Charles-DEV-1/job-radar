from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from postgres_helpers import PostgresStore
from test_filters import FILTERS, example_job

from pipeline.dedupe import hash_job
from pipeline.filters import JobFilter
from pipeline.models import FetchCounts, Job, SourceConfig

BASE = datetime(2026, 10, 6, tzinfo=UTC)
SOURCE = SourceConfig("Example Company", "greenhouse", "board", "https://example.com")
OTHER = SourceConfig("Example Company", "lever", "other", "https://example.org")


def prepared(external_id: str = "1", title: str = "Backend Engineer") -> Job:
    return hash_job(
        JobFilter(FILTERS).classify(example_job(title=title, external_id=external_id))
    )


def snapshot(
    store: PostgresStore, source_id: str, jobs: list[Job], day: int = 0
) -> FetchCounts:
    run_id = str(uuid4())
    when = BASE + timedelta(days=day)
    store.start_run(source_id, run_id, when)
    return store.complete_run(run_id, jobs, when, 50)


def source_ids(store: PostgresStore) -> tuple[str, str]:
    ids = store.sync_sources([SOURCE, OTHER])
    return ids[(SOURCE.ats, SOURCE.slug)], ids[(OTHER.ats, OTHER.slug)]


def test_two_runs_backfill_and_idempotence(pg_store: PostgresStore) -> None:
    source_id, _ = source_ids(pg_store)
    first = snapshot(pg_store, source_id, [prepared()])
    second = snapshot(pg_store, source_id, [prepared()], 1)
    assert first.backfill and first.inserted_jobs == 1 and first.new_jobs == 0
    assert not second.backfill and second.inserted_jobs == second.new_jobs == 0
    rows = pg_store.db.query("select * from public.jobs")
    assert len(rows) == 1 and rows[0]["backfill"] and rows[0]["notified_at"]
    assert rows[0]["status"] == "new"
    assert (
        pg_store.db.query("select count(*)::int as n from public.source_runs")[0]["n"]
        == 2
    )


def test_new_later_jobs_only_and_exclusions(pg_store: PostgresStore) -> None:
    source_id, _ = source_ids(pg_store)
    snapshot(pg_store, source_id, [prepared()])
    later = snapshot(
        pg_store,
        source_id,
        [prepared(), prepared("2"), prepared("3", "Sales Manager")],
        1,
    )
    assert later.inserted_jobs == 2 and later.new_jobs == 1
    rows = pg_store.db.query(
        "select external_id,notified_at,backfill,filter_result "
        "from public.jobs order by external_id"
    )
    assert rows[1]["notified_at"] is None and not rows[1]["backfill"]
    assert rows[2]["filter_result"] == "exclude" and rows[2]["notified_at"]


def test_edit_keeps_identity_status_notes_and_first_seen(
    pg_store: PostgresStore,
) -> None:
    source_id, _ = source_ids(pg_store)
    original = prepared()
    snapshot(pg_store, source_id, [original])
    row = pg_store.db.query("select * from public.jobs")[0]
    pg_store.db.query(
        "update public.jobs set status='applied',notes='Already applied',"
        "resume_status='ready',resume_path='private.docx'"
    )
    edited = deepcopy(original)
    edited.title = "Senior Backend Engineer"
    edited.description_text = "Changed description"
    hash_job(edited)
    result = snapshot(pg_store, source_id, [edited], 1)
    saved = pg_store.db.query("select * from public.jobs")[0]
    assert result.new_jobs == result.inserted_jobs == 0 and result.updated_jobs == 1
    assert saved["id"] == row["id"] and saved["first_seen_at"] == row["first_seen_at"]
    assert saved["status"] == "applied" and saved["notes"] == "Already applied"
    assert saved["resume_status"] == "ready" and saved["resume_path"] == "private.docx"
    assert saved["notified_at"] == row["notified_at"]
    assert saved["canonical_key"] == edited.canonical_key


def test_cross_source_duplicate_keeps_alternate_url_and_liveness(
    pg_store: PostgresStore,
) -> None:
    first_id, other_id = source_ids(pg_store)
    job = prepared()
    snapshot(pg_store, first_id, [job])
    alternate = deepcopy(job)
    alternate.source, alternate.source_company_slug, alternate.external_id = (
        "lever",
        "other",
        "abc",
    )
    alternate.apply_url = "https://example.org/alternate"
    result = snapshot(pg_store, other_id, [alternate], 1)
    assert result.duplicates == 1 and result.inserted_jobs == result.new_jobs == 0
    assert len(pg_store.db.query("select * from public.jobs")) == 1
    assert len(pg_store.db.query("select * from public.job_sources")) == 2
    snapshot(pg_store, first_id, [], 2)
    row = pg_store.db.query("select * from public.jobs")[0]
    assert row["closed_at"] is None and row["apply_url"] == alternate.apply_url
    final = snapshot(pg_store, other_id, [], 3)
    assert final.closed_jobs == 1
    assert pg_store.db.query("select closed_at from public.jobs")[0]["closed_at"]
    reopened = snapshot(pg_store, first_id, [job], 4)
    assert reopened.new_jobs == reopened.inserted_jobs == 0
    assert (
        pg_store.db.query("select closed_at from public.jobs")[0]["closed_at"] is None
    )


def test_same_source_distinct_requisitions_are_preserved(
    pg_store: PostgresStore,
) -> None:
    source_id, _ = source_ids(pg_store)
    result = snapshot(pg_store, source_id, [prepared("1"), prepared("2")])
    assert result.inserted_jobs == 2
    assert len(pg_store.db.query("select * from public.jobs")) == 2


def test_failed_snapshot_rolls_back_and_does_not_close_jobs(
    pg_store: PostgresStore,
) -> None:
    source_id, _ = source_ids(pg_store)
    snapshot(pg_store, source_id, [prepared()])
    bad = prepared("2")
    bad.filter_result = "invalid"
    run_id = str(uuid4())
    when = BASE + timedelta(days=1)
    pg_store.start_run(source_id, run_id, when)
    with pytest.raises(RuntimeError):
        pg_store.complete_run(run_id, [prepared("3"), bad], when, 100)
    pg_store.fail_run(run_id, "Invalid job", 100, when)
    pg_store.fail_run(run_id, "Repeated failure report", 100, when)
    assert len(pg_store.db.query("select * from public.jobs")) == 1
    assert (
        pg_store.db.query("select closed_at from public.jobs")[0]["closed_at"] is None
    )
    health = pg_store.db.query(
        "select * from public.sources where id=$1::uuid", [source_id]
    )[0]
    assert health["consecutive_failures"] == 1 and health["last_job_count"] == 1
    assert health["last_error"] == "Invalid job"


def test_failure_before_first_success_preserves_backfill(
    pg_store: PostgresStore,
) -> None:
    source_id, _ = source_ids(pg_store)
    run_id = str(uuid4())
    pg_store.start_run(source_id, run_id, BASE)
    pg_store.fail_run(run_id, "Timeout", 100, BASE)
    success = snapshot(pg_store, source_id, [prepared()], 1)
    assert success.backfill and success.new_jobs == 0
    health = pg_store.db.query(
        "select * from public.sources where id=$1::uuid", [source_id]
    )[0]
    assert health["consecutive_failures"] == 0 and health["last_error"] is None


def test_rpc_replay_and_older_snapshot_cannot_regress_state(
    pg_store: PostgresStore,
) -> None:
    source_id, _ = source_ids(pg_store)
    run_id = str(uuid4())
    pg_store.start_run(source_id, run_id, BASE)
    first = pg_store.complete_run(run_id, [prepared()], BASE, 10)
    assert pg_store.complete_run(run_id, [prepared()], BASE, 10) == first
    stale_id = str(uuid4())
    pg_store.start_run(source_id, stale_id, BASE + timedelta(hours=1))
    snapshot(pg_store, source_id, [prepared(), prepared("2")], 1)
    with pytest.raises(RuntimeError, match="newer snapshot"):
        pg_store.complete_run(stale_id, [], BASE + timedelta(days=2), 10)
    assert len(pg_store.db.query("select * from public.jobs")) == 2


def test_rls_and_rpc_permissions_deny_public_roles(pg_store: PostgresStore) -> None:
    source_id, _ = source_ids(pg_store)
    snapshot(pg_store, source_id, [prepared()])
    db = pg_store.db
    db.query("reset role")
    enabled = db.query(
        "select relname,relrowsecurity from pg_class "
        "where relname in ('sources','source_runs','jobs','job_sources')"
    )
    assert len(enabled) == 4 and all(row["relrowsecurity"] for row in enabled)
    assert not db.query(
        "select * from pg_policies "
        "where tablename in ('sources','source_runs','jobs','job_sources')"
    )
    for role in ("anon", "authenticated"):
        db.query("set role " + role)
        with pytest.raises(RuntimeError, match="permission denied"):
            db.query("select * from public.jobs")
        with pytest.raises(RuntimeError, match="permission denied"):
            db.query("select public.radar_sync_sources('[]'::jsonb)")
        db.query("reset role")
    db.query("set role service_role")


def test_rls_still_hides_rows_if_select_is_accidentally_granted(
    pg_store: PostgresStore,
) -> None:
    source_id, _ = source_ids(pg_store)
    snapshot(pg_store, source_id, [prepared()])
    db = pg_store.db
    db.query("reset role")
    db.query("grant select on public.jobs to anon")
    try:
        db.query("set role anon")
        assert db.query("select * from public.jobs") == []
    finally:
        db.query("reset role")
        db.query("revoke select on public.jobs from anon")
        db.query("set role service_role")


def test_edit_to_excluded_cannot_reenter_notification_queue(
    pg_store: PostgresStore,
) -> None:
    source_id, _ = source_ids(pg_store)
    snapshot(pg_store, source_id, [])
    snapshot(pg_store, source_id, [prepared()], 1)
    assert (
        pg_store.db.query("select notified_at from public.jobs")[0]["notified_at"]
        is None
    )
    edited = prepared(title="Sales Manager")
    result = snapshot(pg_store, source_id, [edited], 2)
    assert result.new_jobs == 0
    assert pg_store.db.query("select notified_at from public.jobs")[0]["notified_at"]
    result = snapshot(pg_store, source_id, [prepared()], 3)
    assert result.new_jobs == 0
    assert pg_store.db.query("select notified_at from public.jobs")[0]["notified_at"]


def test_source_sync_disables_removed_and_unconfirmed_entries(
    pg_store: PostgresStore,
) -> None:
    first_id, other_id = source_ids(pg_store)
    pg_store.sync_sources([SOURCE])
    rows = pg_store.db.query("select id,enabled from public.sources")
    assert {row["id"]: row["enabled"] for row in rows} == {
        first_id: True,
        other_id: False,
    }
