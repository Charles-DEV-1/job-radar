from copy import deepcopy
from typing import Any

import httpx
import pytest

from pipeline.sources.ats import ADAPTER_CLASSES
from pipeline.sources.ats.base import ATSAdapter, BoardNotFound
from pipeline.sources.ats.smartrecruiters import SmartRecruitersAdapter


@pytest.mark.parametrize("adapter_class", ADAPTER_CLASSES)
@pytest.mark.parametrize(
    "case", ["found", "not_found", "empty", "malformed_json", "timeout"]
)
def test_probe_cases(
    adapter_class: type[ATSAdapter], case: str, recorded: dict[str, Any], make_http: Any
) -> None:
    payload = deepcopy(recorded[adapter_class.name])
    if case == "empty":
        if adapter_class.collection_key is None:
            payload = []
        else:
            payload[adapter_class.collection_key] = []
        if adapter_class.name == "smartrecruiters":
            payload["totalFound"] = 0

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "Authorization" not in request.headers
        if case == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if case == "not_found":
            return httpx.Response(404)
        if case == "malformed_json":
            return httpx.Response(200, text="{broken")
        return httpx.Response(200, json=payload)

    adapter = adapter_class(make_http(handler))
    result = adapter.probe("sample")
    assert result.ats == adapter_class.name
    assert result.duration_ms >= 0
    if case in {"found", "empty"}:
        assert result.status == "found"
        expected = payload["totalFound"] if adapter.name == "smartrecruiters" else 2
        assert result.job_count == (0 if case == "empty" else expected)
        assert result.error is None
        if case == "found":
            assert result.sample_titles
            assert result.sample_apply_urls
    elif case == "not_found":
        assert result.status == "not_found"
        assert result.error is None
    else:
        assert result.status == "error"
        assert result.error


@pytest.mark.parametrize("adapter_class", ADAPTER_CLASSES)
@pytest.mark.parametrize(
    "payload", [{}, {"jobs": None}, {"jobs": ["not an object"]}, "oops"]
)
def test_wrong_shapes_are_errors(
    adapter_class: type[ATSAdapter], payload: Any, make_http: Any
) -> None:
    adapter = adapter_class(make_http(lambda _: httpx.Response(200, json=payload)))
    assert adapter.probe("sample").status == "error"


@pytest.mark.parametrize("adapter_class", ADAPTER_CLASSES)
def test_raw_fetch_is_unmodified(
    adapter_class: type[ATSAdapter], recorded: dict[str, Any], make_http: Any
) -> None:
    payload = deepcopy(recorded[adapter_class.name])
    if adapter_class.name == "smartrecruiters":
        payload["totalFound"] = len(payload["content"])
    adapter = adapter_class(make_http(lambda _: httpx.Response(200, json=payload)))
    expected = (
        payload if adapter.collection_key is None else payload[adapter.collection_key]
    )
    assert adapter.fetch_jobs("sample") == expected


@pytest.mark.parametrize("adapter_class", ADAPTER_CLASSES)
def test_fetch_failure_is_not_an_empty_board(
    adapter_class: type[ATSAdapter], make_http: Any
) -> None:
    adapter = adapter_class(make_http(lambda _: httpx.Response(404)))
    with pytest.raises(BoardNotFound):
        adapter.fetch_jobs("missing")


@pytest.mark.parametrize("adapter_class", ADAPTER_CLASSES)
def test_unverified_adapter_is_disabled(
    adapter_class: type[ATSAdapter], make_http: Any
) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("Disabled adapters must not make requests")

    adapter = adapter_class(make_http(unexpected))
    adapter.enabled = False
    result = adapter.probe("sample")
    assert result.status == "disabled"
    assert result.error
    with pytest.raises(RuntimeError):
        adapter.fetch_jobs("sample")


def test_smartrecruiters_paginates_raw_jobs(
    recorded: dict[str, Any], make_http: Any
) -> None:
    original = recorded["smartrecruiters"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(
            200,
            json={
                "content": [original["content"][offset]],
                "totalFound": 2,
                "offset": offset,
                "limit": 1,
            },
        )

    adapter = SmartRecruitersAdapter(make_http(handler))
    assert adapter.fetch_jobs("StandardBankGroup") == original["content"]
    assert seen[-1].endswith("?offset=1&limit=100")


def test_smartrecruiters_incomplete_page_fails(make_http: Any) -> None:
    adapter = SmartRecruitersAdapter(
        make_http(
            lambda _: httpx.Response(
                200,
                json={
                    "content": [],
                    "totalFound": 3,
                    "offset": 0,
                    "limit": 100,
                },
            )
        )
    )
    assert adapter.probe("sample").status == "error"


def test_smartrecruiters_changed_total_fails(
    recorded: dict[str, Any], make_http: Any
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(
            200,
            json={
                "content": [recorded["smartrecruiters"]["content"][offset]],
                "offset": offset,
                "limit": 1,
                "totalFound": 2 if offset == 0 else 3,
            },
        )

    with pytest.raises(ValueError, match="total changed"):
        SmartRecruitersAdapter(make_http(handler)).fetch_jobs("sample")


def test_job_title_and_description_are_not_identity(
    recorded: dict[str, Any], make_http: Any
) -> None:
    payload = deepcopy(recorded["lever"])
    payload[0]["descriptionPlain"] = (
        "FairMoney is mentioned in an unrelated employer's job"
    )
    result = ADAPTER_CLASSES[1](
        make_http(lambda _: httpx.Response(200, json=payload))
    ).probe("fairmoney")
    assert result.company_names == []
    assert result.company_domains == []


@pytest.mark.parametrize("adapter_class", ADAPTER_CLASSES)
def test_slug_cannot_inject_paths(
    adapter_class: type[ATSAdapter], make_http: Any
) -> None:
    adapter = adapter_class(make_http(lambda _: httpx.Response(200, json={})))
    assert adapter.probe("../wrong?token=secret").status == "error"
