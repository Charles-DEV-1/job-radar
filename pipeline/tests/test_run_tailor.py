import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from postgres_helpers import PostgresStore
from resume_helpers import (
    ControlledBackend,
    PgResumeStore,
    synthetic_resume,
    tailoring_proposal,
    verified_pagination,
)
from test_storage_sql import prepared, snapshot, source_ids

from pipeline import run_tailor
from pipeline.detector import RunRecorder
from pipeline.resume.pdf import Pagination
from pipeline.resume.tailor import TailoringClient
from pipeline.storage import StoreError


def insert_jobs(pg: PostgresStore, filters: list[str]) -> list[dict[str, Any]]:
    source, _ = source_ids(pg)
    jobs = [prepared(str(i), f"Backend Engineer {i}") for i in range(len(filters))]
    for posting, decision in zip(jobs, filters, strict=True):
        posting.filter_result = decision
        posting.description_text = "Requirements: Python, SQL, and AWS."
    snapshot(pg, source, jobs)
    return pg.db.query("select * from public.jobs order by external_id")


def test_only_pending_open_included_jobs_are_claimed_and_maybe_is_opt_in(
    pg_store: PostgresStore,
) -> None:
    rows = insert_jobs(pg_store, ["include", "maybe", "exclude", "include", "include"])
    pg_store.db.query(
        "update public.jobs set closed_at=now() where id=$1", [rows[3]["id"]]
    )
    pg_store.db.query(
        "update public.jobs set resume_status='ready' where id=$1", [rows[4]["id"]]
    )
    store = PgResumeStore(pg_store)
    included = store.claim(str(uuid4()), False)
    assert included and included["id"] == rows[0]["id"]
    assert store.claim(str(uuid4()), False) is None
    optional = store.claim(str(uuid4()), True)
    assert optional and optional["id"] == rows[1]["id"]
    assert store.claim(str(uuid4()), True) is None
    assert len(pg_store.db.query("select * from public.resume_runs")) == 2


def test_claim_transport_retry_returns_same_job_without_a_second_run(
    pg_store: PostgresStore,
) -> None:
    insert_jobs(pg_store, ["include", "include"])
    store = PgResumeStore(pg_store)
    claim = str(uuid4())
    first = store.claim(claim, False)
    assert store.claim(claim, False) == first
    assert len(pg_store.db.query("select * from public.resume_runs")) == 1


def test_claim_outage_still_records_usage_from_completed_jobs(
    tmp_path: Path,
    pg_store: PostgresStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    insert_jobs(pg_store, ["include", "include"])
    store = PgResumeStore(pg_store)
    original_claim = store.claim
    calls = 0

    def claim(claim_id: str, include_maybe: bool) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise StoreError("Claim service unavailable")
        return original_claim(claim_id, include_maybe)

    monkeypatch.setattr(store, "claim", claim)
    report = tmp_path / "run.jsonl"
    with pytest.raises(StoreError, match="unavailable"):
        run_tailor.run_jobs(
            store,
            TailoringClient(ControlledBackend([tailoring_proposal(master)])),
            master,
            original,
            template,
            layout,
            tmp_path / "output",
            RunRecorder(report),
            pagination=verified_pagination(),
        )
    total = json.loads(report.read_text(encoding="utf-8").splitlines()[-1])
    assert total["event"] == "tailor_run" and total["jobs"] == 1
    assert total["llm"]["input_tokens"] == 100 and total["error"]


def test_run_cap_private_upload_notes_usage_and_repeat_idempotence(
    tmp_path: Path, pg_store: PostgresStore
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    insert_jobs(pg_store, ["include", "include", "include"])
    proposed = tailoring_proposal(master)
    backend = ControlledBackend([proposed] * 3)
    client = TailoringClient(backend, 1, 2, sleep=lambda _: None)
    store = PgResumeStore(pg_store)
    report = tmp_path / "tailor-runs.jsonl"
    args = (
        store,
        client,
        master,
        original,
        template,
        layout,
        tmp_path / "output",
        RunRecorder(report),
    )
    first = run_tailor.run_jobs(*args, limit=2, pagination=verified_pagination())
    assert len(first) == 2 and all(result.status == "ready" for result in first)
    assert len(store.uploads) == 2
    saved = pg_store.db.query(
        "select id,resume_status,resume_path,match_notes from public.jobs"
    )
    assert sum(r["resume_status"] == "pending" for r in saved) == 1
    for row in saved:
        if row["resume_status"] == "ready":
            assert row["resume_path"] in store.uploads
            assert [item["requirement"] for item in row["match_notes"]["matched"]] == [
                "Python",
                "SQL",
            ]
            assert row["match_notes"]["gaps"] == ["AWS"]
    assert (
        len(run_tailor.run_jobs(*args, limit=2, pagination=verified_pagination())) == 1
    )
    assert run_tailor.run_jobs(*args, limit=2, pagination=verified_pagination()) == []
    assert len(backend.calls) == len(store.uploads) == 3
    logs = [json.loads(line) for line in report.read_text().splitlines()]
    run = next(r for r in logs if r["event"] == "tailor_run" and r["jobs"] == 2)
    assert run["llm"]["input_tokens"] == 200 and run["llm"][
        "cost_usd"
    ] == pytest.approx(0.00032)
    assert master.summary not in report.read_text()
    assert master.contact.name not in report.read_text()
    assert all(
        r["resume_status"] == "ready"
        for r in pg_store.db.query("select resume_status from public.jobs")
    )


def test_local_only_tailoring_uses_deterministic_changes_without_llm(
    tmp_path: Path, pg_store: PostgresStore
) -> None:
    master, original, template, layout = synthetic_resume(
        tmp_path,
        skills=[
            {"name": "Databases", "items": ["SQL"]},
            {"name": "Backend", "items": ["Python", "FastAPI"]},
        ],
        experience_bullets=[
            "Used SQL to reduce response by 15%.",
            "Built Python APIs serving 20 users.",
        ],
    )
    insert_jobs(pg_store, ["include"])
    store = PgResumeStore(pg_store)
    results = run_tailor.run_jobs(
        store,
        None,
        master,
        original,
        template,
        layout,
        tmp_path / "output",
        RunRecorder(tmp_path / "runs"),
        local_only=True,
        pagination=verified_pagination(),
    )
    assert len(results) == 1 and results[0].status == "ready"
    assert results[0].usage.attempts == 0
    assert results[0].changes["tailoring_changed"]
    assert "no resume content sent to an LLM" in " ".join(results[0].warnings)
    saved = pg_store.db.query("select resume_path,resume_status from public.jobs")[0]
    assert saved["resume_status"] == "ready"
    assert saved["resume_path"] in store.uploads
    assert store.uploads[saved["resume_path"]] != original.read_bytes()


def test_invalid_model_order_field_retries_without_controlling_ordering(
    tmp_path: Path, pg_store: PostgresStore
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    insert_jobs(pg_store, ["include", "include"])
    good = tailoring_proposal(master)
    bad = {**tailoring_proposal(master), "skills_order": ["InventedSkill"]}
    backend = ControlledBackend([bad, good, good])
    store = PgResumeStore(pg_store)
    results = run_tailor.run_jobs(
        store,
        TailoringClient(backend, sleep=lambda _: None),
        master,
        original,
        template,
        layout,
        tmp_path / "output",
        RunRecorder(tmp_path / "runs"),
        pagination=verified_pagination(),
    )
    assert [r.status for r in results] == ["ready", "ready"]
    assert all(r.changes["tailoring_changed"] for r in results)
    saved = pg_store.db.query(
        "select * from public.jobs where resume_status='ready' order by first_seen_at"
    )
    assert len(saved) == 2
    assert all(row["resume_path"] in store.uploads for row in saved)
    assert all(
        store.uploads[row["resume_path"]] != original.read_bytes() for row in saved
    )
    assert all(row["match_notes"] for row in saved)
    runs = pg_store.db.query(
        "select status,metadata from public.resume_runs order by started_at"
    )
    assert runs[0]["metadata"]["llm"]["attempts"] == 2
    assert not runs[0]["metadata"]["fallback"]
    assert len(backend.calls) == 3
    assert backend.calls[1]["validator_errors"]


def test_upload_failure_is_recorded_and_does_not_set_ready(
    tmp_path: Path, pg_store: PostgresStore
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    insert_jobs(pg_store, ["include"])
    results = run_tailor.run_jobs(
        PgResumeStore(pg_store, upload_error=True),
        TailoringClient(
            ControlledBackend([tailoring_proposal(master)]), sleep=lambda _: None
        ),
        master,
        original,
        template,
        layout,
        tmp_path / "output",
        RunRecorder(tmp_path / "runs"),
        pagination=verified_pagination(),
    )
    assert results[0].status == "failed"
    row = pg_store.db.query(
        "select resume_status,resume_path,resume_error from public.jobs"
    )[0]
    assert (
        row["resume_status"] == "failed"
        and row["resume_path"] is None
        and row["resume_error"]
    )
    assert (
        pg_store.db.query("select status from public.resume_runs")[0]["status"]
        == "failed"
    )


def test_low_fit_is_saved_without_llm_and_keeps_master_download(
    tmp_path: Path, stage8_store: Any, capsys: Any
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    rows = insert_jobs(stage8_store, ["include"])
    stage8_store.db.query(
        "update public.jobs set title='iOS Developer', "
        "description_text='Requirements: Swift, UIKit, and iOS development.' "
        "where id=$1",
        [rows[0]["id"]],
    )
    backend = ControlledBackend([])
    store = PgResumeStore(stage8_store)
    outcome = run_tailor.run_jobs(
        store,
        TailoringClient(backend),
        master,
        original,
        template,
        layout,
        tmp_path / "output",
        RunRecorder(tmp_path / "runs"),
        limit=1,
        pagination=Pagination(discover=False),
    )[0]
    row = stage8_store.db.query(
        "select resume_status,resume_path from public.jobs where id=$1",
        [rows[0]["id"]],
    )[0]
    run = stage8_store.db.query("select status from public.resume_runs")[0]
    assert outcome.status == "low_fit"
    run_tailor.print_outcome(outcome)
    assert "Low fit" in capsys.readouterr().out
    assert row["resume_status"] == run["status"] == "low_fit"
    assert row["resume_path"] in store.uploads
    assert store.uploads[row["resume_path"]] == original.read_bytes()
    assert backend.calls == []


def test_expired_claim_recovery_stale_job_rejection_and_completion_replay(
    pg_store: PostgresStore,
) -> None:
    rows = insert_jobs(pg_store, ["include"])
    store = PgResumeStore(pg_store)
    expired = str(uuid4())
    assert store.claim(expired, False)
    pg_store.db.query(
        "update public.jobs set resume_claim_until=now()-interval '1 minute' "
        "where id=$1",
        [rows[0]["id"]],
    )
    fresh = str(uuid4())
    job = store.claim(fresh, False)
    assert (
        job
        and pg_store.db.query(
            "select status from public.resume_runs where id=$1", [expired]
        )[0]["status"]
        == "failed"
    )
    path = f"{job['id']}/{fresh}/Resume.docx"
    assert not store.finish(
        job,
        expired,
        "ready",
        f"{job['id']}/{expired}/Resume.docx",
        "Resume.docx",
        {},
        None,
        {},
    )
    assert store.finish(
        job, fresh, "ready", path, "Resume.docx", {"matched": [], "gaps": []}, None, {}
    )
    assert store.finish(job, fresh, "ready", path, "Resume.docx", {}, None, {})
    assert (
        pg_store.db.query("select resume_path from public.jobs")[0]["resume_path"]
        == path
    )


@pytest.mark.parametrize("change", ["closed", "excluded", "edited", "maybe"])
def test_job_changes_prevent_stale_resume_completion(
    pg_store: PostgresStore, change: str
) -> None:
    insert_jobs(pg_store, ["include"])
    store = PgResumeStore(pg_store)
    claim = str(uuid4())
    job = store.claim(claim, False)
    fields = {
        "closed": "closed_at=now()",
        "excluded": "filter_result='exclude'",
        "edited": "content_hash=repeat('b',64)",
        "maybe": "filter_result='maybe'",
    }
    pg_store.db.query(
        f"update public.jobs set {fields[change]} where id=$1", [job["id"]]
    )
    assert not store.finish(
        job,
        claim,
        "ready",
        f"{job['id']}/{claim}/Resume.docx",
        "Resume.docx",
        {},
        None,
        {},
    )
    saved = pg_store.db.query(
        "select resume_status,resume_path,resume_claim_id from public.jobs"
    )[0]
    assert saved == {
        "resume_status": "pending",
        "resume_path": None,
        "resume_claim_id": None,
    }
    assert (
        pg_store.db.query("select status from public.resume_runs")[0]["status"]
        == "failed"
    )


def test_private_bucket_and_rpc_remain_inaccessible_to_clients(
    pg_store: PostgresStore,
) -> None:
    pg_store.db.query("reset role")
    pg_store.db.query("grant select,insert on storage.objects to anon,authenticated")
    pg_store.db.query(
        "create policy test_permissive_objects on storage.objects for all "
        "to anon,authenticated using (true) with check (true)"
    )
    pg_store.db.query("insert into storage.objects(bucket_id) values ('radar-resumes')")
    try:
        for role in ("anon", "authenticated"):
            pg_store.db.query(f"set role {role}")
            assert (
                pg_store.db.query(
                    "select * from storage.objects where bucket_id='radar-resumes'"
                )
                == []
            )
            with pytest.raises(RuntimeError, match="row-level security"):
                pg_store.db.query(
                    "insert into storage.objects(bucket_id) values ('radar-resumes')"
                )
            with pytest.raises(RuntimeError, match="permission denied"):
                pg_store.db.query(
                    "select public.radar_claim_tailor($1::uuid,false)", [str(uuid4())]
                )
            with pytest.raises(RuntimeError, match="permission denied"):
                pg_store.db.query("select * from public.resume_runs")
    finally:
        pg_store.db.query("reset role")
        pg_store.db.query("drop policy test_permissive_objects on storage.objects")
        pg_store.db.query(
            "revoke select,insert on storage.objects from anon,authenticated"
        )
        pg_store.db.query("delete from storage.objects where bucket_id='radar-resumes'")
        pg_store.db.query("set role service_role")


def test_missing_llm_credentials_leaves_jobs_unclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    path = tmp_path / "master.json"
    path.write_text(master.model_dump_json(), encoding="utf-8")
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run_tailor, "load_dotenv", lambda *args, **kwargs: None)

    def forbidden(_: Any) -> Any:
        pytest.fail("Missing LLM configuration must not open the database")

    monkeypatch.setattr(run_tailor.SupabaseStore, "from_environment", forbidden)
    assert (
        run_tailor.main(
            [
                "--master-docx",
                str(original),
                "--master-json",
                str(path),
                "--template",
                str(template),
                "--layout",
                str(layout),
            ]
        )
        == 2
    )
    assert "Configure LLM_PROVIDER" in capsys.readouterr().out
