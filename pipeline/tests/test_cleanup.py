from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_storage_sql import prepared, snapshot, source_ids

from pipeline.cleanup import (
    CleanupConfig,
    run_cleanup,
    valid_resume_path,
)
from pipeline.detector import RunRecorder

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
CONFIG = CleanupConfig(60, 30, 30)


def row(
    status: str,
    *,
    closed_days: int | None = None,
    changed_days: int | None = 40,
    path: str | None = None,
    text: str | None = "Description text",
    html: str | None = "<p>Description</p>",
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "source_id": str(uuid4()),
        "external_id": str(uuid4()),
        "canonical_key": "a" * 64,
        "status": status,
        "closed_at": (
            (NOW - timedelta(days=closed_days)).isoformat()
            if closed_days is not None
            else None
        ),
        "status_changed_at": (
            (NOW - timedelta(days=changed_days)).isoformat()
            if changed_days is not None
            else None
        ),
        "resume_status": "ready" if path else "pending",
        "resume_path": path,
        "description_text": text,
        "description_html": html,
        "notes": "Preserve this note",
    }


class MemoryCleanupStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.data = deepcopy(rows)
        self.removed: list[str] = []
        self.tombstones: list[dict[str, Any]] = []
        self.fail_removal = False

    def rows(self) -> list[dict[str, Any]]:
        return deepcopy(self.data)

    def pending_deleted_resumes(self) -> list[dict[str, Any]]:
        return deepcopy([row for row in self.tombstones if row.get("resume_path")])

    def delete_closed(
        self, candidate: dict[str, Any], cutoff: datetime
    ) -> tuple[bool, str | None]:
        current = next(
            (item for item in self.data if item["id"] == candidate["id"]), None
        )
        closed = datetime.fromisoformat(current["closed_at"]) if current else None
        if (
            current is None
            or current["status"] not in {"new", "skipped"}
            or closed is None
            or closed >= cutoff
        ):
            return False, None
        self.tombstones.append(
            {
                "source_id": current["source_id"],
                "external_id": current["external_id"],
                "resume_path": current["resume_path"],
            }
        )
        self.data.remove(current)
        return True, current["resume_path"]

    def archive(self, candidate: dict[str, Any], cutoff: datetime) -> bool:
        current = next(
            (item for item in self.data if item["id"] == candidate["id"]), None
        )
        changed = (
            datetime.fromisoformat(current["status_changed_at"]) if current else None
        )
        if (
            current is None
            or current["status"] != candidate["status"]
            or changed is None
            or changed >= cutoff
        ):
            return False
        current["resume_status"] = "archived"
        current["description_text"] = None
        current["description_html"] = None
        return True

    def clear_skipped_descriptions(
        self, candidate: dict[str, Any], cutoff: datetime
    ) -> bool:
        current = next(
            (item for item in self.data if item["id"] == candidate["id"]), None
        )
        changed = (
            datetime.fromisoformat(current["status_changed_at"]) if current else None
        )
        if current is None or current["status"] != "skipped" or current["closed_at"]:
            return False
        if changed is None or changed >= cutoff:
            return False
        current["description_text"] = None
        current["description_html"] = None
        return True

    def delete_resume(self, path: str) -> None:
        if self.fail_removal:
            raise RuntimeError("controlled storage failure")
        if not valid_resume_path(path):
            raise ValueError("invalid tailored resume path")
        self.removed.append(path)

    def clear_archived_resume(self, job_id: str, path: str) -> bool:
        current = next((item for item in self.data if item["id"] == job_id), None)
        if current is None or current["resume_status"] != "archived":
            return False
        if current["resume_path"] != path:
            return False
        current["resume_path"] = None
        return True

    def finish_deleted_resume(
        self, source_id: str, external_id: str, path: str
    ) -> bool:
        for tombstone in self.tombstones:
            if (
                tombstone["source_id"] == source_id
                and tombstone["external_id"] == external_id
                and tombstone["resume_path"] == path
            ):
                tombstone["resume_path"] = None
                return True
        return False


def test_cleanup_rules_preserve_open_important_and_recent_jobs(tmp_path: Path) -> None:
    path = f"{uuid4()}/{uuid4()}/Resume.docx"
    rows = [
        row("new", closed_days=61, changed_days=100, path=path),
        row("skipped", closed_days=61, changed_days=100),
        row(
            "applied",
            closed_days=5,
            changed_days=31,
            path=f"{uuid4()}/{uuid4()}/Applied.docx",
        ),
        row("rejected", changed_days=31),
        row("silent", changed_days=31),
        row("applied", changed_days=29),
        row("skipped", changed_days=31),
        row("skipped", closed_days=31, changed_days=90),
        row("new", changed_days=100),
        row(
            "interview",
            closed_days=400,
            changed_days=400,
            path=f"{uuid4()}/{uuid4()}/Interview.docx",
        ),
        row("offer", closed_days=400, changed_days=400),
    ]
    store = MemoryCleanupStore(rows)
    before = deepcopy(store.data)
    dry = run_cleanup(
        store, CONFIG, RunRecorder(tmp_path / "dry.jsonl"), now=NOW, dry_run=True
    )
    assert dry.closed_jobs_deleted == 2
    assert dry.inactive_jobs_archived == 3
    assert dry.open_skipped_descriptions_cleared == 1
    assert store.data == before and store.removed == []

    result = run_cleanup(store, CONFIG, RunRecorder(tmp_path / "live.jsonl"), now=NOW)
    assert result.closed_jobs_deleted == 2
    assert result.inactive_jobs_archived == 3
    assert result.open_skipped_descriptions_cleared == 1
    assert result.resumes_deleted == 2
    assert len(store.data) == 9
    archived = [item for item in store.data if item["resume_status"] == "archived"]
    assert len(archived) == 3
    assert all(item["description_text"] is None for item in archived)
    assert all(item["description_html"] is None for item in archived)
    assert all(item["notes"] == "Preserve this note" for item in archived)
    open_skipped = next(
        item
        for item in store.data
        if item["status"] == "skipped" and item["closed_at"] is None
    )
    assert open_skipped["description_text"] is None
    assert open_skipped["resume_status"] == "pending"
    assert any(item["status"] == "interview" for item in store.data)
    assert any(item["status"] == "offer" for item in store.data)


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    candidate = row("applied", changed_days=31)
    store = MemoryCleanupStore([candidate])
    run_cleanup(store, CONFIG, RunRecorder(tmp_path / "first.jsonl"), now=NOW)
    second = run_cleanup(store, CONFIG, RunRecorder(tmp_path / "second.jsonl"), now=NOW)
    assert second.inactive_jobs_archived == 0
    assert store.data[0]["resume_status"] == "archived"


def test_deleted_resume_removal_retries_from_tombstone(tmp_path: Path) -> None:
    candidate = row("new", closed_days=61, path=f"{uuid4()}/{uuid4()}/Resume.docx")
    store = MemoryCleanupStore([candidate])
    store.fail_removal = True
    first = run_cleanup(store, CONFIG, RunRecorder(tmp_path / "failed.jsonl"), now=NOW)
    assert first.closed_jobs_deleted == 1 and first.errors == 1
    assert len(store.data) == 0 and store.tombstones[0]["resume_path"]

    store.fail_removal = False
    second = run_cleanup(store, CONFIG, RunRecorder(tmp_path / "retry.jsonl"), now=NOW)
    assert second.pending_resume_retries == 1
    assert second.resumes_deleted == 1 and second.errors == 0
    assert store.tombstones[0]["resume_path"] is None


def test_cleanup_config_is_editable_and_validated(tmp_path: Path) -> None:
    config = tmp_path / "cleanup.yaml"
    config.write_text(
        "retention:\n  closed_new_or_skipped_days: 45\n"
        "  applied_rejected_silent_days: 20\n"
        "  open_skipped_description_days: 15\n",
        encoding="utf-8",
    )
    assert CleanupConfig.load(config) == CleanupConfig(45, 20, 15)
    config.write_text("retention:\n  closed_new_or_skipped_days: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="closed_new_or_skipped_days"):
        CleanupConfig.load(config)


def test_private_resume_path_validation_prevents_master_or_traversal_deletion() -> None:
    valid = f"{uuid4()}/{uuid4()}/Resume.docx"
    assert valid_resume_path(valid)
    assert not valid_resume_path("master/master_resume.docx")
    assert not valid_resume_path(f"{uuid4()}/../master_resume.docx")


def test_deleted_job_is_tombstoned_and_cannot_reappear_as_new(
    stage7_store: Any,
) -> None:
    store = stage7_store
    source_id, _ = source_ids(store)
    job = prepared("retained-tombstone")
    snapshot(store, source_id, [job])
    store.db.query(
        "update public.jobs set closed_at='2026-08-01T00:00:00Z' "
        "where external_id='retained-tombstone'"
    )
    store.db.query(
        "update public.job_sources set closed_at='2026-08-01T00:00:00Z' "
        "where external_id='retained-tombstone'"
    )
    result = store.db.query(
        "select public.radar_cleanup_delete_closed_job("
        "$1::uuid,$2::timestamptz) as result",
        [
            store.db.query(
                "select id from public.jobs where external_id='retained-tombstone'"
            )[0]["id"],
            "2026-08-20T00:00:00Z",
        ],
    )[0]["result"]
    assert result["deleted"] is True
    assert store.db.query("select * from public.jobs") == []

    rerun = snapshot(store, source_id, [job], day=1)
    assert rerun.inserted_jobs == rerun.new_jobs == 0
    assert store.db.query("select * from public.jobs") == []
    assert (
        store.db.query(
            "select count(*)::int as count from public.job_cleanup_tombstones"
        )[0]["count"]
        == 1
    )


def test_description_cleanup_stays_applied_after_source_refresh(
    stage7_store: Any,
) -> None:
    store = stage7_store
    source_id, _ = source_ids(store)
    skipped = prepared("open-skipped")
    applied = prepared("old-applied")
    snapshot(store, source_id, [skipped, applied])
    store.db.query(
        "update public.jobs set status='skipped' where external_id='open-skipped'"
    )
    store.db.query(
        "update public.jobs set status_changed_at='2026-08-01T00:00:00Z' "
        "where external_id='open-skipped'"
    )
    store.db.query(
        "update public.jobs set status='applied' where external_id='old-applied'"
    )
    store.db.query(
        "update public.jobs set status_changed_at='2026-08-01T00:00:00Z' "
        "where external_id='old-applied'"
    )
    skipped_id, applied_id = store.db.query(
        "select id,external_id from public.jobs order by external_id"
    )
    ids = {row["external_id"]: row["id"] for row in [skipped_id, applied_id]}
    store.db.query(
        "update public.jobs set notes='Keep this note' where id=$1",
        [ids["old-applied"]],
    )
    history_before = store.db.query(
        "select count(*)::int as count from public.status_history where job_id=$1",
        [ids["old-applied"]],
    )[0]["count"]
    assert store.db.query(
        "select public.radar_cleanup_clear_open_skipped("
        "$1::uuid,$2::timestamptz) as result",
        [ids["open-skipped"], "2026-09-19T00:00:00Z"],
    )[0]["result"]
    assert store.db.query(
        "select public.radar_cleanup_archive_job("
        "$1::uuid,'applied',$2::timestamptz) as result",
        [ids["old-applied"], "2026-09-19T00:00:00Z"],
    )[0]["result"]

    snapshot(store, source_id, [skipped, applied], day=1)
    stored = store.db.query(
        "select external_id,description_text,description_html,resume_status "
        "from public.jobs order by external_id"
    )
    assert all(row["description_text"] is None for row in stored)
    assert all(row["description_html"] is None for row in stored)
    assert (
        next(row for row in stored if row["external_id"] == "old-applied")[
            "resume_status"
        ]
        == "archived"
    )
    archived = store.db.query(
        "select notes from public.jobs where id=$1", [ids["old-applied"]]
    )[0]
    assert archived["notes"] == "Keep this note"
    history_after = store.db.query(
        "select count(*)::int as count from public.status_history where job_id=$1",
        [ids["old-applied"]],
    )[0]["count"]
    assert history_after == history_before
