import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from pipeline.detector import Detector, RunRecorder
from pipeline.discovery import CareersDiscovery
from pipeline.models import Company
from pipeline.sources.ats import create_adapters
from pipeline.sources.ats.workable import WorkableAdapter


def test_failing_source_does_not_stop_another(
    company: Company, recorded: dict[str, Any], make_http: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fairmoney.io":
            return httpx.Response(404)
        if request.url.host == "boards-api.greenhouse.io":
            raise httpx.ReadTimeout("timeout", request=request)
        if request.url.host == "apply.workable.com":
            return httpx.Response(200, json=recorded["workable"])
        return httpx.Response(404)

    http = make_http(handler)
    report = tmp_path / "runs.jsonl"
    result = Detector(
        create_adapters(http), CareersDiscovery(http), RunRecorder(report)
    ).detect(company)
    assert result.ats == "workable" and result.detection_status == "confirmed"
    assert result.errors
    records = [
        json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["event"] == "probe" and record["status"] == "error" for record in records
    )
    assert any(
        record["event"] == "probe" and record["status"] == "found" for record in records
    )
    assert "description" not in report.read_text(encoding="utf-8")
    assert records[-1]["event"] == "company"


def test_complete_negatives_are_not_found(
    company: Company, make_http: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fairmoney.io" and request.url.path != "/robots.txt":
            return httpx.Response(200, text="<html>No careers links yet</html>")
        return httpx.Response(404)

    http = make_http(handler)
    result = Detector(
        create_adapters(http), CareersDiscovery(http), RunRecorder(tmp_path / "runs")
    ).detect(company)
    assert result.ats == "none" and result.detection_status == "not_found"


def test_errors_never_get_written_as_no_board(
    company: Company, make_http: Any, tmp_path: Path
) -> None:
    http = make_http(lambda _: httpx.Response(503))
    result = Detector(
        create_adapters(http), CareersDiscovery(http), RunRecorder(tmp_path / "runs")
    ).detect(company)
    assert result.ats == "unknown" and result.detection_status == "needs_review"
    assert result.errors


def test_discovered_link_with_unavailable_api_needs_review(
    company: Company, make_http: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fairmoney.io" and request.url.path != "/robots.txt":
            return httpx.Response(
                200, text='<a href="https://jobs.eu.lever.co/brand">Jobs</a>'
            )
        return httpx.Response(404)

    http = make_http(handler)
    result = Detector(
        create_adapters(http), CareersDiscovery(http), RunRecorder(tmp_path / "runs")
    ).detect(company)
    assert result.ats == "unknown" and result.detection_status == "needs_review"
    assert any("official ATS link found" in error for error in result.errors)


def test_wrong_board_is_reviewed_then_correct_board_can_win(
    company: Company, recorded: dict[str, Any], make_http: Any, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fairmoney.io":
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, text="<html></html>")
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json=recorded["greenhouse"])
        if request.url.host == "apply.workable.com":
            return httpx.Response(200, json=recorded["workable"])
        return httpx.Response(404)

    http = make_http(handler)
    result = Detector(
        create_adapters(http), CareersDiscovery(http), RunRecorder(tmp_path / "runs")
    ).detect(company)
    assert result.ats == "workable" and result.detection_status == "confirmed"
    assert any(
        match.probe.ats == "greenhouse" and match.status == "needs_review"
        for match in result.matches
    )


def test_unexpected_adapter_bug_is_recorded_and_continues(
    company: Company, recorded: dict[str, Any], make_http: Any, tmp_path: Path
) -> None:
    http = make_http(
        lambda request: (
            httpx.Response(200, json=recorded["workable"])
            if request.url.host == "apply.workable.com"
            else httpx.Response(404)
        )
    )
    adapters = create_adapters(http)

    def buggy(_: str) -> Any:
        raise RuntimeError("adapter bug")

    adapters[0].probe = buggy  # type: ignore[method-assign]
    result = Detector(
        adapters, CareersDiscovery(http), RunRecorder(tmp_path / "runs")
    ).detect(company)
    assert result.detection_status == "confirmed"
    assert any("Unexpected RuntimeError" in error for error in result.errors)


def test_official_empty_board_is_kept_and_probe_is_cached(
    company: Company, make_http: Any, tmp_path: Path
) -> None:
    probes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "apply.workable.com":
            probes.append(str(request.url))
            return httpx.Response(200, json={"jobs": []})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text='<a href="https://apply.workable.com/fairmoney/">Jobs</a>'
        )

    http = make_http(handler)
    report = tmp_path / "runs"
    detector = Detector(
        [WorkableAdapter(http)], CareersDiscovery(http), RunRecorder(report)
    )
    first = detector.detect(company)
    second = detector.detect(replace(company, aliases=("FM",)))
    assert first.job_count == second.job_count == 0
    assert first.detection_status == second.detection_status == "confirmed"
    assert len(probes) == 1
    assert '"cached": true' in report.read_text(encoding="utf-8")
