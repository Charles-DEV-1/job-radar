"""Stored-job reclassification is guarded and updates filter fields only."""

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions
from test_filters import FILTERS, example_job

from pipeline.detector import RunRecorder
from pipeline.filters import JobFilter
from pipeline.models import Job
from pipeline.run_filter import SupabaseFilterStore, run_filter
from pipeline.storage import StoreError, SupabaseStore


def sample_rows() -> list[dict[str, Any]]:
    rows = []
    for index, title in enumerate(
        ("Engineering Manager", "Senior Backend Engineer", "Junior Backend Engineer")
    ):
        row = asdict(example_job(title, description="Requirements: Python SQL AWS"))
        row.update(
            id=str(index),
            content_hash="a" * 64,
            filter_result="include",
            filter_reason="Old reason",
            resume_status="ready",
            resume_path="private.docx",
            notified_at="2026-10-12",
        )
        rows.append(row)
    return rows


class MemoryStore:
    def __init__(self) -> None:
        self.data = sample_rows()
        self.conflicts: set[str] = set()
        self.errors: set[str] = set()

    def rows(self) -> list[dict[str, Any]]:
        return deepcopy(self.data)

    def update(self, row: dict[str, Any], job: Job) -> bool:
        if row["id"] in self.errors:
            raise StoreError("Mock stored-job update failed")
        if row["id"] in self.conflicts:
            return False
        saved = next(item for item in self.data if item["id"] == row["id"])
        saved.update(filter_result=job.filter_result, filter_reason=job.filter_reason)
        return True


def test_preview_apply_and_repeat_are_idempotent(tmp_path: Path) -> None:
    store = MemoryStore()
    original = deepcopy(store.data)
    recorder = RunRecorder(tmp_path / "filter-runs.jsonl")
    preview = run_filter(store, JobFilter(FILTERS), recorder)
    assert preview.category_changed == 2 and store.data == original
    result = run_filter(store, JobFilter(FILTERS), recorder, apply=True)
    assert (
        result.scanned == 3 and result.category_changed == 2 and result.reason_only == 1
    )
    assert result.transitions == {"include -> exclude": 1, "include -> maybe": 1}
    for old, saved in zip(original, store.data, strict=True):
        assert {
            key: value for key, value in old.items() if not key.startswith("filter_")
        } == {
            key: value for key, value in saved.items() if not key.startswith("filter_")
        }
    again = run_filter(store, JobFilter(FILTERS), recorder, apply=True)
    assert again.category_changed == again.reason_only == 0
    assert "Requirements: Python" not in recorder.path.read_text()


def test_conflicts_and_errors_are_recorded_and_isolated(tmp_path: Path) -> None:
    store = MemoryStore()
    store.errors.add("0")
    store.conflicts.add("1")
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    result = run_filter(store, JobFilter(FILTERS), recorder, apply=True)
    assert result.failures == result.conflicts == result.reason_only == 1
    assert result.category_changed == 0
    assert store.data[0]["filter_result"] == store.data[1]["filter_result"] == "include"
    events = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    assert events[0]["status"] == "error" and events[1]["status"] == "conflict"


def test_supabase_http_pagination_and_guarded_filter_only_updates() -> None:
    sample = sample_rows()[0]
    reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        assert request.url.path == "/rest/v1/jobs"
        if request.method == "GET":
            reads += 1
            assert request.url.params["order"] == "id.asc"
            assert request.url.params["offset"] == ("0" if reads == 1 else "500")
            return httpx.Response(
                200, json=[{"id": "sample"}] * (500 if reads == 1 else 1)
            )
        assert request.method == "PATCH"
        assert set(json.loads(request.content)) == {"filter_result", "filter_reason"}
        for key in ("id", "content_hash", "filter_result", "filter_reason"):
            assert request.url.params[key] == "eq." + sample[key]
        return httpx.Response(200, json=[{"id": sample["id"]}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        sdk = create_client(
            "https://project.supabase.co",
            "sb_secret_mock_key_for_tests_only",
            options=SyncClientOptions(httpx_client=session),
        )
        store = SupabaseFilterStore(SupabaseStore(sdk))
        assert len(store.rows()) == 501 and reads == 2
        assert store.update(
            sample, JobFilter(FILTERS).classify(example_job("Engineering Manager"))
        )


@pytest.mark.parametrize("operation", ["read", "update"])
def test_supabase_errors_are_sanitized(operation: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "23514",
                "message": "secret full private description",
                "details": None,
                "hint": None,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        sdk = create_client(
            "https://project.supabase.co",
            "sb_secret_mock_key_for_tests_only",
            options=SyncClientOptions(httpx_client=session),
        )
        store = SupabaseFilterStore(SupabaseStore(sdk))
        with pytest.raises(StoreError) as exc:
            if operation == "read":
                store.rows()
            else:
                store.update(sample_rows()[0], example_job())
    assert "secret" not in str(exc.value) and "private description" not in str(
        exc.value
    )
