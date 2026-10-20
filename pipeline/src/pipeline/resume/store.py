"""Private storage and claim RPCs, using the existing isolated Supabase session."""

from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError, field_validator

from pipeline.resume.schema import StrictModel
from pipeline.storage import StoreError, SupabaseStore

BUCKET = "radar-resumes"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JSON_MIME = "application/json"


class ClaimedJob(StrictModel):
    id: str
    title: str
    company: str
    description_text: str
    content_hash: str

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("title", "company", "content_hash")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Empty claim field")
        return value


class ResumeStore(Protocol):
    def assert_private(self) -> None: ...
    def claim(self, claim_id: str, include_maybe: bool) -> dict[str, Any] | None: ...
    def upload(self, path: str, document: Path) -> None: ...
    def remove(self, path: str) -> None: ...
    def finish(
        self,
        job: dict[str, Any],
        claim_id: str,
        status: str,
        path: str | None,
        filename: str | None,
        notes: dict[str, Any] | None,
        error: str | None,
        metadata: dict[str, Any],
    ) -> bool: ...


class SupabaseResumeStore:
    def __init__(self, store: SupabaseStore) -> None:
        self.store = store

    def assert_private(self) -> None:
        try:
            bucket = self.store.client.storage.get_bucket(BUCKET)
            if bucket.public:
                raise StoreError("Resume bucket must be private")
        except StoreError:
            raise
        except Exception:
            raise StoreError(
                "Private resume bucket unavailable; apply the Stage 4 migration"
            ) from None

    def claim(self, claim_id: str, include_maybe: bool) -> dict[str, Any] | None:
        result = self.store._rpc(
            "radar_claim_tailor",
            {
                "p_claim_id": claim_id,
                "p_include_maybe": include_maybe,
            },
        )
        if result is not None and not isinstance(result, dict):
            raise StoreError("Invalid tailoring claim response")
        if result is None:
            return None
        try:
            return ClaimedJob.model_validate(result).model_dump()
        except ValidationError:
            raise StoreError("Invalid tailoring claim fields") from None

    def upload(self, path: str, document: Path) -> None:
        if not document.is_file() or document.stat().st_size > 8_388_608:
            raise StoreError("Resume file missing or exceeds private bucket limit")
        self.assert_private()
        try:
            self.store.client.storage.from_(BUCKET).upload(
                path,
                document.read_bytes(),
                file_options={"content-type": DOCX_MIME, "upsert": "false"},
            )
        except Exception:
            raise StoreError("Private resume upload failed") from None

    def upload_input(self, path: str, document: Path) -> None:
        """Upload a private pipeline input without exposing its contents."""
        parts = path.split("/")
        if (
            len(parts) != 2
            or parts[0] != "pipeline-inputs"
            or parts[1] in {"", ".", ".."}
            or document.suffix.lower() not in {".json", ".docx"}
            or not document.is_file()
            or document.stat().st_size > 8_388_608
        ):
            raise StoreError("Resume input missing or invalid")
        self.assert_private()
        mime = DOCX_MIME if document.suffix.lower() == ".docx" else JSON_MIME
        try:
            self.store.client.storage.from_(BUCKET).upload(
                path,
                document.read_bytes(),
                file_options={"content-type": mime, "upsert": "true"},
            )
        except Exception:
            raise StoreError("Private resume input upload failed") from None

    def download_input(self, path: str, destination: Path) -> None:
        """Download one allowlisted input object to a caller-owned temp folder."""
        parts = path.split("/")
        if (
            len(parts) != 2
            or parts[0] != "pipeline-inputs"
            or parts[1] in {"", ".", ".."}
            or Path(parts[1]).suffix.lower() not in {".json", ".docx"}
        ):
            raise StoreError("Invalid private resume input path")
        self.assert_private()
        try:
            content = self.store.client.storage.from_(BUCKET).download(path)
        except Exception:
            raise StoreError("Private resume input download failed") from None
        if not isinstance(content, bytes) or not content or len(content) > 8_388_608:
            raise StoreError("Private resume input is empty or exceeds bucket limit")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        except OSError:
            raise StoreError("Private resume input could not be staged") from None

    def remove(self, path: str) -> None:
        try:
            self.store.client.storage.from_(BUCKET).remove([path])
        except Exception:
            raise StoreError("Orphan resume cleanup failed") from None

    def finish(
        self,
        job: dict[str, Any],
        claim_id: str,
        status: str,
        path: str | None,
        filename: str | None,
        notes: dict[str, Any] | None,
        error: str | None,
        metadata: dict[str, Any],
    ) -> bool:
        result = self.store._rpc(
            "radar_finish_tailor",
            {
                "p_job_id": job["id"],
                "p_claim_id": claim_id,
                "p_content_hash": job["content_hash"],
                "p_status": status,
                "p_resume_path": path,
                "p_resume_filename": filename,
                "p_match_notes": notes,
                "p_error": error,
                "p_metadata": metadata,
            },
        )
        if type(result) is not bool:
            raise StoreError("Invalid tailoring completion response")
        return result

    def signed_url(self, path: str, seconds: int = 60) -> str:
        if not 1 <= seconds <= 300:
            raise ValueError(
                "Resume download URL expiry must be between 1 and 300 seconds"
            )
        self.assert_private()
        try:
            result = self.store.client.storage.from_(BUCKET).create_signed_url(
                path, seconds
            )
            return result["signedURL"]
        except Exception:
            raise StoreError("Private resume download URL failed") from None
