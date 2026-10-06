from dataclasses import replace
from typing import Any

import httpx
import pytest

from pipeline.discovery import CareersDiscovery, page_links, parse_board_url
from pipeline.models import Company


@pytest.mark.parametrize(
    ("url", "ats", "slug"),
    [
        (
            "https://job-boards.eu.greenhouse.io/moniepoint/jobs/1",
            "greenhouse",
            "moniepoint",
        ),
        (
            "https://boards.greenhouse.io/embed/job_board?for=brand",
            "greenhouse",
            "brand",
        ),
        ("https://jobs.lever.co/brand/123", "lever", "brand"),
        ("https://jobs.ashbyhq.com/m-kopa?embed=js", "ashby", "m-kopa"),
        ("https://apply.workable.com/brand/", "workable", "brand"),
        ("https://brand.workable.com/", "workable", "brand"),
        ("https://brand.recruitee.com/o/title", "recruitee", "brand"),
        (
            "https://careers.smartrecruiters.com/StandardBankGroup",
            "smartrecruiters",
            "StandardBankGroup",
        ),
    ],
)
def test_board_url_patterns(url: str, ats: str, slug: str) -> None:
    link = parse_board_url(url)
    assert link and link.ats == ats and link.slug == slug


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co.evil.com/brand",
        "https://notrecruitee.com/brand",
        "https://apply.workable.com/j/ABCD/",
        "https://jobs.ashbyhq.com/a%2Fb",
    ],
)
def test_false_ats_links_are_rejected(url: str) -> None:
    assert parse_board_url(url) is None


def test_embeds_escaped_urls_and_careers_links() -> None:
    html = """<a href="/careers">Join us</a>
    <iframe src="https://jobs.lever.co/brand"></iframe>
    <script>const board = "https:\\/\\/jobs.ashbyhq.com\\/brand";</script>
    <!-- <a href="https://jobs.lever.co/wrong">old link</a> -->"""
    boards, pages = page_links(html, "https://brand.example/")
    assert [(board.ats, board.slug) for board in boards] == [
        ("lever", "brand"),
        ("ashby", "brand"),
    ]
    assert pages == ["https://brand.example/careers"]


def test_follow_official_careers_links_and_cache_robots(
    company: Company, make_http: Any
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if request.url.path == "/":
            return httpx.Response(200, text='<a href="/careers">Careers</a>')
        return httpx.Response(
            200, text='<a href="https://apply.workable.com/fairmoney/">Jobs</a>'
        )

    result = CareersDiscovery(make_http(handler)).discover(company)
    assert paths == ["/robots.txt", "/", "/careers"]
    assert result.links[0].evidence_url == "https://fairmoney.io/careers"
    assert result.errors == []


def test_robots_denial_never_fetches_page(company: Company, make_http: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    result = CareersDiscovery(make_http(handler)).discover(company)
    assert result.links == []
    assert "disallows" in result.errors[0]


def test_robots_failure_is_not_permission(company: Company, make_http: Any) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(503)

    result = CareersDiscovery(make_http(handler)).discover(company)
    assert paths == ["/robots.txt"]
    assert result.errors


def test_redirect_checks_destination_robots(company: Company, make_http: Any) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "careers.fairmoney.io":
            assert request.url.path == "/robots.txt"
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            302, headers={"location": "https://careers.fairmoney.io/jobs"}
        )

    result = CareersDiscovery(make_http(handler)).discover(company)
    assert result.errors
    assert calls[-1] == "https://careers.fairmoney.io/robots.txt"
    assert "https://careers.fairmoney.io/jobs" not in calls


def test_official_redirect_to_ats_is_evidence(company: Company, make_http: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            302, headers={"location": "https://jobs.ashbyhq.com/brand"}
        )

    result = CareersDiscovery(make_http(handler)).discover(company)
    assert result.links[0].evidence_url == "https://fairmoney.io/"


def test_configured_external_board_is_not_official_evidence(
    company: Company, make_http: Any
) -> None:
    entry = replace(company, careers_url="https://jobs.lever.co/brand")
    result = CareersDiscovery(make_http(lambda _: httpx.Response(404))).discover(entry)
    assert result.links[0].evidence_url is None


def test_robots_crawl_delay_is_respected(company: Company, make_http: Any) -> None:
    http = make_http(
        lambda _: httpx.Response(200, text="User-agent: *\nCrawl-delay: 5\n")
    )
    CareersDiscovery(http).discover(company)
    assert http.host_intervals["fairmoney.io"] == 5
