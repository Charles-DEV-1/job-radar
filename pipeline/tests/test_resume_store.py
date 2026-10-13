"""Official Storage SDK contracts with controlled responses; no live uploads."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

from pipeline.resume.store import BUCKET, DOCX_MIME, SupabaseResumeStore
from pipeline.storage import StoreError, SupabaseStore


def store_client(session: httpx.Client) -> SupabaseResumeStore:
    return SupabaseResumeStore(
        SupabaseStore(
            create_client(
                "https://project.supabase.co",
                "sb_secret_mock_key_for_tests_only",
                options=SyncClientOptions(
                    httpx_client=session,
                    persist_session=False,
                    auto_refresh_token=False,
                ),
            )
        )
    )


def bucket_response(public: bool = False) -> dict[str, Any]:
    return {
        "id": BUCKET,
        "name": BUCKET,
        "owner": "",
        "public": public,
        "created_at": "2026-10-12T00:00:00Z",
        "updated_at": "2026-10-12T00:00:00Z",
        "file_size_limit": 8388608,
        "allowed_mime_types": [DOCX_MIME],
    }


def test_private_docx_upload_and_short_lived_signed_download(tmp_path: Path) -> None:
    document = tmp_path / "Resume.docx"
    document.write_bytes(b"controlled SDK contract payload")
    path = "job/claim/Resume.docx"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=bucket_response())
        if "/sign/" in request.url.path:
            assert json.loads(request.content)["expiresIn"] == "60"
            return httpx.Response(
                200,
                json={
                    "signedURL": "/object/sign/radar-resumes/job/claim/Resume.docx"
                    "?token=controlled-test-token"
                },
            )
        if request.method == "DELETE":
            assert json.loads(request.content) == {"prefixes": [path]}
            return httpx.Response(200, json=[])
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert request.headers["x-upsert"] == "false"
        assert DOCX_MIME.encode() in request.content
        assert document.read_bytes() in request.content
        return httpx.Response(200, json={"Key": BUCKET + "/" + path})

    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        store = store_client(session)
        store.assert_private()
        store.upload(path, document)
        signed = store.signed_url(path)
        assert "/object/sign/" in signed and "token=" in signed
        store.remove(path)
        with pytest.raises(ValueError):
            store.signed_url(path, 3600)
    assert len(seen) == 6
    assert all(
        request.headers["apikey"] == "sb_secret_mock_key_for_tests_only"
        for request in seen
    )


def test_public_bucket_is_rejected_before_upload() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=bucket_response(True))
        )
    ) as session:
        with pytest.raises(StoreError, match="private"):
            store_client(session).assert_private()


def test_storage_errors_never_expose_credentials_or_resume_contents(
    tmp_path: Path,
) -> None:
    document = tmp_path / "Resume.docx"
    document.write_bytes(b"private resume content")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "statusCode": "400",
                "error": "private resume content",
                "message": "sb_secret_mock_key_for_tests_only",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(StoreError) as exc:
            store_client(session).upload("job/claim/Resume.docx", document)
    assert "private resume content" not in str(exc.value) and "sb_secret" not in str(
        exc.value
    )
