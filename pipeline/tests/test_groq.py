"""Mocked Groq contracts, bounded rate retries, pacing and durable page recovery."""

import json
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeClock
from postgres_helpers import PostgresStore
from resume_helpers import job, synthetic_resume
from test_stage3 import SOURCE, run_page

from pipeline.llm import client as llm_module
from pipeline.llm.client import ExtractionClient, SDKBackend, Usage, retry_after_delay
from pipeline.llm.pacing import TokenBudget
from pipeline.resume.pdf import Pagination
from pipeline.resume.tailor import TailoringClient, tailor_job
from pipeline.resume.validate import unchanged
from pipeline.sources.careers_page import CareersPageAdapter
from pipeline.sources.pages import PageFetcher
from pipeline.storage import PacedTransport

MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]


def groq_reply(
    recorded: dict[str, Any], text: str = "[]", model: str = MODELS[0]
) -> dict[str, Any]:
    reply = deepcopy(recorded["groq-chat-completions"]["reply"])
    reply["model"] = model
    reply["choices"][0]["message"]["content"] = text
    return reply


def mock_provider(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    def transport(http: Any) -> PacedTransport:
        def checked(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer test-groq-key"
            assert "JobRadar" in request.headers["User-Agent"]
            return handler(request)

        return PacedTransport(http, httpx.MockTransport(checked))

    monkeypatch.setattr(llm_module, "PacedTransport", transport)


@pytest.mark.parametrize("model", MODELS)
def test_groq_environment_chat_payload_strict_schema_usage_and_key_isolation(
    model: str,
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == model
        assert payload["messages"][0]["role"] == "system"
        assert "JSON array" in payload["messages"][0]["content"]
        assert payload["max_completion_tokens"] == 4096
        return httpx.Response(200, json=groq_reply(recorded, model=model))

    mock_provider(monkeypatch, handler)

    def public(request: httpx.Request) -> httpx.Response:
        assert (
            "Authorization" not in request.headers
            and "x-api-key" not in request.headers
        )
        return httpx.Response(200)

    http = make_http(public)
    for name, value in {
        "LLM_PROVIDER": "groq",
        "LLM_API_KEY": "test-groq-key",
        "LLM_MODEL": model,
        "OPENAI_BASE_URL": "https://unused.example",
        "LLM_INPUT_USD_PER_MILLION": "1",
        "LLM_OUTPUT_USD_PER_MILLION": "2",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LLM_MAX_TOKENS_PER_MINUTE", raising=False)
    client = ExtractionClient.from_environment(http)
    usage = Usage()
    assert client.extract("{}", usage) == []
    assert usage.attempts == 1 and usage.input_tokens == 10 and usage.output_tokens == 5
    assert usage.cost_usd == pytest.approx(0.00002)
    assert client.backend.budget.limit == 6000
    http.get(SOURCE.url)
    assert len(calls) == 1


@pytest.mark.parametrize("limit", ["0", "-1", "bad", "NaN", "6000.5"])
def test_invalid_token_budget_fails_configuration_without_http(
    limit: str,
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "LLM_PROVIDER": "groq",
        "LLM_API_KEY": "test-groq-key",
        "LLM_MODEL": MODELS[0],
        "LLM_MAX_TOKENS_PER_MINUTE": limit,
    }.items():
        monkeypatch.setenv(name, value)
    http = make_http(lambda _: pytest.fail("Invalid budget must not send HTTP"))
    with pytest.raises(ValueError, match="positive integer"):
        ExtractionClient.from_environment(http)


def test_429_honors_fractional_retry_after_and_exponential_backoff(
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = FakeClock()
    http = make_http(lambda _: httpx.Response(200), clock=timer)
    times: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        times.append(timer.now)
        if len(times) <= 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "2.5"},
                json=recorded["groq-chat-completions"]["rate_limit"],
            )
        return httpx.Response(200, json=groq_reply(recorded))

    mock_provider(monkeypatch, handler)
    usage = Usage()
    assert (
        ExtractionClient(SDKBackend("groq", "test-groq-key", MODELS[0], http)).extract(
            "{}", usage
        )
        == []
    )
    assert times == [0, 2.5, 5, 9]
    assert usage.attempts == 4 and usage.uncounted_attempts == 3
    assert (
        usage.input_tokens == 10 and usage.output_tokens == 5 and usage.cost_usd is None
    )


def test_server_retry_after_is_not_shortened_to_thirty_seconds(
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = FakeClock()
    times: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        times.append(timer.now)
        return (
            httpx.Response(
                429,
                headers={"Retry-After": "120"},
                json=recorded["groq-chat-completions"]["rate_limit"],
            )
            if len(times) == 1
            else httpx.Response(200, json=groq_reply(recorded))
        )

    mock_provider(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200), clock=timer)
    SDKBackend("groq", "test-groq-key", MODELS[0], http).complete("system", "content")
    assert times == [0, 120] and all(delay <= 60 for delay in timer.sleeps)


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, 0),
        ("bad", 0),
        ("inf", 0),
        ("-1", 0),
        ("2.5", 2.5),
        ("Tue, 13 Oct 2026 12:00:09 GMT", 9),
        ("Tue, 13 Oct 2026 11:59:00 GMT", 0),
    ],
)
def test_retry_after_seconds_and_http_date(header: str | None, expected: float) -> None:
    now = datetime(2026, 10, 13, 12, tzinfo=UTC).timestamp()
    assert retry_after_delay(header, now) == expected


def test_rate_limit_failure_records_error_keeps_baseline_and_retries_next_run(
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
    pg_store: PostgresStore,
    tmp_path: Path,
) -> None:
    fixture = recorded["stage3-pages"]
    current = {"html": fixture["base"], "rate_limited": False}
    provider_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        if current["rate_limited"]:
            return httpx.Response(
                429, json=recorded["groq-chat-completions"]["rate_limit"]
            )
        posting = (
            fixture["postings"][:1]
            if current["html"] == fixture["base"]
            else fixture["postings"][1:]
        )
        return httpx.Response(200, json=groq_reply(recorded, json.dumps(posting)))

    mock_provider(monkeypatch, handler)

    def page(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            text=fixture["robots_allow"]
            if request.url.path == "/robots.txt"
            else current["html"],
        )

    http = make_http(page)
    client = ExtractionClient(
        SDKBackend("groq", "test-groq-key", MODELS[0], http), sleep=http.sleep
    )
    adapter = CareersPageAdapter(PageFetcher(http), client)
    assert run_page(adapter, pg_store, tmp_path).status == "ok"
    source_id = pg_store.db.query("select id from public.sources")[0]["id"]
    before = pg_store.monitor_state(source_id)
    current.update(html=fixture["changed"], rate_limited=True)
    failed = run_page(adapter, pg_store, tmp_path)
    assert failed.status == "error" and "HTTP 429" in failed.error
    assert len(provider_calls) == 5  # successful baseline plus exactly four attempts
    assert pg_store.monitor_state(source_id) == before
    error_run = pg_store.db.query(
        "select * from public.source_runs where status='error'"
    )[0]
    assert error_run["metadata"]["llm"]["attempts"] == 4
    assert "sensitive-provider-body" not in error_run["error_message"]
    assert "test-groq-key" not in (tmp_path / "runs.jsonl").read_text(encoding="utf-8")
    current["rate_limited"] = False
    recovered = run_page(adapter, pg_store, tmp_path)
    assert recovered.status == "ok" and recovered.counts.new_jobs == 1
    assert (
        len(provider_calls) == 6
        and pg_store.monitor_state(source_id)["hash"] != before["hash"]
    )


def test_token_budget_shared_between_clients_and_actual_usage_controls_next_call(
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = FakeClock()
    times: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        times.append(timer.now)
        reply = groq_reply(recorded)
        reply["usage"] = {
            "prompt_tokens": 1000,
            "completion_tokens": 2000,
            "total_tokens": 3000,
        }
        return httpx.Response(200, json=reply)

    mock_provider(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200), clock=timer)
    first = SDKBackend("groq", "test-groq-key", MODELS[0], http)
    second = SDKBackend("groq", "test-groq-key", MODELS[1], http)
    assert first.budget is second.budget
    first.complete("system", "content")
    second.complete("system", "content")
    assert times == [0, 60]


def test_pending_reservation_cannot_expire_while_another_thread_waits() -> None:
    waiting, release = threading.Event(), threading.Event()
    timer = FakeClock()

    def sleep(delay: float) -> None:
        waiting.set()
        assert release.wait(2)
        timer.sleep(delay)

    budget = TokenBudget(100, timer, sleep)
    first = budget.reserve(80)
    second: list[Any] = []
    thread = threading.Thread(target=lambda: second.append(budget.reserve(30)))
    thread.start()
    assert waiting.wait(2) and not second
    timer.now = 120  # a long pending operation must remain reserved
    budget.finish(first, 20)
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive() and len(second) == 1


def test_groq_strict_json_rejects_wrong_root_and_extra_posting_fields(
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        '{"jobs": []}',
        json.dumps(
            [
                {
                    "title": "Example Engineer",
                    "location": None,
                    "apply_url": "https://example.com/job",
                    "description_snippet": None,
                    "invented": True,
                }
            ]
        ),
        "[]",
    ]
    calls: list[str] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(responses.pop(0))
        return httpx.Response(200, json=groq_reply(recorded, calls[-1]))

    mock_provider(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200))
    usage = Usage()
    assert (
        ExtractionClient(
            SDKBackend("groq", "test-groq-key", MODELS[0], http), sleep=http.sleep
        ).extract("{}", usage)
        == []
    )
    assert len(calls) == 3 and usage.attempts == 3


@pytest.mark.parametrize("model", MODELS)
def test_groq_resume_exhausted_rate_limit_falls_back_without_nested_retries(
    model: str,
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json=recorded["groq-chat-completions"]["rate_limit"])

    mock_provider(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200))
    usage = Usage()
    client = TailoringClient(
        SDKBackend("groq", "test-groq-key", model, http), sleep=http.sleep
    )
    result = tailor_job(
        master,
        job(),
        original,
        template,
        layout,
        tmp_path / "output",
        client,
        usage,
        Pagination(discover=False),
    )
    assert result.status == "failed" and "HTTP 429" in result.error
    assert result.path.read_bytes() == original.read_bytes()
    assert len(calls) == usage.attempts == 4


def test_configured_budget_caps_completion_and_rejects_oversized_input(
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=groq_reply(recorded))

    mock_provider(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200))
    backend = SDKBackend("groq", "test-groq-key", MODELS[0], http, 1000)
    backend.complete("system", "content")
    assert 0 < payloads[0]["max_completion_tokens"] < 1000
    with pytest.raises(ValueError, match="LLM_MAX_TOKENS_PER_MINUTE"):
        backend.complete("system", "private-content" * 1000)
    usage = Usage()
    with pytest.raises(ValueError, match="LLM_MAX_TOKENS_PER_MINUTE"):
        ExtractionClient(backend).extract("private-content" * 1000, usage)
    assert len(payloads) == 1
    assert usage.attempts == 1


@pytest.mark.parametrize("model", MODELS)
def test_groq_valid_resume_json_renders_with_unchanged_truth_validator(
    model: str,
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == model
        return httpx.Response(
            200, json=groq_reply(recorded, unchanged(master).model_dump_json(), model)
        )

    mock_provider(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200))
    client = TailoringClient(SDKBackend("groq", "test-groq-key", model, http))
    usage = Usage()
    result = tailor_job(
        master,
        job(),
        original,
        template,
        layout,
        tmp_path / "output",
        client,
        usage,
        Pagination(discover=False),
    )
    assert result.status == "ready" and result.error is None
    assert usage.attempts == 1 and usage.input_tokens == 10 and usage.output_tokens == 5
    assert result.match_notes == {"matched": [], "gaps": []}


def test_missing_provider_usage_keeps_full_token_reservation(
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = FakeClock()
    times: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        times.append(timer.now)
        reply = groq_reply(recorded)
        if len(times) == 1:
            reply.pop("usage")
        return httpx.Response(200, json=reply)

    mock_provider(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200), clock=timer)
    backend = SDKBackend("groq", "test-groq-key", MODELS[0], http)
    assert not backend.complete("system", "content").usage_known
    assert backend.complete("system", "content").usage_known
    assert times == [0, 60]
