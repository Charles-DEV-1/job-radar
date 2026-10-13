"""Resume requests must fit a full response without consuming futile retries."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from resume_helpers import job, synthetic_resume
from test_groq import MODELS, groq_reply, mock_provider

from pipeline.llm.client import SDKBackend, Usage
from pipeline.llm.pacing import prompt_tokens
from pipeline.resume.pdf import Pagination
from pipeline.resume.privacy import ContactRedactor
from pipeline.resume.prompt import compact_json, request
from pipeline.resume.tailor import TailoringClient, tailor_job
from pipeline.resume.validate import unchanged


def test_compact_request_preserves_all_facts_and_description_words(
    tmp_path: Path,
) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    current_job = job()
    current_job["description_text"] = "Requirements:\n Python\tSQL\n  AWS\n"
    system, payload, minimum = request(master, current_job)
    assert payload["job"]["description_text"] == "Requirements: Python SQL AWS"
    for key, pointer in payload["evidence"].items():
        value: Any = payload["master"]
        for part in pointer.lstrip("/").split("/"):
            value = value[int(part)] if isinstance(value, list) else value[part]
        if key == "summary":
            assert value == master.summary
        elif key == "b-1":
            assert value == master.experience[0].bullets[0].text
    serialized = compact_json(payload)
    # A real bullet occurs once instead of twice (facts + evidence text).
    assert serialized.count(master.experience[0].bullets[0].text) == 1
    assert '"title":' not in system  # Schema presentation labels removed.
    assert '"additionalProperties":false' in system
    assert minimum >= 2048


@pytest.mark.parametrize("model", MODELS)
def test_8000_budget_renders_complete_resume_with_reserved_output(
    tmp_path: Path,
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    current_job = job()
    current_job["description_text"] += "\n" + "Public role requirements. " * 100
    _, _, minimum = request(master, current_job)
    calls: list[dict[str, Any]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        calls.append(payload)
        system, content = (m["content"] for m in payload["messages"])
        capacity = payload["max_completion_tokens"]
        assert minimum <= capacity <= 4096
        assert prompt_tokens(system, content) + capacity <= 8000
        assert payload["reasoning_effort"] == "low"
        return httpx.Response(
            200, json=groq_reply(recorded, unchanged(master).model_dump_json(), model)
        )

    mock_provider(monkeypatch, handler)
    backend = SDKBackend(
        "groq", "test-groq-key", model, make_http(lambda _: None), 8000
    )
    result = tailor_job(
        master,
        current_job,
        original,
        template,
        layout,
        tmp_path / "output",
        TailoringClient(backend),
        Usage(),
        Pagination(discover=False),
    )
    assert result.status == "ready", result.error
    assert len(calls) == 1


def test_prompt_that_leaves_tiny_output_space_is_rejected_without_http(
    tmp_path: Path, make_http: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    current_job = job()
    current_job["description_text"] = "x" * 12_000
    system, payload, minimum = request(master, current_job)
    redactor = ContactRedactor(master.contact)
    estimated = prompt_tokens(
        redactor.text(system), compact_json(redactor.payload(payload))
    )
    # Old behavior would send a request with just 565 output tokens remaining.
    limit = estimated + 565
    assert minimum > 565
    mock_provider(monkeypatch, lambda _: pytest.fail("Insufficient response capacity"))
    backend = SDKBackend(
        "groq", "test-groq-key", MODELS[1], make_http(lambda _: None), limit
    )
    usage = Usage()
    result = tailor_job(
        master,
        current_job,
        original,
        template,
        layout,
        tmp_path / "output",
        TailoringClient(backend, sleep=lambda _: pytest.fail("No futile retry")),
        usage,
        Pagination(discover=False),
    )
    assert result.status == "failed" and "minimum response" in result.error
    assert usage.attempts == 1 and usage.input_tokens == usage.output_tokens == 0
    assert result.path.read_bytes() == original.read_bytes()


@pytest.mark.parametrize("missing_usage", [False, True])
def test_truncation_and_missing_usage_have_distinct_errors(
    tmp_path: Path,
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
    missing_usage: bool,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        reply = groq_reply(recorded, '{"summary":')
        reply["choices"][0]["finish_reason"] = "length"
        if missing_usage:
            reply.pop("usage")
        return httpx.Response(200, json=reply)

    mock_provider(monkeypatch, handler)
    backend = SDKBackend(
        "groq", "test-groq-key", MODELS[1], make_http(lambda _: None), 8000
    )
    usage = Usage()
    result = tailor_job(
        master,
        job(),
        original,
        template,
        layout,
        tmp_path / "output",
        TailoringClient(backend, sleep=lambda _: None),
        usage,
        Pagination(discover=False),
    )
    assert result.status == "failed"
    if missing_usage:
        assert "missing token usage" in result.error and calls == 3
    else:
        assert "output token limit" in result.error and calls == 1
        assert usage.input_tokens == 10 and usage.output_tokens == 5
    assert result.path.read_bytes() == original.read_bytes()
