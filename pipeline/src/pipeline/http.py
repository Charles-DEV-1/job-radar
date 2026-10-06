"""Shared synchronous HTTP transport with bounded retries and host pacing."""

import time
from collections.abc import Callable
from typing import Self
from urllib.parse import urlsplit

import httpx

USER_AGENT = "JobRadar/0.1 (private single-user ATS discovery; public endpoints only)"
FORBIDDEN_HOSTS = ("linkedin.com", "indeed.com", "indeed.co.uk")


def checked_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or not host or parsed.username:
        raise ValueError("Expected a public HTTP(S) URL without credentials")
    if any(
        host == blocked or host.endswith("." + blocked) for blocked in FORBIDDEN_HOSTS
    ):
        raise ValueError(f"Automated access is forbidden for {host}")
    return host


class HTTPClient:
    def __init__(
        self,
        *,
        timeout: float = 10,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0 or retries < 0:
            raise ValueError("timeout must be positive and retries nonnegative")
        self.client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"},
        )
        self.retries = retries
        self.clock = clock
        self.sleep = sleep
        self.last_request: dict[str, float] = {}
        self.host_intervals: dict[str, float] = {}

    def set_host_interval(self, url: str, interval: float) -> None:
        host = checked_url(url)
        self.host_intervals[host] = max(1, interval, self.host_intervals.get(host, 1))

    def get(self, url: str) -> httpx.Response:
        host = checked_url(url)
        for attempt in range(self.retries + 1):
            if host in self.last_request:
                wait = self.host_intervals.get(host, 1) - (
                    self.clock() - self.last_request[host]
                )
                if wait > 0:
                    self.sleep(wait)
            self.last_request[host] = self.clock()
            try:
                response = self.client.get(url)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                self.sleep(0.5 * 2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retries:
                    delay = 0.5 * 2**attempt
                    try:
                        delay = max(
                            delay, min(float(response.headers["Retry-After"]), 30)
                        )
                    except (KeyError, ValueError):
                        pass
                    self.sleep(delay)
                    continue
            return response
        raise RuntimeError("HTTP retry loop exhausted")

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
