"""Gemini API requests use mocked HTTP and the shared strict JSON pipeline."""

import json
from typing import Any

import httpx
import pytest

from pipeline.llm import client as llm_module
from pipeline.llm.client import ExtractionClient, SDKBackend, Usage
from pipeline.storage import PacedTransport


def mock_google(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    def transport(limiter: Any) -> PacedTransport:
        def checked(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert str(request.url) == (
                "https://generativelanguage.googleapis.com/v1beta/"
                "models/gemini-test:generateContent"
            )
            assert request.headers["x-goog-api-key"] == "test-google-key"
            assert "JobRadar" in request.headers["User-Agent"]
            return handler(request)

        return PacedTransport(limiter, httpx.MockTransport(checked))

    monkeypatch.setattr(llm_module, "PacedTransport", transport)


def test_google_environment_json_mode_usage_and_key_isolation(
    make_http: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["systemInstruction"]["parts"][0]["text"].startswith(
            "Extract job postings"
        )
        assert payload["contents"][0]["parts"][0]["text"] == "{}"
        assert payload["generationConfig"] == {
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        }
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "internal reasoning", "thought": True},
                                {"text": "[]"},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 2,
                },
            },
        )

    mock_google(monkeypatch, handler)

    def public(request: httpx.Request) -> httpx.Response:
        assert "x-goog-api-key" not in request.headers
        return httpx.Response(200)

    http = make_http(public)
    for name, value in {
        "LLM_PROVIDER": "google",
        "LLM_API_KEY": "test-google-key",
        "LLM_MODEL": "gemini-test",
        "LLM_INPUT_USD_PER_MILLION": "1",
        "LLM_OUTPUT_USD_PER_MILLION": "2",
    }.items():
        monkeypatch.setenv(name, value)
    client = ExtractionClient.from_environment(http)
    usage = Usage()
    assert client.extract("{}", usage) == []
    assert len(requests) == 1
    assert usage.input_tokens == 10 and usage.output_tokens == 7
    assert usage.cost_usd == pytest.approx(0.000024)
    http.get("https://example.test/public")


def test_google_retry_honors_retry_after_without_leaking_error_body(
    make_http: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[float] = []
    from conftest import FakeClock

    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(clock.now)
        if len(calls) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "3"},
                json={"error": {"message": "private diagnostic body"}},
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "{}"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                },
            },
        )

    mock_google(monkeypatch, handler)
    http = make_http(lambda _: httpx.Response(200), clock=clock)
    reply = SDKBackend("google", "test-google-key", "gemini-test", http).complete(
        "system", "content"
    )
    assert reply.complete and reply.usage_known
    assert reply.rate_limit_retries == 1
    assert calls == [0, 3]
