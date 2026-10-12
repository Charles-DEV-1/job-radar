"""Service-role Supabase RPCs; each successful source snapshot commits atomically."""

import base64
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Protocol, Self

import httpx
from postgrest.exceptions import APIError
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

from pipeline.http import USER_AGENT, HTTPClient, checked_url
from pipeline.models import FetchCounts, Job, SourceConfig


class StoreError(Exception):
    """A sanitized storage failure safe to report without leaking request payloads."""


class Store(Protocol):
    def sync_sources(
        self, sources: list[SourceConfig]
    ) -> dict[tuple[str, str], str]: ...
    def start_run(self, source_id: str, run_id: str, started_at: datetime) -> None: ...
    def monitor_state(self, source_id: str) -> dict[str, Any]: ...
    def complete_monitor(
        self,
        run_id: str,
        jobs: list[Job],
        observed_at: datetime,
        duration_ms: int,
        state: dict[str, Any],
        expected_hash: str | None,
        metadata: dict[str, Any],
    ) -> FetchCounts: ...
    def run_metadata(self, run_id: str, metadata: dict[str, Any]) -> None: ...
    def complete_run(
        self, run_id: str, jobs: list[Job], observed_at: datetime, duration_ms: int
    ) -> FetchCounts: ...
    def fail_run(
        self,
        run_id: str,
        error: str,
        duration_ms: int,
        completed_at: datetime,
        jobs_found: int = 0,
    ) -> None: ...


def job_record(job: Job) -> dict[str, Any]:
    record = asdict(job)
    record["first_seen_at"] = job.first_seen_at.isoformat()
    record["posted_at"] = job.posted_at.isoformat() if job.posted_at else None
    return record


class PacedTransport(httpx.BaseTransport):
    def __init__(
        self, limiter: HTTPClient, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.limiter = limiter
        self.transport = transport or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.limiter._pace(checked_url(str(request.url)))
        return self.transport.handle_request(request)

    def close(self) -> None:
        self.transport.close()


def require_service_key(key: str) -> None:
    if key.startswith("sb_secret_"):
        return
    try:
        segment = key.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        )
        if payload.get("role") == "service_role":
            return
    except (ValueError, IndexError, TypeError):
        pass
    raise ValueError("SUPABASE_SERVICE_KEY must be a service-role JWT or secret key")


class SupabaseStore:
    def __init__(self, client: Client, session: httpx.Client | None = None) -> None:
        self.client = client
        self.session = session

    @classmethod
    def from_environment(cls, limiter: HTTPClient) -> Self:
        url, key = (
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_SERVICE_KEY", ""),
        )
        if not url or not key:
            raise ValueError(
                "Set SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment or .env"
            )
        checked_url(url)
        require_service_key(key)
        # Separate HTTP session: Supabase credentials can never attach to ATS requests.
        session = httpx.Client(
            timeout=30,
            transport=PacedTransport(limiter),
            headers={"User-Agent": USER_AGENT},
        )
        options = SyncClientOptions(
            httpx_client=session,
            auto_refresh_token=False,
            persist_session=False,
            postgrest_client_timeout=30,
        )
        return cls(create_client(url, key, options=options), session)

    def _rpc(self, name: str, params: dict[str, Any]) -> Any:
        for attempt in range(3):
            try:
                return self.client.rpc(name, params).execute().data
            except (httpx.TransportError, APIError) as exc:
                code = str(getattr(exc, "code", ""))
                retryable = isinstance(exc, httpx.TransportError) or code in {
                    "429",
                    "500",
                    "502",
                    "503",
                    "504",
                }
                if retryable and attempt < 2:
                    time.sleep(0.5 * 2**attempt)
                    continue
                suffix = f", code {code}" if code and code.isalnum() else ""
                raise StoreError(
                    f"Supabase {name} failed ({type(exc).__name__}{suffix})"
                ) from None
        raise StoreError("Supabase request exhausted retries")

    def sync_sources(self, sources: list[SourceConfig]) -> dict[tuple[str, str], str]:
        rows = self._rpc(
            "radar_sync_sources", {"p_sources": [asdict(source) for source in sources]}
        )
        if not isinstance(rows, list):
            raise StoreError("Supabase returned an invalid source registry")
        return {(row["ats"], row["slug"]): row["id"] for row in rows}

    def start_run(self, source_id: str, run_id: str, started_at: datetime) -> None:
        self._rpc(
            "radar_start_run",
            {
                "p_source_id": source_id,
                "p_run_id": run_id,
                "p_started_at": started_at.isoformat(),
            },
        )

    def monitor_state(self, source_id: str) -> dict[str, Any]:
        result = self._rpc("radar_monitor_state", {"p_source_id": source_id})
        if not isinstance(result, dict):
            raise StoreError("Supabase returned invalid monitor state")
        return result

    def complete_monitor(
        self,
        run_id: str,
        jobs: list[Job],
        observed_at: datetime,
        duration_ms: int,
        state: dict[str, Any],
        expected_hash: str | None,
        metadata: dict[str, Any],
    ) -> FetchCounts:
        result = self._rpc(
            "radar_commit_monitor",
            {
                "p_run_id": run_id,
                "p_jobs": [job_record(job) for job in jobs],
                "p_observed_at": observed_at.isoformat(),
                "p_duration_ms": duration_ms,
                "p_state": state,
                "p_expected_hash": expected_hash,
                "p_metadata": metadata,
            },
        )
        if not isinstance(result, dict):
            raise StoreError("Supabase returned invalid monitor counts")
        return FetchCounts(**result)

    def run_metadata(self, run_id: str, metadata: dict[str, Any]) -> None:
        self._rpc("radar_run_metadata", {"p_run_id": run_id, "p_metadata": metadata})

    def complete_run(
        self, run_id: str, jobs: list[Job], observed_at: datetime, duration_ms: int
    ) -> FetchCounts:
        counts = self._rpc(
            "radar_commit_snapshot",
            {
                "p_run_id": run_id,
                "p_jobs": [job_record(job) for job in jobs],
                "p_observed_at": observed_at.isoformat(),
                "p_duration_ms": duration_ms,
            },
        )
        if not isinstance(counts, dict):
            raise StoreError("Supabase returned invalid snapshot counts")
        return FetchCounts(**counts)

    def fail_run(
        self,
        run_id: str,
        error: str,
        duration_ms: int,
        completed_at: datetime,
        jobs_found: int = 0,
    ) -> None:
        self._rpc(
            "radar_fail_run",
            {
                "p_run_id": run_id,
                "p_error": error,
                "p_duration_ms": duration_ms,
                "p_completed_at": completed_at.isoformat(),
                "p_jobs_found": jobs_found,
            },
        )

    def close(self) -> None:
        if self.session:
            self.session.close()
