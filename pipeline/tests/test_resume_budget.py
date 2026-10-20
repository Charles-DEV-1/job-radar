"""Resume requests must fit a full response without consuming futile retries."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from resume_helpers import (
    job,
    synthetic_resume,
    tailoring_proposal,
    verified_pagination,
)
from test_groq import MODELS, groq_reply, mock_provider

from pipeline.llm.client import SDKBackend, Usage
from pipeline.llm.pacing import prompt_tokens
from pipeline.resume.prompt import compact_json, request
from pipeline.resume.tailor import TailoringClient, tailor_job


def test_compact_request_preserves_all_facts_and_description_words(
    tmp_path: Path,
) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    current_job = job()
    current_job["description_text"] = "Requirements:\n Python\tSQL\n  AWS\n"
    system, payload, minimum = request(master, current_job)
    assert payload["job"]["description_text"] == "Requirements: Python SQL AWS"
    assert payload["slot_character_limits"]["summary"] == len(master.summary)
    assert payload["slot_character_limits"]["bullets"] == {
        bullet.id: len(bullet.text)
        for role in master.experience
        for bullet in role.bullets
    }
    assert "at most 3" in system
    assert payload["master"]["summary"] == master.summary
    assert payload["master"]["experience"][0]["bullets"][0]["text"] == (
        master.experience[0].bullets[0].text
    )
    assert "evidence" not in payload and "match_note_options" not in payload
    serialized = compact_json(payload)
    # A real bullet occurs once in the model facts.
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
            200,
            json=groq_reply(recorded, json.dumps(tailoring_proposal(master)), model),
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
        verified_pagination(),
    )
    assert result.status == "ready", result.error
    assert len(calls) == 1


def test_master_facts_alone_exceed_budget_without_http(
    tmp_path: Path, make_http: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    current_job = job()
    current_job["description_text"] = "Required: Python. " + "x" * 12_000
    _, _, minimum = request(master, current_job)
    # Job compaction cannot remove master facts or the reserved full response.
    limit = minimum - 1
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
        verified_pagination(),
    )
    assert result.status == "ready" and result.error is None
    assert any("minimum response" in warning for warning in result.warnings)
    assert usage.attempts == 1 and usage.input_tokens == usage.output_tokens == 0
    assert result.path.read_bytes() != original.read_bytes()


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
        verified_pagination(),
    )
    assert result.status == "ready" and result.error is None
    if missing_usage:
        assert calls == 3
        assert any("missing token usage" in warning for warning in result.warnings)
    else:
        assert calls == 1
        assert any("output token limit" in warning for warning in result.warnings)
        assert usage.input_tokens == 10 and usage.output_tokens == 5
    assert result.path.read_bytes() != original.read_bytes()
