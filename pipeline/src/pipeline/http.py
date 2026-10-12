"""Shared synchronous HTTP transport with bounded retries and host pacing."""

import threading
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
        self._registry_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def set_host_interval(self, url: str, interval: float) -> None:
        host = checked_url(url)
        with self._registry_lock:
            self.host_intervals[host] = max(
                1, interval, self.host_intervals.get(host, 1)
            )

    def _pace(self, host: str, deadline: float | None = None) -> None:
        with self._registry_lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        acquired = (
            lock.acquire(timeout=max(0, deadline - time.monotonic()))
            if deadline
            else lock.acquire()
        )
        if not acquired:
            raise TimeoutError("Page deadline exceeded while waiting for host")
        try:
            if host in self.last_request:
                wait = self.host_intervals.get(host, 1) - (
                    self.clock() - self.last_request[host]
                )
                if wait > 0:
                    if deadline and time.monotonic() + wait >= deadline:
                        raise TimeoutError("Page deadline exceeded while pacing")
                    self.sleep(wait)
            self.last_request[host] = self.clock()
        finally:
            lock.release()

    def get(self, url: str, *, deadline: float | None = None) -> httpx.Response:
        host = checked_url(url)
        for attempt in range(self.retries + 1):
            self._pace(host, deadline)
            try:
                if deadline is None:
                    response = self.client.get(url)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Page deadline exceeded")
                    # Stream with a size cap and a caller-enforced wall deadline.
                    with self.client.stream(
                        "GET", url, timeout=min(10, remaining)
                    ) as stream:
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in stream.iter_bytes():
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Page deadline exceeded")
                            size += len(chunk)
                            if size > 8_000_000:
                                raise ValueError("Page response exceeds size limit")
                            chunks.append(chunk)
                        response = httpx.Response(
                            stream.status_code,
                            headers={
                                k: v
                                for k, v in stream.headers.items()
                                if k.lower()
                                not in {"content-encoding", "content-length"}
                            },
                            content=b"".join(chunks),
                            request=stream.request,
                        )
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                self._backoff(0.5 * 2**attempt, deadline)
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
                    self._backoff(delay, deadline)
                    continue
            return response
        raise RuntimeError("HTTP retry loop exhausted")

    def _backoff(self, delay: float, deadline: float | None) -> None:
        if deadline and time.monotonic() + delay >= deadline:
            raise TimeoutError("Page deadline exceeded during retry")
        self.sleep(delay)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
