"""Empty-board provenance, Lever ownership and careers fallback regressions."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline.companies import CompanyFile
from pipeline.detect_ats import print_report
from pipeline.detector import Detector, RunRecorder
from pipeline.discovery import CareersDiscovery
from pipeline.models import Company, DetectionResult, ProbeResult
from pipeline.run_fetch import load_sources
from pipeline.sources.ats.lever import LeverAdapter
from pipeline.sources.ats.workable import WorkableAdapter
from pipeline.verification import verify_match


@pytest.mark.parametrize("evidence", [None, "https://apply.workable.com/fairmoney/"])
def test_empty_name_match_requires_official_page_link(
    company: Company, evidence: str | None
) -> None:
    company = replace(company, careers_url="https://apply.workable.com/fairmoney/")
    probe = ProbeResult("workable", "fairmoney", "found", company_names=["FairMoney"])
    match = verify_match(company, probe, evidence)
    assert match.status == "needs_review"
    assert match.reason == "empty, name match only"
    official = verify_match(company, probe, "https://fairmoney.io/careers")
    assert official.status == "confirmed" and "empty, verify" in official.reason


def test_empty_domain_match_alone_is_not_confirmed(company: Company) -> None:
    probe = ProbeResult(
        "workable", "fairmoney", "found", company_domains=["fairmoney.io"]
    )
    assert verify_match(company, probe).status == "needs_review"


@pytest.mark.parametrize("status", [200, 404])
def test_official_link_confirms_only_successfully_returned_empty_collection(
    company: Company, make_http: Any, tmp_path: Path, status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co":
            return httpx.Response(status, json=[])
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text='<a href="https://jobs.lever.co/fairmoney">Careers</a>'
        )

    http = make_http(handler)
    result = Detector(
        [LeverAdapter(http)], CareersDiscovery(http), RunRecorder(tmp_path / "run")
    ).detect(company)
    if status == 200:
        assert result.detection_status == "confirmed" and result.job_count == 0
        assert "empty, verify" in result.reason
    else:
        assert result.detection_status == "needs_review" and result.errors


def test_recorded_tala_lever_confirms_without_official_page_proof(
    company: Company, recorded: dict[str, Any], make_http: Any, tmp_path: Path
) -> None:
    tala = replace(company, name="Tala", aliases=(), domain="tala.co")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=recorded["lever-tala"])
        return httpx.Response(404)

    http = make_http(handler)
    report = tmp_path / "run"
    result = Detector(
        [LeverAdapter(http)], CareersDiscovery(http), RunRecorder(report)
    ).detect(tala)
    assert result.detection_status == "confirmed" and result.job_count == 1
    assert "Lever posting" in result.reason
    assert not result.matches[0].probe.company_names
    log = report.read_text(encoding="utf-8")
    assert "job_identity_texts" not in log
    assert recorded["lever-tala"][0]["descriptionPlain"] not in log


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ("Tala is hiring engineers.", "confirmed"),
        ("Read https://careers.tala.co/jobs for details.", "confirmed"),
        ("Talaria is hiring engineers.", "needs_review"),
        ("A different lender is hiring engineers.", "needs_review"),
    ],
)
def test_lever_checks_every_posting_without_slug_guess_confirmation(
    company: Company, make_http: Any, evidence: str, expected: str
) -> None:
    company = replace(company, name="Tala", aliases=(), domain="tala.co")
    jobs = [
        {"text": "Engineer", "applyUrl": f"https://jobs.lever.co/other/{i}/apply"}
        for i in range(4)
    ]
    jobs[-1]["descriptionPlain"] = evidence
    probe = LeverAdapter(make_http(lambda _: httpx.Response(200, json=jobs))).probe(
        "tala"
    )
    match = verify_match(company, probe)
    assert match.status == expected
    assert verify_match(company, probe, "https://tala.co/careers").status == expected
    assert probe.sample_apply_urls and len(probe.sample_apply_urls) == 3


def test_lever_posting_apply_url_can_supply_ownership(
    company: Company, make_http: Any
) -> None:
    jobs = [
        {"text": "Engineer", "applyUrl": "https://jobs.lever.co/fairmoney/id/apply"}
    ]
    probe = LeverAdapter(make_http(lambda _: httpx.Response(200, json=jobs))).probe(
        "fairmoney"
    )
    assert verify_match(company, probe).status == "confirmed"


@pytest.mark.parametrize(
    "status,count,careers,expected",
    [
        ("confirmed", 8, True, ["workable"]),
        ("confirmed", 0, True, ["careers_page", "workable"]),
        ("confirmed", None, True, ["careers_page", "workable"]),
        ("needs_review", 0, True, ["careers_page"]),
        ("needs_review", 8, True, ["careers_page"]),
        ("not_found", 0, True, ["careers_page"]),
        ("needs_review", None, False, []),
    ],
)
def test_source_builder_keeps_careers_coverage(
    make_http: Any,
    tmp_path: Path,
    status: str,
    count: int | None,
    careers: bool,
    expected: list[str],
) -> None:
    entry = {
        "name": "FairMoney",
        "domain": "fairmoney.io",
        "category": "fintech",
        "country": "NG",
        "enabled": True,
        "careers_url": "https://fairmoney.io/careers" if careers else None,
        "ats": "workable",
        "ats_slug": "fairmoney",
        "detection_status": status,
        "ats_job_count": count,
    }
    path = tmp_path / "companies.yaml"
    path.write_text(json.dumps({"companies": [entry]}))
    adapters = {"workable": WorkableAdapter(make_http(lambda _: httpx.Response(404)))}
    sources = load_sources(path, adapters)
    assert [source.ats for source in sources] == expected
    assert load_sources(path, adapters) == sources


def test_company_writer_persists_job_count_and_preserves_user_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text("""# owner comment
companies:
  - name: Tala
    aliases: []
    domain: tala.co
    careers_url: null
    category: fintech
    country: KE
    enabled: true
    owner_note: keep this
""")
    config = CompanyFile(path)
    company = config.companies[0]
    config.write(
        [DetectionResult(company, "lever", "tala", "confirmed", 10, "verified")]
    )
    saved = CompanyFile(path).document["companies"][0]
    assert saved["ats_job_count"] == 10 and saved["owner_note"] == "keep this"
    assert "# owner comment" in path.read_text()


def test_detection_report_prints_four_category_summary(
    company: Company, capsys: Any
) -> None:
    results = [
        DetectionResult(company, "lever", "one", "confirmed", 2, "live"),
        DetectionResult(company, "workable", "two", "confirmed", 0, "official"),
        DetectionResult(
            company, "workable", "three", "needs_review", 0, "empty, name match only"
        ),
        DetectionResult(company, "none", None, "not_found", None, "missing"),
    ]
    print_report(results)
    summary = capsys.readouterr().out.splitlines()[-1]
    assert summary == (
        "Detection summary: confirmed with jobs: 1; confirmed but empty: 1; "
        "needs review: 1; not found: 1"
    )


@pytest.mark.parametrize("count", [-1, True, "10", 1.5])
def test_invalid_saved_counts_are_rejected(tmp_path: Path, count: Any) -> None:
    entry = {
        "name": "Tala",
        "domain": "tala.co",
        "category": "fintech",
        "country": "KE",
        "enabled": True,
        "ats_job_count": count,
    }
    path = tmp_path / "companies.yaml"
    path.write_text(json.dumps({"companies": [entry]}))
    with pytest.raises(ValueError, match="ats_job_count"):
        CompanyFile(path)


def test_lever_review_report_retains_actual_sample_urls(
    company: Company, capsys: Any
) -> None:
    url = "https://jobs.lever.co/unrelated/job-id/apply"
    probe = ProbeResult(
        "lever", "fairmoney", "found", job_count=1, sample_apply_urls=[url]
    )
    match = verify_match(company, probe)
    assert match.status == "needs_review"
    print_report(
        [
            DetectionResult(
                company,
                "lever",
                "fairmoney",
                match.status,
                1,
                match.reason,
                matches=[match],
            )
        ]
    )
    assert "Sample apply URLs: " + url in capsys.readouterr().out


@pytest.mark.parametrize(
    "domain", ["https://careers.tala.co/jobs", "https://tala.co.evil.test/jobs"]
)
def test_lever_domain_evidence_uses_hostname_boundaries(
    company: Company, domain: str
) -> None:
    entry = replace(company, name="Independent Brand", aliases=(), domain="tala.co")
    probe = ProbeResult(
        "lever", "arbitrary", "found", job_count=1, job_identity_texts=[domain]
    )
    expected = (
        "confirmed" if domain == "https://careers.tala.co/jobs" else "needs_review"
    )
    assert verify_match(entry, probe).status == expected
