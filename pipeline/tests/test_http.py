from typing import Any

import httpx
import pytest
from conftest import FakeClock

from pipeline.http import USER_AGENT


def test_one_request_per_second_per_host(make_http: Any) -> None:
    timer = FakeClock()
    starts: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append((request.url.host, timer()))
        assert request.headers["User-Agent"] == USER_AGENT
        return httpx.Response(200)

    http = make_http(handler, clock=timer)
    http.get("https://one.example/a")
    http.get("https://two.example/a")
    http.get("https://one.example/b")
    http.get("https://one.example/c")
    assert starts == [
        ("one.example", 0),
        ("two.example", 0),
        ("one.example", 1),
        ("one.example", 2),
    ]


@pytest.mark.parametrize("failure", ["timeout", "503", "429"])
def test_retries_are_bounded_and_paced(failure: str, make_http: Any) -> None:
    timer = FakeClock()
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(timer())
        if len(starts) < 3:
            if failure == "timeout":
                raise httpx.ConnectTimeout("timeout", request=request)
            return httpx.Response(int(failure))
        return httpx.Response(200)

    http = make_http(handler, retries=2, clock=timer)
    assert http.get("https://one.example/").status_code == 200
    assert starts == [0, 1, 2]
    assert timer.sleeps[:2] == [0.5, 0.5]


def test_transport_error_propagates_after_retries(make_http: Any) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(httpx.ReadTimeout):
        make_http(handler, retries=2).get("https://one.example/")
    assert attempts == 3


def test_retry_after_and_longer_robots_interval(make_http: Any) -> None:
    timer = FakeClock()
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429, headers={"Retry-After": "3"})
            if calls == 1
            else httpx.Response(200)
        )

    http = make_http(handler, retries=1, clock=timer)
    http.set_host_interval("https://one.example/", 5)
    assert http.get("https://one.example/").status_code == 200
    assert timer() == 5


@pytest.mark.parametrize(
    "url", ["https://www.linkedin.com/jobs", "https://indeed.com/", "file:///secret"]
)
def test_forbidden_urls_are_rejected(url: str, make_http: Any) -> None:
    with pytest.raises(ValueError):
        make_http(lambda _: httpx.Response(200)).get(url)
