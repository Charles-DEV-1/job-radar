"""Real PostgreSQL tests for the Stage 5 owner policies and atomic history."""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from postgres_helpers import PostgresEngine, PostgresStore
from test_storage_sql import prepared, snapshot, source_ids

OWNER = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def dashboard_db() -> Iterator[PostgresEngine]:
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    module = root / ".tools/pglite"
    if not node or not (module / "node_modules/@electric-sql/pglite").exists():
        pytest.skip("Install the documented PGlite dependency")
    db = PostgresEngine(
        node,
        Path(__file__).with_name("db_bridge.mjs"),
        module,
        root / "supabase/migrations/20261006000100_stage2_fetch_storage.sql",
        dashboard=True,
    )
    yield db
    db.close()


@pytest.fixture
def owner_data(dashboard_db: PostgresEngine) -> tuple[PostgresEngine, str]:
    db = dashboard_db
    db.query("reset role")
    db.query(
        "truncate public.status_history,public.notification_jobs,"
        "public.notification_batches,public.resume_runs,public.source_monitor_state,"
        "public.job_sources,public.jobs,public.source_runs,public.sources,"
        "public.dashboard_settings,auth.users"
    )
    db.query("set role service_role")
    db.query(
        "insert into auth.users(id,email,email_confirmed_at) "
        "values($1,'owner@example.test',now()),($2,'other@example.test',now())",
        [OWNER, OTHER],
    )
    db.query("select public.radar_configure_dashboard('OWNER@example.test')")
    store = PostgresStore(db)
    source, _ = source_ids(store)
    snapshot(store, source, [prepared()])
    job_id = db.query("select id from public.jobs")[0]["id"]
    return db, job_id


def authenticate(
    db: PostgresEngine, uid: str = OWNER, email: str = "owner@example.test"
) -> None:
    db.query("reset role")
    db.query("select set_config('request.jwt.claim.sub',$1,false)", [uid])
    db.query(
        "select set_config('request.jwt.claims',$1,false)",
        [json.dumps({"email": email})],
    )
    db.query("set role authenticated")


def test_only_verified_configured_owner_can_read_or_edit(
    owner_data: tuple[PostgresEngine, str],
) -> None:
    db, job_id = owner_data
    authenticate(db, OTHER, "other@example.test")
    assert db.query("select * from public.jobs") == []
    assert db.query("select * from public.status_history") == []
    assert (
        db.query(
            "update public.jobs set status='applied' where id=$1 returning id", [job_id]
        )
        == []
    )
    authenticate(db)
    assert len(db.query("select * from public.jobs")) == 1
    assert db.query("select public.radar_dashboard_is_owner() as allowed")[0]["allowed"]
    with pytest.raises(RuntimeError, match="permission denied"):
        db.query("select public.radar_configure_dashboard('other@example.test')")
    with pytest.raises(RuntimeError, match="permission denied"):
        db.query(
            "update public.jobs set description_text='invented' where id=$1", [job_id]
        )
    with pytest.raises(RuntimeError, match="permission denied"):
        db.query(
            "update public.jobs set resume_path='other.docx' where id=$1", [job_id]
        )
    db.query("reset role")
    db.query("set role anon")
    with pytest.raises(RuntimeError, match="permission denied"):
        db.query("select * from public.jobs")


def test_denies_unconfirmed_user_and_email_identity_mismatch(
    owner_data: tuple[PostgresEngine, str],
) -> None:
    db, _ = owner_data
    authenticate(db, OTHER, "owner@example.test")
    assert db.query("select * from public.jobs") == []
    db.query("reset role")
    db.query("update auth.users set email_confirmed_at=null where id=$1", [OWNER])
    authenticate(db)
    assert db.query("select * from public.jobs") == []


def test_status_history_atomic_idempotent_notes_preserve_dates(
    owner_data: tuple[PostgresEngine, str],
) -> None:
    db, job_id = owner_data
    authenticate(db)
    initial = db.query("select count(*)::int as n from public.status_history")[0]["n"]
    db.query("update public.jobs set status='applied' where id=$1", [job_id])
    timestamp = db.query("select status_changed_at from public.jobs")[0][
        "status_changed_at"
    ]
    db.query(
        "update public.jobs set status='applied',notes='Follow up' where id=$1",
        [job_id],
    )
    assert (
        db.query("select status_changed_at from public.jobs")[0]["status_changed_at"]
        == timestamp
    )
    assert (
        db.query("select count(*)::int as n from public.status_history")[0]["n"]
        == initial + 1
    )
    history = db.query("select * from public.status_history where status='applied'")[0]
    assert history["changed_at"] == timestamp
    with pytest.raises(RuntimeError, match="check constraint"):
        db.query("update public.jobs set status='invented' where id=$1", [job_id])
    assert (
        db.query("select count(*)::int as n from public.status_history")[0]["n"]
        == initial + 1
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        db.query(
            "insert into public.status_history(job_id,status,changed_at) "
            "values($1,'offer',now())",
            [job_id],
        )


def test_notification_membership_is_explicit_not_backfill_timestamps(
    owner_data: tuple[PostgresEngine, str],
) -> None:
    db, job_id = owner_data
    authenticate(db)
    assert db.query("select * from public.notification_batches") == []
    db.query("reset role")
    db.query("set role service_role")
    batch = str(uuid4())
    db.query(
        "insert into public.notification_batches(id,sent_at) values($1,now())", [batch]
    )
    db.query(
        "insert into public.notification_jobs(notification_id,job_id) values($1,$2)",
        [batch, job_id],
    )
    authenticate(db)
    assert (
        db.query("select job_id from public.notification_jobs")[0]["job_id"] == job_id
    )
    authenticate(db, OTHER, "other@example.test")
    assert db.query("select * from public.notification_jobs") == []


def test_no_direct_authenticated_resume_access(
    owner_data: tuple[PostgresEngine, str],
) -> None:
    db, _ = owner_data
    db.query("reset role")
    db.query("insert into storage.objects(bucket_id) values('radar-resumes')")
    db.query("grant select on storage.objects to authenticated")
    db.query(
        "create policy test_permissive_objects on storage.objects "
        "for select to authenticated using(true)"
    )
    authenticate(db)
    assert (
        db.query("select * from storage.objects where bucket_id='radar-resumes'") == []
    )
    db.query("reset role")
    db.query("drop policy test_permissive_objects on storage.objects")
