"""Ready artifacts distinguish visible tailoring, unchanged content and fallback."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from postgres_helpers import PostgresStore
from resume_helpers import (
    ControlledBackend,
    PgResumeStore,
    job,
    synthetic_resume,
    verified_pagination,
)
from test_run_tailor import insert_jobs

from pipeline import run_tailor
from pipeline.detector import RunRecorder
from pipeline.llm.client import Usage
from pipeline.resume.pdf import Pagination
from pipeline.resume.privacy import tailoring_facts
from pipeline.resume.prompt import compact_json, request
from pipeline.resume.tailor import TailoringClient, tailor_job
from pipeline.resume.validate import unchanged


def test_deterministic_changes_print_actual_path_and_human_report(
    tmp_path: Path, pg_store: PostgresStore, capsys: Any
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    insert_jobs(pg_store, ["include"])
    outcomes = run_tailor.run_jobs(
        PgResumeStore(pg_store),
        TailoringClient(ControlledBackend([unchanged(master).model_dump()])),
        master,
        original,
        template,
        layout,
        tmp_path / "output",
        RunRecorder(tmp_path / "runs.jsonl"),
        limit=1,
        pagination=verified_pagination(),
    )
    outcome = outcomes[0]
    assert outcome.status == "ready" and outcome.path.is_file(), outcome.error
    assert outcome.changes["document_changed"] is True
    report = json.loads(outcome.report_path.read_text())
    assert report["status"] == "ready" and report["changes"] == outcome.changes
    assert master.summary in outcome.report_path.read_text()
    run = pg_store.db.query("select metadata from public.resume_runs")[0]
    assert run["metadata"]["changes"]["document_changed"] is True
    assert run["metadata"]["fallback"] is False
    run_tailor.print_outcome(outcome)
    printed = capsys.readouterr().out
    assert "ready (tailored)" in printed
    assert "Summary before:" in printed and "Summary after:" in printed
    assert "Skills before:" in printed and "Skills after:" in printed
    assert str(outcome.path.resolve()) in printed
    assert str(outcome.report_path.resolve()) in printed


def test_valid_skill_reordering_is_reported_as_tailored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    proposed = unchanged(master).model_dump()
    proposed["skills_order"] = ["SQL", "Python"]
    pagination = Pagination("controlled-renderer", discover=False)
    monkeypatch.setattr(pagination, "convert", lambda *_: 1)
    result = tailor_job(
        master,
        {**job(), "title": "Backend SQL Engineer"},
        original,
        template,
        layout,
        tmp_path / "output",
        TailoringClient(ControlledBackend([proposed])),
        Usage(),
        pagination,
    )
    assert result.status == "ready" and result.changes["document_changed"] is True
    assert result.changes["tailoring_changed"] is True
    assert result.changes["skill_groups_changed"] == 1
    assert result.changes["skills_omitted"] == 0
    assert result.changes["summary_changed"] is True
    outcome = run_tailor.Outcome(
        "synthetic-job",
        result.status,
        Usage(),
        path=result.path,
        report_path=result.report_path,
        changes=result.changes,
    )
    run_tailor.print_outcome(outcome)
    assert "ready (tailored)" in capsys.readouterr().out


def test_same_job_retry_gets_new_filename_and_retains_previous_file(
    tmp_path: Path,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    current_job = job()
    results = [
        tailor_job(
            master,
            current_job,
            original,
            template,
            layout,
            tmp_path / "output",
            TailoringClient(ControlledBackend([unchanged(master).model_dump()])),
            Usage(),
            verified_pagination(),
            attempt_id=str(uuid4()),
        )
        for _ in range(2)
    ]
    assert results[0].filename != results[1].filename
    assert all(
        result.path.is_file() and result.report_path.is_file() for result in results
    )


def test_upload_failure_reports_master_fallback_not_earlier_tailoring(
    tmp_path: Path, pg_store: PostgresStore
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    insert_jobs(pg_store, ["include"])
    outcome = run_tailor.run_jobs(
        PgResumeStore(pg_store, upload_error=True),
        TailoringClient(ControlledBackend([unchanged(master).model_dump()])),
        master,
        original,
        template,
        layout,
        tmp_path / "output",
        RunRecorder(tmp_path / "runs.jsonl"),
        limit=1,
        pagination=verified_pagination(),
    )[0]
    assert (
        outcome.status == "failed"
        and outcome.path.read_bytes() == original.read_bytes()
    )
    assert outcome.changes == {}
    assert json.loads(outcome.report_path.read_text())["status"] == "failed"


def test_prompt_compaction_keeps_all_skills_words_and_used_evidence(
    tmp_path: Path,
) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    master.approved_summary_rephrasings = []
    master.skills[0].items += ["SyntheticSkill" + str(i) for i in range(30)]
    for role in master.experience:
        for bullet in role.bullets:
            bullet.approved_rephrasings = []
    current_job = job()
    _, payload, _ = request(master, current_job)
    original_facts, original_evidence = tailoring_facts(master)
    assert payload["master"]["skills"] == original_facts["skills"]
    assert payload["job"]["description_text"] == current_job["description_text"]
    assert "approved_summary_rephrasings" not in payload["master"]
    assert (
        "approved_rephrasings" not in payload["master"]["experience"][0]["bullets"][0]
    )
    for match in payload["match_note_options"]["matched"]:
        assert all(key in payload["evidence"] for key in match["evidence_ids"])
    before = {**payload, "master": original_facts, "evidence": original_evidence}
    assert len(compact_json(before)) - len(compact_json(payload)) > 3 * 167


def test_cosmetic_changes_are_not_presented_as_tailoring(capsys: Any) -> None:
    outcome = run_tailor.Outcome(
        "synthetic-job",
        "ready",
        Usage(),
        changes={"document_changed": True, "tailoring_changed": False},
    )
    run_tailor.print_outcome(outcome)
    printed = capsys.readouterr().out
    assert "ready (formatting only)" in printed and "(tailored)" not in printed
