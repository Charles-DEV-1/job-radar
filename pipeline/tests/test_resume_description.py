"""Compaction operates locally, preserves originals, and bounds real HTTP input."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from resume_helpers import (
    ControlledBackend,
    job,
    synthetic_resume,
    tailoring_proposal,
    verified_pagination,
)
from test_groq import MODELS, groq_reply, mock_provider

from pipeline.llm.client import SDKBackend, Usage
from pipeline.llm.pacing import prompt_tokens
from pipeline.resume.description import compact_description
from pipeline.resume.prompt import request
from pipeline.resume.tailor import TailoringClient, tailor_job


def test_short_unheaded_description_is_preserved() -> None:
    text = "Build Python APIs. Use SQL for reports.\nWork with engineers."
    compacted = compact_description(text)
    assert compacted.text == " ".join(text.split())
    assert not compacted.truncated and compacted.dropped_sections == 0


def test_heading_rules_remove_boilerplate_preserve_relevant_sections() -> None:
    text = """About us:
CompanyBoilerplate AWS
Job Title: Backend Engineer
Responsibilities:
Build Python APIs.
Qualifications:
SQL experience.
Tech stack: Python SQL
Benefits:
BenefitsSentinel Kubernetes
Equal Opportunity:
OpportunitySentinel Terraform
How to apply:
ApplicationSentinel Docker
"""
    compacted = compact_description(text)
    assert compacted.dropped_sections == 4
    for sentinel in (
        "CompanyBoilerplate",
        "BenefitsSentinel",
        "OpportunitySentinel",
        "ApplicationSentinel",
    ):
        assert sentinel not in compacted.text
    for relevant in (
        "Backend Engineer",
        "Build Python APIs.",
        "SQL experience.",
        "Tech stack: Python SQL",
    ):
        assert relevant in compacted.text


def test_lowest_value_sections_trim_first() -> None:
    compacted = compact_description(
        "Role overview:\n"
        + "OverviewSentinel " * 20
        + "\nResponsibilities:\nBuild APIs.\nRequirements:\nPython SQL AWS"
    )
    compacted.trim(1000)
    assert "OverviewSentinel" not in compacted.text
    assert "Build APIs." in compacted.text and "Python SQL AWS" in compacted.text
    compacted.trim(1000)
    assert "Build APIs." not in compacted.text
    assert "Python SQL AWS" in compacted.text


def test_company_heading_and_inline_equal_opportunity_notice() -> None:
    text = (
        "About Example Company\nCompanySentinel\nRequirements:\nPython SQL\n"
        "Example Company is an equal opportunity and affirmative action employer.\n"
        "NoticeSentinel"
    )
    result = compact_description(text, "Example Company")
    assert result.text == "Requirements: Python SQL"
    assert result.dropped_sections == 2


@pytest.mark.parametrize("model", MODELS)
def test_long_description_fits_budget_and_logs_truncation(
    tmp_path: Path,
    recorded: dict[str, Any],
    make_http: Any,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    master, original, template, layout = synthetic_resume(tmp_path)
    current_job = job()
    full = (
        "About us:\n"
        + "BoilerplateSentinel " * 1000
        + "\nRequirements:\nPython SQL AWS\nResponsibilities:\n"
        + "Build and maintain public APIs. " * 1000
        + "\nBenefits:\nKubernetesBenefits"
    )
    current_job["description_text"] = full
    calls: list[dict[str, Any]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        system, content = [message["content"] for message in body["messages"]]
        payload = json.loads(content)
        calls.append(payload)
        assert "Python SQL AWS" in payload["job"]["description_text"]
        assert (
            "BoilerplateSentinel" not in content and "KubernetesBenefits" not in content
        )
        assert prompt_tokens(system, content) + body["max_completion_tokens"] <= 6000
        assert body["max_completion_tokens"] >= request(master, current_job)[2]
        return httpx.Response(
            200,
            json=groq_reply(recorded, json.dumps(tailoring_proposal(master)), model),
        )

    mock_provider(monkeypatch, handler)
    backend = SDKBackend(
        "groq", "test-groq-key", model, make_http(lambda _: None), 6000
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
    assert len(calls) == 1 and current_job["description_text"] == full
    assert any("budget truncation=yes" in warning for warning in result.warnings)
    assert "budget truncation=yes" in result.report_path.read_text()


@pytest.mark.parametrize("quote,accepted", [("AWS", True), ("Kubernetes", False)])
def test_gap_quotes_only_validate_against_sent_compacted_text(
    tmp_path: Path, quote: str, accepted: bool
) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    current_job = job()
    current_job["description_text"] = (
        "Requirements:\nPython SQL AWS\nBenefits:\nKubernetes"
    )
    backend = ControlledBackend([tailoring_proposal(master)])
    client = TailoringClient(backend, sleep=lambda _: None)
    warnings: list[str] = []
    result = client.propose(
        master, current_job, Usage(), lambda _: None, warnings=warnings
    )
    if accepted:
        assert quote in result.match_notes.gaps
    else:
        assert quote not in result.match_notes.gaps
        assert any("sections dropped" in warning for warning in warnings)
    for payload in backend.calls:
        assert "Kubernetes" not in payload["job"]["description_text"]
        assert "match_note_options" not in payload


def test_gap_cannot_quote_redacted_contact(tmp_path: Path) -> None:
    master, _, _, _ = synthetic_resume(tmp_path)
    master.contact.email = "private@example.test"
    current_job = job()
    current_job["description_text"] += " Must contact private@example.test"
    backend = ControlledBackend([tailoring_proposal(master)])
    result = TailoringClient(backend, sleep=lambda _: None).propose(
        master, current_job, Usage(), lambda _: None
    )
    assert "private@example.test" not in result.match_notes.gaps
    assert all(
        "private@example.test" not in json.dumps(payload) for payload in backend.calls
    )
