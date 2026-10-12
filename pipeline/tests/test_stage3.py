"""Recorded provider facts replayed through controlled page mutations and real SQL."""

import gzip
import hashlib
import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from postgres_helpers import PostgresStore
from test_filters import FILTERS

from pipeline.detector import RunRecorder
from pipeline.filters import JobFilter
from pipeline.llm import client as llm_module
from pipeline.llm.client import ExtractionClient, Posting, Reply, Usage
from pipeline.models import SourceConfig
from pipeline.robots import RobotsPolicy
from pipeline.run_fetch import load_sources, run_sources
from pipeline.sources.ats import create_adapters
from pipeline.sources.careers_page import CareersPageAdapter
from pipeline.sources.feed import FeedAdapter, load_feeds, parse_feed
from pipeline.sources.pages import (
    PageFetcher,
    bounded,
    clean_text,
    permitted_domain,
    snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = SourceConfig(
    "FairMoney",
    "careers_page",
    "fairmoney-page",
    "https://fairmoney.io/careers",
    True,
    "fairmoney.io",
    "careers_page",
)


class RecordedBackend:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def complete(self, system: str, content: str) -> Reply:
        assert "untrusted" in system
        self.calls.append(content)
        return Reply(self.responses.pop(0), 100, 30)


def setup_page(
    recorded: dict[str, Any], make_http: Any, responses: list[list[dict[str, Any]]]
) -> tuple[CareersPageAdapter, dict[str, str], RecordedBackend]:
    fixture = recorded["stage3-pages"]
    current = {
        "html": fixture["base"],
        "robots": fixture["robots_allow"],
        "detail": fixture["base"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "JobRadar" in request.headers["User-Agent"]
        assert "Authorization" not in request.headers
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=current["robots"])
        return httpx.Response(
            200,
            text=current["html"]
            if request.url.host == "fairmoney.io"
            else current["detail"],
        )

    backend = RecordedBackend([json.dumps(r) for r in responses])
    llm = ExtractionClient(backend, 2, 4, sleep=lambda _: None)
    pages = PageFetcher(
        make_http(handler), renderer=lambda url, _: (current["html"], url)
    )
    return CareersPageAdapter(pages, llm), current, backend


def run_page(adapter: CareersPageAdapter, store: PostgresStore, tmp_path: Path) -> Any:
    return run_sources(
        [SOURCE],
        {"careers_page": adapter},
        store,
        JobFilter(FILTERS),
        RunRecorder(tmp_path / "runs.jsonl"),
        1,
    )[0]


def test_static_change_backfill_diff_new_job_and_replay(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    fixture = recorded["stage3-pages"]
    adapter, current, backend = setup_page(
        recorded, make_http, [fixture["postings"][:1], fixture["postings"][1:]]
    )
    first = run_page(adapter, pg_store, tmp_path)
    assert first.status == "ok" and first.counts.backfill
    assert first.counts.inserted_jobs == 1 and first.counts.new_jobs == 0
    second = run_page(adapter, pg_store, tmp_path)
    assert second.status == "ok" and second.counts.new_jobs == 0
    assert len(backend.calls) == 1
    current["html"] = fixture["changed"]
    added = run_page(adapter, pg_store, tmp_path)
    assert added.status == "ok", added.error
    assert added.counts.new_jobs == added.counts.inserted_jobs == 1
    assert added.counts.closed_jobs == 0
    assert len(backend.calls) == 2
    payload = json.loads(backend.calls[-1])
    assert "Careers" not in "\n".join(payload["changed_regions"])
    assert fixture["postings"][1]["apply_url"] in backend.calls[-1]
    assert run_page(adapter, pg_store, tmp_path).counts.new_jobs == 0
    assert len(backend.calls) == 2
    rows = pg_store.db.query("select * from public.jobs")
    assert len(rows) == 2 and all(row["source_type"] == "careers_page" for row in rows)
    assert (
        rows[1]["external_id"]
        == hashlib.sha256(rows[1]["apply_url"].encode()).hexdigest()
    )
    runs = pg_store.db.query(
        "select metadata from public.source_runs order by started_at"
    )
    assert runs[0]["metadata"]["llm"]["input_tokens"] == 100
    assert runs[0]["metadata"]["llm"]["cost_usd"] == pytest.approx(0.00032)
    assert runs[1]["metadata"]["llm"]["attempts"] == 0
    log = (tmp_path / "runs.jsonl").read_text()
    assert fixture["postings"][0]["title"] not in log


def test_noise_only_dates_banners_cookies_tracking_and_reorder_skip_llm(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    fixture = recorded["stage3-pages"]
    adapter, current, backend = setup_page(
        recorded, make_http, [fixture["postings"][:1]]
    )
    run_page(adapter, pg_store, tmp_path)
    current["html"] = (
        fixture["base"]
        .replace("2026-10-10", "2026-10-11")
        .replace("Weekly banner", "New promotional banner")
        .replace("Accept cookies", "Please accept cookies")
        .replace("Home About", "Home Products Contact")
        .replace("utm_source=weekly", "utm_source=daily&fbclid=abc")
    )
    outcome = run_page(adapter, pg_store, tmp_path)
    assert outcome.status == "ok" and outcome.counts.new_jobs == 0
    assert len(backend.calls) == 1
    assert (
        snapshot("<main><p>A</p><p>B</p></main>", SOURCE.url).content_hash
        == snapshot("<main><p>B</p><p>A</p></main>", SOURCE.url).content_hash
    )


def test_js_shell_mock_playwright_and_cached_method(
    recorded: dict[str, Any], make_http: Any
) -> None:
    fixture = recorded["stage3-pages"]
    requested: list[str] = []
    rendered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(
            200,
            text=fixture["robots_allow"]
            if request.url.path == "/robots.txt"
            else fixture["shell"],
        )

    def renderer(url: str, deadline: float) -> tuple[str, str]:
        assert deadline > time.monotonic()
        rendered.append(url)
        return fixture["base"], url

    pages = PageFetcher(make_http(handler), renderer=renderer)
    page, method = pages.fetch(SOURCE.url, domain=SOURCE.company_domain)
    assert method == "playwright" and fixture["postings"][0]["title"] in page.text
    pages.fetch(SOURCE.url, method, SOURCE.company_domain)
    assert requested.count("/careers") == 1 and len(rendered) == 2


@pytest.mark.parametrize(
    "mutation", ["fake", "foreign", "empty_title", "relative", "forbidden"]
)
def test_extracted_hallucinated_or_unsafe_urls_are_rejected(
    mutation: str, recorded: dict[str, Any], make_http: Any
) -> None:
    item = deepcopy(recorded["stage3-pages"]["postings"][0])
    urls = {
        "fake": "https://fairmoney.io/invented-job",
        "foreign": "https://attacker.example/job",
        "relative": "/careers",
        "forbidden": "https://www.linkedin.com/jobs/view/123",
    }
    if mutation == "empty_title":
        item["title"] = "   "
    else:
        item["apply_url"] = urls[mutation]
    adapter, current, _ = setup_page(recorded, make_http, [[item]])
    if mutation == "foreign":
        current["html"] += '<a href="https://attacker.example/job">Engineer</a>'
    jobs, _, warnings = adapter.fetch(SOURCE, {}, datetime.now(UTC), Usage())
    assert jobs == [] and len(warnings) == 1


def test_unsafe_detail_redirect_rejected(
    recorded: dict[str, Any], make_http: Any
) -> None:
    fixture = recorded["stage3-pages"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host != "attacker.example"
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=fixture["robots_allow"])
        if request.url.host == "apply.workable.com":
            return httpx.Response(
                302, headers={"Location": "https://attacker.example/job"}
            )
        return httpx.Response(200, text=fixture["base"])

    backend = RecordedBackend([json.dumps(fixture["postings"][:1])])
    adapter = CareersPageAdapter(
        PageFetcher(make_http(handler)), ExtractionClient(backend)
    )
    jobs, _, warnings = adapter.fetch(SOURCE, {}, datetime.now(UTC), Usage())
    assert jobs == [] and "redirect" in warnings[0]


def test_empty_page_failure_recorded_and_baseline_retained(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    fixture = recorded["stage3-pages"]
    adapter, current, backend = setup_page(
        recorded, make_http, [fixture["postings"][:1]]
    )
    run_page(adapter, pg_store, tmp_path)
    source_id = pg_store.db.query("select id from public.sources")[0]["id"]
    before = pg_store.monitor_state(source_id)
    current["html"] = fixture["empty"]
    result = run_page(adapter, pg_store, tmp_path)
    assert result.status == "error" and "empty" in result.error
    assert pg_store.monitor_state(source_id) == before
    assert len(backend.calls) == 1
    row = pg_store.db.query("select * from public.source_runs where status='error'")[0]
    assert "empty" in row["error_message"] and row["metadata"]["llm"]["attempts"] == 0
    assert (
        pg_store.db.query("select closed_at from public.jobs")[0]["closed_at"] is None
    )


def test_shrunken_snapshot_does_not_call_llm(
    recorded: dict[str, Any], make_http: Any
) -> None:
    adapter, current, backend = setup_page(recorded, make_http, [[]])
    current["html"] = "<main>No open positions</main>"
    with pytest.raises(ValueError, match="shrank"):
        adapter.fetch(SOURCE, {"lines": ["A" * 1000]}, datetime.now(UTC), Usage())
    assert backend.calls == []


def test_robots_disallow_never_fetches_page_or_calls_llm(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    paths: list[str] = []
    fixture = recorded["stage3-pages"]

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, text=fixture["robots_disallow"])

    backend = RecordedBackend([])
    adapter = CareersPageAdapter(
        PageFetcher(make_http(handler)), ExtractionClient(backend)
    )
    result = run_page(adapter, pg_store, tmp_path)
    assert result.status == "error" and "robots" in result.error
    assert paths == ["/robots.txt"] and backend.calls == []


def test_http_deadline_and_gzip_decoding(
    make_http: Any, recorded: dict[str, Any]
) -> None:
    fixture = recorded["stage3-pages"]
    client = make_http(
        lambda _: httpx.Response(
            200,
            content=gzip.compress(fixture["base"].encode()),
            headers={"Content-Encoding": "gzip"},
        )
    )
    response = client.get(SOURCE.url, deadline=time.monotonic() + 5)
    assert "Careers" in response.text
    with pytest.raises(TimeoutError):
        client.get(SOURCE.url, deadline=time.monotonic() - 1)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="Total page timeout"):
        bounded(lambda: time.sleep(0.2), started + 0.01)
    assert time.monotonic() - started < 0.15


def test_company_source_selection(tmp_path: Path, make_http: Any) -> None:
    base = {
        "domain": "fairmoney.io",
        "category": "fintech",
        "country": "NG",
        "enabled": True,
        "careers_url": SOURCE.url,
    }
    entries = [
        {**base, "name": "Unknown", "ats": "unknown"},
        {
            **base,
            "name": "Not found",
            "ats": "none",
            "detection_status": "not_found",
            "enabled": False,
        },
        {
            **base,
            "name": "Review ATS",
            "ats": "ashby",
            "detection_status": "needs_review",
        },
        {**base, "name": "No page", "ats": "unknown", "careers_url": None},
    ]
    path = tmp_path / "companies.yaml"
    path.write_text(json.dumps({"companies": entries}))
    adapters = {
        a.name: a for a in create_adapters(make_http(lambda _: httpx.Response(404)))
    }
    result = load_sources(path, adapters)
    assert [s.company_name for s in result] == ["Unknown", "Not found"]
    assert (
        all(s.source_type == "careers_page" for s in result) and not result[1].enabled
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jobs.fairmoney.io/role", True),
        ("https://apply.workable.com/j/a", True),
        ("https://fairmoney.io.attacker.com/role", False),
        ("https://workable.com.evil.net/j/a", False),
        ("https://jobs.example.co.uk/role", False),
        ("https://linkedin.com/jobs/123", False),
    ],
)
def test_registrable_domain_policy(url: str, expected: bool) -> None:
    assert permitted_domain(url, "fairmoney.io") == expected


def test_strict_schema_retry_usage_and_safe_error() -> None:
    backend = RecordedBackend(['{"jobs": []}', "[]"])
    client = ExtractionClient(backend, 1, 1, sleep=lambda _: None)
    usage = Usage()
    assert client.extract("{}", usage) == []
    assert (
        usage.attempts == 2 and usage.input_tokens == 200 and usage.output_tokens == 60
    )
    broken = RecordedBackend(["private description and fake-secret"] * 3)
    with pytest.raises(ValueError) as error:
        ExtractionClient(broken, sleep=lambda _: None).extract("{}", Usage())
    assert "fake-secret" not in str(error.value) and "description" not in str(
        error.value
    )
    with pytest.raises(ValueError):
        Posting.model_validate(
            {
                "title": "Engineer",
                "apply_url": "https://fairmoney.io/a",
                "location": None,
                "description_snippet": None,
                "invented": True,
            }
        )


def test_monitor_sql_atomicity_stale_baseline_replay_and_private_access(
    pg_store: PostgresStore, recorded: dict[str, Any], make_http: Any, tmp_path: Path
) -> None:
    adapter, _, _ = setup_page(recorded, make_http, [[]])
    run_page(adapter, pg_store, tmp_path)
    source_id = pg_store.db.query("select id from public.sources")[0]["id"]
    previous = pg_store.monitor_state(source_id)
    run_id = str(uuid4())
    now = datetime.now(UTC) + timedelta(seconds=1)
    pg_store.start_run(source_id, run_id, now)
    with pytest.raises(RuntimeError, match="baseline changed"):
        pg_store.complete_monitor(run_id, [], now, 1, previous, "wrong", {})
    assert pg_store.monitor_state(source_id) == previous
    assert (
        pg_store.db.query(
            "select completed_at from public.source_runs where id=$1", [run_id]
        )[0]["completed_at"]
        is None
    )
    first = pg_store.complete_monitor(
        run_id, [], now, 1, previous, previous["hash"], {}
    )
    assert pg_store.complete_monitor(run_id, [], now, 1, previous, "wrong", {}) == first
    pg_store.db.query("set role anon")
    with pytest.raises(RuntimeError, match="permission denied"):
        pg_store.db.query("select * from public.source_monitor_state")
    with pytest.raises(RuntimeError, match="permission denied"):
        pg_store.monitor_state(source_id)
    pg_store.db.query("reset role")


@pytest.mark.parametrize("kind", ["rss", "atom", "json"])
def test_generic_feed_with_actual_provider_job_facts(
    kind: str,
    recorded: dict[str, Any],
    make_http: Any,
    pg_store: PostgresStore,
    tmp_path: Path,
) -> None:
    item = recorded["stage3-pages"]["postings"][1]
    if kind == "json":
        body = json.dumps({"jobs": [item]})
    elif kind == "rss":
        body = (
            f"<rss><channel><item><title>{item['title']}</title>"
            f"<link>{item['apply_url']}</link>"
            "<location>Lagos, Nigeria</location></item></channel></rss>"
        )
    else:
        body = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            f"<title>{item['title']}</title>"
            f'<link rel="alternate" href="{item["apply_url"]}"/>'
            "<location>Lagos, Nigeria</location></entry></feed>"
        )
    http = make_http(
        lambda r: httpx.Response(
            200,
            text=recorded["stage3-pages"]["robots_allow"]
            if r.url.path == "/robots.txt"
            else body,
        )
    )
    source = SourceConfig(
        "FairMoney", "feed", kind, SOURCE.url, True, "fairmoney.io", "feed"
    )
    adapter = FeedAdapter(PageFetcher(http))

    def run() -> Any:
        return run_sources(
            [source],
            {"feed": adapter},
            pg_store,
            JobFilter(FILTERS),
            RunRecorder(tmp_path / "runs"),
            1,
        )[0]

    assert run().counts.backfill
    assert run().counts.new_jobs == 0
    assert (
        pg_store.db.query("select source_type from public.jobs")[0]["source_type"]
        == "feed"
    )


@pytest.mark.parametrize(
    "body",
    [
        "<html>Not a feed</html>",
        "{}",
        "[{}]",
        '<!DOCTYPE rss [<!ENTITY a SYSTEM "file:///private">]><rss>&a;</rss>',
    ],
)
def test_invalid_feeds_fail_closed(body: str) -> None:
    with pytest.raises(ValueError):
        rows = parse_feed(body)
        for row in rows:
            Posting.model_validate(row)


def test_feed_enable_requires_verification_and_african_scope(tmp_path: Path) -> None:
    path = tmp_path / "feeds.yaml"
    entry = {
        "name": "Supplied feed",
        "company": "FairMoney",
        "domain": "fairmoney.io",
        "url": SOURCE.url,
        "enabled": True,
        "verified": False,
        "scope": "africa",
    }
    path.write_text(json.dumps({"feeds": [entry]}))
    with pytest.raises(ValueError, match="verified"):
        load_feeds(path)
    entry["verified"] = True
    path.write_text(json.dumps({"feeds": [entry]}))
    assert load_feeds(path)[0].source_type == "feed"


def test_mixed_ats_and_page_failure_isolated(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    payload = deepcopy(recorded["greenhouse-content"])
    payload["meta"]["total"] = len(payload["jobs"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, text=recorded["stage3-pages"]["robots_disallow"])

    http = make_http(handler)
    adapters: dict[str, Any] = {a.name: a for a in create_adapters(http)}
    adapters["careers_page"] = CareersPageAdapter(PageFetcher(http))
    ats = SourceConfig(
        "Moniepoint",
        "greenhouse",
        "moniepoint",
        "https://boards-api.greenhouse.io/v1/boards/moniepoint/jobs",
    )
    outcomes = run_sources(
        [ats, SOURCE],
        adapters,
        pg_store,
        JobFilter(FILTERS),
        RunRecorder(tmp_path / "runs"),
        2,
    )
    assert [o.status for o in outcomes] == ["ok", "error"]
    assert len(pg_store.db.query("select * from public.source_runs")) == 2
    assert len(pg_store.db.query("select * from public.jobs")) == 2


def test_llm_failure_keeps_snapshot_and_records_usage(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    adapter, current, backend = setup_page(
        recorded, make_http, [recorded["stage3-pages"]["postings"][:1]]
    )
    run_page(adapter, pg_store, tmp_path)
    before = pg_store.db.query("select state from public.source_monitor_state")[0][
        "state"
    ]
    current["html"] = recorded["stage3-pages"]["changed"]
    backend.responses = ["broken JSON private-description"] * 3
    outcome = run_page(adapter, pg_store, tmp_path)
    assert outcome.status == "error" and "private-description" not in outcome.error
    assert (
        pg_store.db.query("select state from public.source_monitor_state")[0]["state"]
        == before
    )
    metadata = pg_store.db.query(
        "select metadata from public.source_runs where status='error'"
    )[0]["metadata"]
    assert metadata["llm"]["input_tokens"] == 300 and metadata["llm"]["attempts"] == 3


def test_real_chromium_ipc_rendering_with_polite_mock_http(
    recorded: dict[str, Any], make_http: Any
) -> None:
    if not os.environ.get("RUN_CHROMIUM_TESTS"):
        pytest.skip("Set RUN_CHROMIUM_TESTS=1 with an installed Chromium browser")
    fixture = recorded["stage3-pages"]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=fixture["robots_allow"])
        if request.url.path == "/app.js":
            return httpx.Response(
                200,
                text="document.body.innerHTML=" + json.dumps(fixture["base"]) + ";",
                headers={"Content-Type": "text/javascript"},
            )
        return httpx.Response(
            200, text=fixture["shell"], headers={"Content-Type": "text/html"}
        )

    pages = PageFetcher(make_http(handler), timeout=25)
    page, method = pages.fetch(SOURCE.url, domain=SOURCE.company_domain)
    assert method == "playwright" and fixture["postings"][0]["title"] in page.text
    assert requested.count("/robots.txt") == 1 and "/app.js" in requested


def test_actual_page_timeout_is_recorded_as_failure(
    recorded: dict[str, Any], make_http: Any, pg_store: PostgresStore, tmp_path: Path
) -> None:
    def slow_render(url: str, deadline: float) -> tuple[str, str]:
        time.sleep(0.1)
        return recorded["stage3-pages"]["base"], url

    http = make_http(
        lambda r: httpx.Response(
            200,
            text=recorded["stage3-pages"]["robots_allow"]
            if r.url.path == "/robots.txt"
            else recorded["stage3-pages"]["shell"],
        )
    )
    adapter = CareersPageAdapter(PageFetcher(http, timeout=0.01, renderer=slow_render))
    result = run_page(adapter, pg_store, tmp_path)
    assert result.status == "error" and result.error == "TimeoutError"
    assert (
        pg_store.db.query("select status from public.source_runs")[0]["status"]
        == "error"
    )


def test_truncated_llm_reply_tokens_counted_and_unknown_cost_reported() -> None:
    class TruncatedBackend:
        def complete(self, system: str, content: str) -> Reply:
            return Reply("[]", 200, 100, complete=False)

    usage = Usage()
    with pytest.raises(ValueError):
        ExtractionClient(TruncatedBackend(), 2, 4, sleep=lambda _: None).extract(
            "{}", usage
        )
    assert usage.input_tokens == 600 and usage.output_tokens == 300
    assert usage.cost_usd == pytest.approx(0.0024)

    class FailingBackend:
        def complete(self, system: str, content: str) -> Reply:
            raise TimeoutError("private provider response")

    usage = Usage()
    with pytest.raises(ValueError):
        ExtractionClient(FailingBackend(), 2, 4, sleep=lambda _: None).extract(
            "{}", usage
        )
    assert usage.cost_usd is None and usage.uncounted_attempts == 3


@pytest.mark.parametrize(
    "date",
    [
        "10 October 2026",
        "October 10, 2026",
        "Saturday, 10 October 2026",
        "2026-10-10T10:22:30+01:00",
        "10/10/2026",
        "3 hours ago",
    ],
)
def test_date_formats_clean_without_damaging_job_links(date: str) -> None:
    url = "https://fairmoney.io/jobs/2026-10-10/backend"
    assert clean_text("Updated: " + date + " [" + url + "]") == "Updated: [" + url + "]"


def test_recorded_public_html_snapshots(make_http: Any) -> None:
    shell = (FIXTURES / "fairmoney-shell-recorded.html").read_text(encoding="utf-8")
    actual = (FIXTURES / "techcabal-jobs-recorded.html").read_text(encoding="utf-8")
    cleaned = snapshot(actual, "https://techcabal.com/jobs/")
    assert "Job Opportunities" in cleaned.text and cleaned.content_hash
    pages = PageFetcher(
        make_http(
            lambda r: (
                httpx.Response(404)
                if r.url.path == "/robots.txt"
                else httpx.Response(200, text=shell)
            )
        ),
        renderer=lambda url, _: (actual, url),
    )
    result, method = pages.fetch("https://fairmoney.io/")
    assert method == "playwright" and "Job Opportunities" in result.text


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_official_provider_sdk_contract_and_credential_isolation(
    provider: str, make_http: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.storage import PacedTransport

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["model"] == "configured-model"
        if provider == "openai":
            assert request.headers["Authorization"] == "Bearer test-key"
            reply = {
                "id": "test-reply",
                "object": "chat.completion",
                "created": 1,
                "model": "configured-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "[]"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        else:
            assert request.headers["x-api-key"] == "test-key"
            reply = {
                "id": "test-reply",
                "type": "message",
                "role": "assistant",
                "model": "configured-model",
                "content": [{"type": "text", "text": "[]"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        return httpx.Response(200, json=reply)

    def public_handler(request: httpx.Request) -> httpx.Response:
        assert (
            "Authorization" not in request.headers
            and "x-api-key" not in request.headers
        )
        return httpx.Response(200)

    http = make_http(public_handler)
    monkeypatch.setattr(
        llm_module,
        "PacedTransport",
        lambda limiter: PacedTransport(limiter, httpx.MockTransport(handler)),
    )
    client = ExtractionClient(
        llm_module.SDKBackend(provider, "test-key", "configured-model", http)
    )
    usage = Usage()
    assert client.extract("{}", usage) == []
    assert usage.input_tokens == 10 and usage.output_tokens == 5
    http.get(SOURCE.url)
    assert len(requests) == 1


def test_real_browser_blocked_resource_recorded_as_warning(
    recorded: dict[str, Any], make_http: Any
) -> None:
    if not os.environ.get("RUN_CHROMIUM_TESTS"):
        pytest.skip("Set RUN_CHROMIUM_TESTS=1 with an installed Chromium browser")
    fixture = recorded["stage3-pages"]
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text=fixture["robots_allow"] + "Disallow: /blocked.js\n"
            )
        html = fixture["base"] + '<script src="/blocked.js"></script>'
        return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})

    page, _ = PageFetcher(make_http(handler), timeout=25).fetch(
        SOURCE.url, "playwright", SOURCE.company_domain
    )
    assert page.warnings and "ValueError" in page.warnings[0]
    assert not any(url.endswith("blocked.js") for url in requests)


def test_link_without_visible_text_is_an_empty_page(make_http: Any) -> None:
    body = '<main><a href="https://fairmoney.io/a"></a></main>'
    pages = PageFetcher(
        make_http(
            lambda r: (
                httpx.Response(404)
                if r.url.path == "/robots.txt"
                else httpx.Response(200, text=body)
            )
        ),
        renderer=lambda url, _: (body, url),
    )
    with pytest.raises(ValueError, match="empty"):
        pages.fetch(SOURCE.url, domain=SOURCE.company_domain)


@pytest.mark.parametrize(
    "rules,path,allowed",
    [
        ("Allow: /\nDisallow: /careers", "/careers", False),
        ("Disallow: /careers/*\nAllow: /careers/public$", "/careers/private", False),
        ("Disallow: /careers/*\nAllow: /careers/public$", "/careers/public", True),
        (
            "Disallow: /careers/*\nAllow: /careers/public$",
            "/careers/public/other",
            False,
        ),
        ("Allow: /same\nDisallow: /same", "/same", True),
    ],
)
def test_robots_longest_rule_wildcards_and_anchors(
    rules: str, path: str, allowed: bool
) -> None:
    policy = RobotsPolicy()
    policy.parse(("User-agent: *\n" + rules).splitlines())
    assert policy.can_fetch("JobRadar/0.1", "https://fairmoney.io" + path) == allowed


def test_robots_blank_lines_merged_groups_and_fractional_crawl_delay() -> None:
    policy = RobotsPolicy()
    policy.parse(
        (
            "User-agent: *\n\nDisallow: /a\nCrawl-delay: 2.5\n\n"
            "User-agent: *\nDisallow: /b\nRequest-rate: 1/4"
        ).splitlines()
    )
    assert not policy.can_fetch("JobRadar/0.1", "https://fairmoney.io/a")
    assert not policy.can_fetch("JobRadar/0.1", "https://fairmoney.io/b")
    assert policy.polite_interval("JobRadar/0.1") == 4
