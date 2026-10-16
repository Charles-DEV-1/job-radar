import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline.detect_ats import print_report
from pipeline.detector import Detector, RunRecorder
from pipeline.discovery import CareersDiscovery, known_ats_url, trusted_careers_url
from pipeline.models import Company, ProbeResult
from pipeline.sources.ats import create_adapters
from pipeline.sources.ats.workable import WorkableAdapter
from pipeline.sources.pages import permitted_domain
from pipeline.verification import verify_match


@pytest.mark.parametrize("empty_first", [True, False])
def test_all_providers_probed_and_active_board_wins(
    company: Company,
    recorded: dict[str, Any],
    make_http: Any,
    tmp_path: Path,
    empty_first: bool,
    capsys: Any,
) -> None:
    entry = replace(
        company,
        name="Moniepoint",
        aliases=(),
        domain="moniepoint.com",
        careers_url=(
            "https://apply.workable.com/moniepoint/"
            if empty_first
            else "https://job-boards.eu.greenhouse.io/moniepoint"
        ),
    )
    # Controlled identity change to the recorded empty Workable response, paired
    # with the recorded live Moniepoint Greenhouse response (see fixture notes).
    empty = deepcopy(recorded["detection-20261012"]["paystack_workable"])
    empty["name"] = "Moniepoint"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == entry.domain and request.url.path != "/robots.txt":
            return httpx.Response(
                200,
                text=(
                    '<a href="https://job-boards.eu.greenhouse.io/moniepoint">Jobs</a>'
                    if empty_first
                    else '<a href="https://apply.workable.com/moniepoint/">Jobs</a>'
                ),
            )
        if request.url.host == "apply.workable.com":
            return httpx.Response(200, json=empty)
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json=recorded["greenhouse"])
        return httpx.Response(404)

    http = make_http(handler)
    adapters = create_adapters(http)
    adapters.sort(key=lambda a: a.name != ("workable" if empty_first else "greenhouse"))
    report = tmp_path / "runs"
    result = Detector(adapters, CareersDiscovery(http), RunRecorder(report)).detect(
        entry
    )
    assert result.ats == "greenhouse" and result.detection_status == "confirmed"
    assert result.job_count == len(recorded["greenhouse"]["jobs"]) > 0
    assert result.errors == []
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    probes = [row for row in rows if row["event"] == "probe"]
    assert {row["ats"] for row in probes} == {a.name for a in adapters}
    assert len(probes) == len({(row["ats"], row["slug"]) for row in probes})
    assert probes[0]["ats"] == ("workable" if empty_first else "greenhouse")
    empty_matches = [m for m in result.matches if m.probe.ats == "workable"]
    expected_reason = "empty, name match only" if empty_first else "empty, verify"
    assert empty_matches and all(expected_reason in m.reason for m in empty_matches)
    assert any(expected_reason in m["reason"] for m in rows[-1]["matches"])
    print_report([result])
    assert expected_reason in capsys.readouterr().out


def test_empty_board_does_not_stop_other_slugs_in_same_provider(
    company: Company, recorded: dict[str, Any], make_http: Any, tmp_path: Path
) -> None:
    entry = replace(company, careers_url="https://apply.workable.com/old-board/")
    empty = deepcopy(recorded["workable"])
    empty["jobs"] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "apply.workable.com":
            return httpx.Response(
                200,
                json=empty
                if request.url.path.endswith("old-board")
                else recorded["workable"],
            )
        return httpx.Response(
            404 if request.url.path == "/robots.txt" else 200, text="<html></html>"
        )

    http = make_http(handler)
    result = Detector(
        [WorkableAdapter(http)], CareersDiscovery(http), RunRecorder(tmp_path / "runs")
    ).detect(entry)
    assert result.ats_slug == "fairmoney" and result.job_count > 0


def test_smartrecruiters_missing_slug_empty_200_is_not_found(
    company: Company, recorded: dict[str, Any], make_http: Any, tmp_path: Path
) -> None:
    entry = replace(
        company,
        careers_url="https://careers.smartrecruiters.com/jobradar-missing-company-20261012",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.smartrecruiters.com":
            return httpx.Response(
                200, json=recorded["detection-20261012"]["smartrecruiters_missing"]
            )
        if request.url.host == company.domain and request.url.path != "/robots.txt":
            return httpx.Response(200, text="<html>No careers links</html>")
        return httpx.Response(404)

    http = make_http(handler)
    report = tmp_path / "runs"
    result = Detector(
        create_adapters(http), CareersDiscovery(http), RunRecorder(report)
    ).detect(entry)
    assert result.detection_status == "not_found" and result.ats == "none"
    assert result.errors == []
    assert result.matches and all(m.status == "not_found" for m in result.matches)
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    probes = [
        r for r in rows if r["event"] == "probe" and r["ats"] == "smartrecruiters"
    ]
    assert probes and all(r["status"] == "not_found" and not r["error"] for r in probes)


@pytest.mark.parametrize(
    "identity",
    [{}, {"company_names": ["Wrong Company"]}, {"company_domains": ["wrong.com"]}],
)
def test_empty_unverified_boards_never_confirm(
    company: Company, identity: dict[str, Any]
) -> None:
    entry = replace(company, careers_url="https://apply.workable.com/fairmoney/")
    probe = ProbeResult("workable", "fairmoney", "found", job_count=0, **identity)
    for evidence in (None, "https://fairmoney.io/careers", entry.careers_url):
        expected = (
            "confirmed"
            if not identity and evidence == "https://fairmoney.io/careers"
            else "not_found"
        )
        assert verify_match(entry, probe, evidence).status == expected


@pytest.mark.parametrize(
    "alias", ["PiggyTech Global Limited", "PiggyTech Global Limited (PiggyVest)"]
)
def test_recorded_parenthesized_company_name_matches_alias(
    company: Company, recorded: dict[str, Any], make_http: Any, alias: str
) -> None:
    entry = replace(company, name="PiggyVest", aliases=(alias,), domain="piggyvest.com")
    http = make_http(
        lambda _: httpx.Response(
            200, json=recorded["detection-20261012"]["piggyvest_workable"]
        )
    )
    match = verify_match(entry, WorkableAdapter(http).probe("piggyvest"))
    assert match.status == "needs_review" and match.reason == "empty, name match only"
    match.probe.job_count = 1
    assert verify_match(entry, match.probe).status == "confirmed"
    wrong = ProbeResult(
        "workable",
        "piggyvest",
        "found",
        job_count=1,
        company_names=["Unrelated Company (PiggyVest)"],
    )
    assert verify_match(entry, wrong).status == "needs_review"


@pytest.mark.parametrize(
    "url",
    [
        "https://cowrywise.breezy.hr/",
        "https://apply.workable.com/piggyvest/",
        "https://job-boards.greenhouse.io/paystack",
        "https://jobs.lever.co/brand",
        "https://jobs.ashbyhq.com/brand",
    ],
)
def test_explicit_known_ats_urls_are_allowed(company: Company, url: str) -> None:
    entry = replace(company, careers_url=url)
    assert known_ats_url(url) and trusted_careers_url(entry, url)
    assert permitted_domain(url, company.domain)
    assert not trusted_careers_url(company, url)


@pytest.mark.parametrize(
    "url",
    [
        "https://cowrywise.breezy.hr.evil.com/",
        "https://notworkable.com/brand",
        "https://unknown.example/brand",
    ],
)
def test_explicit_unknown_or_lookalike_host_is_rejected(
    company: Company, url: str
) -> None:
    assert not trusted_careers_url(replace(company, careers_url=url), url)


def test_trust_stays_with_configured_board_path(company: Company) -> None:
    entry = replace(company, careers_url="https://apply.workable.com/fairmoney/")
    assert trusted_careers_url(entry, entry.careers_url + "jobs")
    assert not trusted_careers_url(entry, "https://apply.workable.com/other-board/")
    assert not trusted_careers_url(entry, "https://apply.workable.com/fairmoney-evil/")


@pytest.mark.parametrize("disallow", [False, True])
def test_explicit_breezy_page_is_checked_with_robots(
    company: Company, recorded: dict[str, Any], make_http: Any, disallow: bool
) -> None:
    entry = replace(
        company,
        name="Cowrywise",
        domain="cowrywise.com",
        careers_url="https://cowrywise.breezy.hr/",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return (
                httpx.Response(200, text="User-agent: *\nDisallow: /\n")
                if disallow
                else httpx.Response(404)
            )
        if request.url.host == "cowrywise.breezy.hr":
            return httpx.Response(
                200, text=recorded["detection-20261012"]["cowrywise_breezy"]
            )
        return httpx.Response(200, text="<html></html>")

    result = CareersDiscovery(make_http(handler)).discover(entry)
    assert "https://cowrywise.breezy.hr/robots.txt" in calls
    if disallow:
        assert entry.careers_url not in calls
        assert any("robots.txt disallows" in error for error in result.errors)
    else:
        assert entry.careers_url in calls and result.errors == []
        assert result.links == []  # No unverified Breezy API adapter is introduced.


def test_configured_live_board_without_metadata_is_confirmed(company: Company) -> None:
    url = "https://job-boards.greenhouse.io/paystack"
    entry = replace(company, careers_url=url)
    probe = ProbeResult("greenhouse", "paystack", "found", job_count=2)
    assert verify_match(entry, probe, url).status == "confirmed"
    assert verify_match(company, probe, url).status == "needs_review"
