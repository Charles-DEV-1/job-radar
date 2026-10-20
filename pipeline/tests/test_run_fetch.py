import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from postgres_helpers import PostgresStore
from test_filters import FILTERS

from pipeline import run_fetch
from pipeline.detector import RunRecorder
from pipeline.filters import JobFilter
from pipeline.models import SourceConfig
from pipeline.run_fetch import load_sources, main, run_sources
from pipeline.sources.ats import create_adapters


def test_full_orchestrator_two_runs_and_one_failing_source(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    good = SourceConfig(
        "Moniepoint",
        "greenhouse",
        "moniepoint",
        "https://boards-api.greenhouse.io/v1/boards/moniepoint/jobs",
    )
    bad = SourceConfig(
        "Failing Company",
        "lever",
        "missing",
        "https://api.lever.co/v0/postings/missing?mode=json",
    )
    gate = threading.Barrier(2)
    complete_fixture = deepcopy(recorded["greenhouse-content"])
    complete_fixture["meta"]["total"] = len(complete_fixture["jobs"])

    def handler(request: httpx.Request) -> httpx.Response:
        gate.wait(timeout=5)
        if request.url.host == "api.lever.co":
            raise httpx.ReadTimeout("mock timeout", request=request)
        assert request.url.params.get("content") == "true"
        return httpx.Response(200, json=complete_fixture)

    http = make_http(handler)
    adapters = {adapter.name: adapter for adapter in create_adapters(http)}
    recorder = RunRecorder(tmp_path / "runs.jsonl")
    first = run_sources(
        [good, bad], adapters, pg_store, JobFilter(FILTERS), recorder, 2
    )
    second = run_sources(
        [good, bad], adapters, pg_store, JobFilter(FILTERS), recorder, 2
    )
    for results in (first, second):
        assert results[0].status == "ok" and results[1].status == "error"
        assert results[0].counts.new_jobs == 0
    assert first[0].counts.backfill and first[0].counts.inserted_jobs == 2
    assert second[0].counts.inserted_jobs == 0
    rows = pg_store.db.query("select * from public.jobs")
    assert len(rows) == 2 and all(
        row["backfill"] and row["notified_at"] for row in rows
    )
    health = pg_store.db.query("select * from public.sources where ats='lever'")[0]
    assert health["consecutive_failures"] == 2
    assert len(pg_store.db.query("select * from public.source_runs")) == 4
    logs = [
        json.loads(line)
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(logs) == 4
    assert "description" not in recorder.path.read_text(encoding="utf-8")


def test_timeout_malformed_and_empty_body_are_isolated_and_recorded(
    recorded: dict[str, Any],
    make_http: Any,
    pg_store: PostgresStore,
    tmp_path: Path,
) -> None:
    good_payload = deepcopy(recorded["greenhouse-content"])
    good_payload["meta"]["total"] = len(good_payload["jobs"])

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.lever.co":
            raise httpx.ReadTimeout("recorded timeout", request=request)
        if host == "api.ashbyhq.com":
            return httpx.Response(200, text="{")
        if host == "apply.workable.com":
            return httpx.Response(200, text="")
        assert host == "boards-api.greenhouse.io"
        return httpx.Response(200, json=good_payload)

    sources = [
        SourceConfig(
            f"{provider} case",
            provider,
            f"{provider}-audit",
            f"https://{host}/fixture",
        )
        for provider, host in (
            ("lever", "api.lever.co"),
            ("ashby", "api.ashbyhq.com"),
            ("workable", "apply.workable.com"),
            ("greenhouse", "boards-api.greenhouse.io"),
        )
    ]
    recorder = RunRecorder(tmp_path / "isolation.jsonl")
    adapters = {
        adapter.name: adapter for adapter in create_adapters(make_http(handler))
    }

    outcomes = run_sources(
        sources, adapters, pg_store, JobFilter(FILTERS), recorder, concurrency=4
    )

    assert [outcome.status for outcome in outcomes] == [
        "error",
        "error",
        "error",
        "ok",
    ]
    assert outcomes[0].error == "ReadTimeout"
    assert outcomes[1].error and outcomes[1].error.startswith("Expecting property name")
    assert outcomes[2].error and outcomes[2].error.startswith("Expecting value:")
    assert len(pg_store.db.query("select * from public.jobs")) == len(
        good_payload["jobs"]
    )
    source_runs = pg_store.db.query(
        "select s.ats,r.status,r.error_message from public.source_runs r "
        "join public.sources s on s.id=r.source_id"
    )
    assert {row["ats"] for row in source_runs if row["status"] == "error"} == {
        "lever",
        "ashby",
        "workable",
    }
    assert len([row for row in source_runs if row["status"] == "ok"]) == 1
    assert all(row["error_message"] for row in source_runs if row["status"] == "error")


def test_malformed_job_keeps_previous_snapshot(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    payload = deepcopy(recorded["greenhouse-content"])
    payload["meta"]["total"] = len(payload["jobs"])
    source = SourceConfig(
        "Moniepoint", "greenhouse", "moniepoint", "https://example.com"
    )
    http = make_http(lambda _: httpx.Response(200, json=payload))
    adapters = {adapter.name: adapter for adapter in create_adapters(http)}
    recorder = RunRecorder(tmp_path / "runs")
    first = run_sources([source], adapters, pg_store, JobFilter(FILTERS), recorder)
    assert first[0].status == "ok"
    payload["jobs"][0].pop("id")
    second = run_sources([source], adapters, pg_store, JobFilter(FILTERS), recorder)
    assert second[0].status == "error"
    failed_run = pg_store.db.query(
        "select * from public.source_runs where status='error'"
    )[0]
    assert failed_run["jobs_found"] == 2
    assert all(
        row["closed_at"] is None
        for row in pg_store.db.query("select closed_at from public.jobs")
    )


def test_load_sources_requires_confirmed_and_preserves_disabled(
    tmp_path: Path, make_http: Any
) -> None:
    path = tmp_path / "companies.yaml"
    base = {
        "domain": "example.com",
        "aliases": [],
        "category": "fintech",
        "country": "NG",
        "enabled": True,
        "detection_status": "confirmed",
    }
    entries = [
        {**base, "name": "Yes", "ats": "greenhouse", "ats_slug": "yes"},
        {
            **base,
            "name": "Disabled",
            "ats": "lever",
            "ats_slug": "disabled",
            "enabled": False,
        },
        {
            **base,
            "name": "Review",
            "ats": "ashby",
            "ats_slug": "review",
            "detection_status": "needs_review",
        },
    ]
    path.write_text(json.dumps({"companies": entries}), encoding="utf-8")
    adapters = {
        a.name: a for a in create_adapters(make_http(lambda _: httpx.Response(404)))
    }
    sources = load_sources(path, adapters)
    assert len(sources) == 2 and sources[0].enabled and not sources[1].enabled


def test_cli_no_confirmed_companies_does_not_need_secrets(
    tmp_path: Path, capsys: Any
) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text('{"companies": []}', encoding="utf-8")
    assert main(["--companies-file", str(path)]) == 0
    assert "No confirmed ATS entries" in capsys.readouterr().out


def test_workable_locations_merge_before_filter_and_storage(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    source = SourceConfig("FairMoney", "workable", "fairmoney", "https://example.com")
    payload = {"name": "FairMoney", "jobs": recorded["workable-location-variants"]}
    http = make_http(lambda _: httpx.Response(200, json=payload))
    adapters = {a.name: a for a in create_adapters(http)}
    recorder = RunRecorder(tmp_path / "runs")
    first = run_sources([source], adapters, pg_store, JobFilter(FILTERS), recorder)
    second = run_sources([source], adapters, pg_store, JobFilter(FILTERS), recorder)
    assert first[0].status == second[0].status == "ok"
    assert first[0].counts.jobs_found == 2 and first[0].counts.inserted_jobs == 2
    assert first[0].counts.new_jobs == second[0].counts.new_jobs == 0
    assert second[0].counts.updated_jobs == second[0].counts.inserted_jobs == 0
    rows = pg_store.db.query("select external_id, location_raw from public.jobs")
    assert len(rows) == 2
    merged = next(row for row in rows if row["external_id"] == "F297960A25")
    assert "Abuja" in merged["location_raw"] and "Lagos" in merged["location_raw"]


def test_cli_twice_against_real_sql(
    recorded: dict[str, Any],
    make_http: Any,
    pg_store: PostgresStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        json.dumps(
            {
                "companies": [
                    {
                        "name": "Moniepoint",
                        "domain": "moniepoint.com",
                        "aliases": [],
                        "category": "fintech",
                        "country": "NG",
                        "enabled": True,
                        "ats": "greenhouse",
                        "ats_slug": "moniepoint",
                        "detection_status": "confirmed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = deepcopy(recorded["greenhouse-content"])
    payload["meta"]["total"] = len(payload["jobs"])
    monkeypatch.setattr(
        run_fetch,
        "HTTPClient",
        lambda: make_http(lambda _: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(run_fetch.SupabaseStore, "from_environment", lambda _: pg_store)
    args = [
        "--companies-file",
        str(companies),
        "--filters-file",
        str(FILTERS),
        "--report-file",
        str(tmp_path / "runs"),
    ]
    assert main(args) == 0
    first = capsys.readouterr().out
    assert "New notifiable jobs: 0" in first and "True" in first
    assert main(args) == 0
    second = capsys.readouterr().out
    assert "New notifiable jobs: 0" in second and "False" in second
    assert len(pg_store.db.query("select * from public.jobs")) == 2
