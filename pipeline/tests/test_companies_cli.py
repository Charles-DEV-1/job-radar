from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import detect_ats
from pipeline.companies import CompanyFile
from pipeline.models import DetectionResult

TEMPLATE = """# keep top comment
companies:
  # keep entry comment
  - name: "FairMoney"
    aliases: [Fair Money]
    domain: fairmoney.io # official domain
    careers_url: null
    category: fintech
    country: NG
    enabled: true
    ats: unknown # detector field
    ats_slug: null
    detection_status: needs_review
"""


def test_write_preserves_comments_order_and_other_fields(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(TEMPLATE, encoding="utf-8")
    source = CompanyFile(path)
    source.write(
        [
            DetectionResult(
                source.companies[0], "workable", "fairmoney", "confirmed", 0, "matched"
            )
        ]
    )
    output = path.read_text(encoding="utf-8")
    for text in (
        "# keep top comment",
        "# keep entry comment",
        "# official domain",
        "# detector field",
        '"FairMoney"',
    ):
        assert text in output
    assert output.index("name:") < output.index("domain:") < output.index("ats:")
    assert "ats: workable" in output
    assert "ats_slug: fairmoney" in output
    assert "detection_status: confirmed" in output
    assert CompanyFile(path).companies == source.companies
    source.write(
        [
            DetectionResult(
                source.companies[0], "workable", "fairmoney", "confirmed", 0, "matched"
            )
        ]
    )
    assert path.read_text(encoding="utf-8") == output


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("domain: fairmoney.io", "domain: ''"),
        ("domain: fairmoney.io", "domain: https://fairmoney.io"),
        ("enabled: true", "enabled: 'true'"),
        ("category: fintech", "category: invalid"),
        ("country: NG", "country: Nigeria"),
        ("aliases: [Fair Money]", "aliases: Fair Money"),
    ],
)
def test_invalid_company_input(tmp_path: Path, before: str, after: str) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(TEMPLATE.replace(before, after), encoding="utf-8")
    with pytest.raises(ValueError):
        CompanyFile(path)


def test_cli_read_only_then_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, make_http: Any
) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(TEMPLATE, encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "fairmoney.io":
            return httpx.Response(
                200, text='<a href="https://apply.workable.com/fairmoney/">Jobs</a>'
            )
        if request.url.host == "apply.workable.com":
            return httpx.Response(200, json={"name": "FairMoney", "jobs": []})
        return httpx.Response(404)

    monkeypatch.setattr(detect_ats, "HTTPClient", lambda: make_http(handler))
    args = [
        "--companies-file",
        str(path),
        "--report-file",
        str(tmp_path / "report"),
        "--company",
        "Fair Money",
    ]
    assert detect_ats.main(args) == 0
    assert path.read_text(encoding="utf-8") == TEMPLATE
    assert "0 open jobs (empty, verify)" in capsys.readouterr().out
    assert detect_ats.main([*args, "--write"]) == 0
    assert "ats: workable" in path.read_text(encoding="utf-8")


def test_disabled_examples_do_not_make_requests(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text(
        TEMPLATE.replace("enabled: true", "enabled: false"), encoding="utf-8"
    )
    assert detect_ats.main(["--companies-file", str(path)]) == 0
    assert "No enabled companies" in capsys.readouterr().out


def test_malformed_yaml_has_clear_failure(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("companies: [broken", encoding="utf-8")
    assert detect_ats.main(["--companies-file", str(path)]) == 2
    assert "ATS detection failed" in capsys.readouterr().err


def test_review_report_shows_actual_board_identity(company: Any, capsys: Any) -> None:
    from pipeline.models import ProbeResult
    from pipeline.verification import verify_match

    probe = ProbeResult(
        "lever",
        "collision",
        "found",
        job_count=2,
        company_names=["Wrong Company"],
        sample_titles=["Backend Engineer"],
        sample_apply_urls=["https://jobs.lever.co/collision/id"],
    )
    match = verify_match(company, probe)
    detect_ats.print_report(
        [
            DetectionResult(
                company, "lever", "collision", "needs_review", 2, match.reason, [match]
            )
        ]
    )
    output = capsys.readouterr().out
    for expected in (
        "Needs your review",
        "Wrong Company",
        "Backend Engineer",
        "https://jobs.lever.co/collision/id",
    ):
        assert expected in output
