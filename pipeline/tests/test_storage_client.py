import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

from pipeline.models import FetchCounts, SourceConfig
from pipeline.storage import (
    PacedTransport,
    StoreError,
    SupabaseStore,
    require_service_key,
)


def test_official_sdk_rpc_contract_and_credential_isolation(make_http: Any) -> None:
    seen: list[str] = []
    key = "sb_secret_mock_key_for_tests_only"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["apikey"] == key
        body = json.loads(request.content)
        assert body["p_sources"][0]["ats"] == "greenhouse"
        return httpx.Response(
            200, json=[{"ats": "greenhouse", "slug": "board", "id": "source-id"}]
        )

    def public_handler(request: httpx.Request) -> httpx.Response:
        assert "apikey" not in request.headers
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={})

    public_http = make_http(public_handler)
    with httpx.Client(
        transport=PacedTransport(public_http, httpx.MockTransport(handler))
    ) as session:
        client = create_client(
            "https://project.supabase.co",
            key,
            options=SyncClientOptions(
                httpx_client=session, persist_session=False, auto_refresh_token=False
            ),
        )
        store = SupabaseStore(client)
        assert store.sync_sources(
            [SourceConfig("Company", "greenhouse", "board", "https://example.com")]
        ) == {("greenhouse", "board"): "source-id"}
        public_http.get("https://boards-api.greenhouse.io/v1/boards/board/jobs")
    assert seen == ["https://project.supabase.co/rest/v1/rpc/radar_sync_sources"]


def test_storage_error_never_logs_secret_or_job_payload(make_http: Any) -> None:
    key = "sb_secret_mock_key_for_tests_only"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "23514",
                "message": key + " full private job description",
                "details": "sensitive",
                "hint": None,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        store = SupabaseStore(
            create_client(
                "https://project.supabase.co",
                key,
                options=SyncClientOptions(httpx_client=session),
            )
        )
        with pytest.raises(StoreError) as error:
            store.fail_run("run-id", "Timeout", 1, datetime.now(UTC))
    assert key not in str(error.value) and "private job" not in str(error.value)
    assert "23514" in str(error.value)


def test_anon_and_publishable_keys_are_rejected() -> None:
    for key in ("", "sb_publishable_mock", "not-a-service-key"):
        with pytest.raises(ValueError, match="service-role"):
            require_service_key(key)
    require_service_key("sb_secret_mock_key_for_tests_only")


@pytest.mark.parametrize("source_type", ["ats", "careers_page", "feed"])
def test_source_constraint_failure_explains_monitor_migration(source_type: Any) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            400,
            json={
                "code": "23514",
                "message": "private payload",
                "details": None,
                "hint": None,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        store = SupabaseStore(
            create_client(
                "https://project.supabase.co",
                "sb_secret_mock_key_for_tests_only",
                options=SyncClientOptions(httpx_client=session),
            )
        )
        source = SourceConfig(
            "Company",
            "workable" if source_type == "ats" else source_type,
            "board",
            "https://example.com",
            source_type=source_type,
        )
        with pytest.raises(StoreError) as error:
            store.sync_sources([source])
    assert ("20261010000100_stage3_careers_monitor.sql" in str(error.value)) == (
        source_type != "ats"
    )
    assert "private payload" not in str(error.value)
    assert len(calls) == 1  # A constraint failure is not transient.


@pytest.mark.parametrize("void_status", [200, 204])
def test_sdk_snapshot_counts_and_void_results(void_status: int) -> None:
    counts = {
        "jobs_found": 0,
        "inserted_jobs": 0,
        "new_jobs": 0,
        "updated_jobs": 0,
        "duplicates": 0,
        "closed_jobs": 0,
        "backfill": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("radar_commit_snapshot"):
            return httpx.Response(200, json=counts)
        return httpx.Response(void_status, text="null" if void_status == 200 else "")

    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        store = SupabaseStore(
            create_client(
                "https://project.supabase.co",
                "sb_secret_mock_key_for_tests_only",
                options=SyncClientOptions(httpx_client=session),
            )
        )
        now = datetime.now(UTC)
        store.start_run("source-id", "run-id", now)
        assert store.complete_run("run-id", [], now, 0) == FetchCounts(**counts)
        store.fail_run("run-id", "Failed", 0, now)
