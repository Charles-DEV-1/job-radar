"""Retention cleanup for closed jobs, inactive applications, and resumes."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from dotenv import load_dotenv
from ruamel.yaml import YAML
from supabase import Client

from pipeline.detect_ats import REPO_ROOT
from pipeline.detector import RunRecorder
from pipeline.http import HTTPClient
from pipeline.resume.store import BUCKET
from pipeline.storage import StoreError, SupabaseStore


@dataclass(frozen=True)
class CleanupConfig:
    closed_new_or_skipped_days: int
    applied_rejected_silent_days: int
    open_skipped_description_days: int

    @classmethod
    def load(cls, path: Path) -> CleanupConfig:
        with path.open(encoding="utf-8") as handle:
            value = YAML(typ="safe").load(handle)
        if not isinstance(value, dict) or not isinstance(value.get("retention"), dict):
            raise ValueError("Cleanup config must contain a retention mapping")
        retention = value["retention"]
        keys = (
            "closed_new_or_skipped_days",
            "applied_rejected_silent_days",
            "open_skipped_description_days",
        )
        values: list[int] = []
        for key in keys:
            days = retention.get(key)
            if type(days) is not int or not 1 <= days <= 3650:
                raise ValueError(f"retention.{key} must be an integer from 1 to 3650")
            values.append(days)
        return cls(*values)


class CleanupStore(Protocol):
    def rows(self) -> list[dict[str, Any]]: ...
    def pending_deleted_resumes(self) -> list[dict[str, Any]]: ...
    def delete_closed(
        self, row: dict[str, Any], cutoff: datetime
    ) -> tuple[bool, str | None]: ...
    def archive(self, row: dict[str, Any], cutoff: datetime) -> bool: ...
    def clear_skipped_descriptions(
        self, row: dict[str, Any], cutoff: datetime
    ) -> bool: ...
    def delete_resume(self, path: str) -> None: ...
    def clear_archived_resume(self, job_id: str, path: str) -> bool: ...
    def finish_deleted_resume(
        self, source_id: str, external_id: str, path: str
    ) -> bool: ...


TAILORED_PATH = re.compile(r"^[0-9a-fA-F-]{36}/[0-9a-fA-F-]{36}/[A-Za-z0-9_.-]+\.docx$")


def valid_resume_path(path: str) -> bool:
    if not TAILORED_PATH.fullmatch(path) or ".." in path:
        return False
    try:
        UUID(path.split("/", maxsplit=1)[0])
        UUID(path.split("/", maxsplit=2)[1])
    except (ValueError, IndexError):
        return False
    return True


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


class SupabaseCleanupStore:
    def __init__(self, client: Client) -> None:
        self.client = client

    def rows(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        columns = (
            "id,status,closed_at,status_changed_at,resume_status,resume_path,"
            "source_id,external_id,canonical_key"
        )
        while True:
            try:
                rows = (
                    self.client.table("jobs")
                    .select(columns)
                    .in_(
                        "status",
                        [
                            "new",
                            "skipped",
                            "applied",
                            "rejected",
                            "silent",
                        ],
                    )
                    .range(len(result), len(result) + 499)
                    .execute()
                    .data
                )
            except Exception as exc:
                raise StoreError(
                    f"Cleanup job read failed ({type(exc).__name__})"
                ) from None
            result.extend(rows or [])
            if len(rows or []) < 500:
                return result

    def pending_deleted_resumes(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        while True:
            try:
                rows = (
                    self.client.table("job_cleanup_tombstones")
                    .select("source_id,external_id,resume_path")
                    .not_.is_("resume_path", "null")
                    .range(len(result), len(result) + 499)
                    .execute()
                    .data
                )
            except Exception:
                raise StoreError(
                    "Cleanup tombstone read failed; apply the Stage 7 migration"
                ) from None
            result.extend(rows or [])
            if len(rows or []) < 500:
                return result

    def delete_closed(
        self, row: dict[str, Any], cutoff: datetime
    ) -> tuple[bool, str | None]:
        try:
            result = (
                self.client.rpc(
                    "radar_cleanup_delete_closed_job",
                    {"p_job_id": row["id"], "p_closed_before": cutoff.isoformat()},
                )
                .execute()
                .data
            )
        except Exception as exc:
            raise StoreError(f"Cleanup delete failed ({type(exc).__name__})") from None
        if not isinstance(result, dict) or not isinstance(result.get("deleted"), bool):
            raise StoreError("Supabase returned an invalid cleanup delete result")
        path = result.get("resume_path")
        return result["deleted"], path if isinstance(path, str) else None

    def archive(self, row: dict[str, Any], cutoff: datetime) -> bool:
        try:
            result = (
                self.client.rpc(
                    "radar_cleanup_archive_job",
                    {
                        "p_job_id": row["id"],
                        "p_expected_status": row["status"],
                        "p_changed_before": cutoff.isoformat(),
                    },
                )
                .execute()
                .data
            )
        except Exception as exc:
            raise StoreError(f"Cleanup archive failed ({type(exc).__name__})") from None
        if type(result) is not bool:
            raise StoreError("Supabase returned an invalid archive result")
        return result

    def clear_skipped_descriptions(self, row: dict[str, Any], cutoff: datetime) -> bool:
        try:
            result = (
                self.client.rpc(
                    "radar_cleanup_clear_open_skipped",
                    {
                        "p_job_id": row["id"],
                        "p_changed_before": cutoff.isoformat(),
                    },
                )
                .execute()
                .data
            )
        except Exception as exc:
            raise StoreError(
                f"Skipped-description cleanup failed ({type(exc).__name__})"
            ) from None
        if type(result) is not bool:
            raise StoreError("Supabase returned an invalid description cleanup result")
        return result

    def delete_resume(self, path: str) -> None:
        if not valid_resume_path(path):
            raise StoreError("Refusing to remove an invalid tailored resume path")
        try:
            result = self.client.storage.from_(BUCKET).remove([path])
        except Exception as exc:
            raise StoreError(
                f"Resume file removal failed ({type(exc).__name__})"
            ) from None
        if getattr(result, "error", None):
            raise StoreError("Resume file removal failed (Storage API error)")

    def clear_archived_resume(self, job_id: str, path: str) -> bool:
        try:
            result = (
                self.client.table("jobs")
                .update({"resume_path": None})
                .eq("id", job_id)
                .eq("resume_status", "archived")
                .in_("status", ["applied", "rejected", "silent"])
                .eq("resume_path", path)
                .select("id")
                .execute()
                .data
            )
        except Exception as exc:
            raise StoreError(
                f"Archived resume cleanup failed ({type(exc).__name__})"
            ) from None
        return bool(result)

    def finish_deleted_resume(
        self, source_id: str, external_id: str, path: str
    ) -> bool:
        try:
            result = (
                self.client.rpc(
                    "radar_cleanup_finish_deleted_resume",
                    {
                        "p_source_id": source_id,
                        "p_external_id": external_id,
                        "p_resume_path": path,
                    },
                )
                .execute()
                .data
            )
        except Exception as exc:
            raise StoreError(
                f"Deleted resume cleanup acknowledgement failed ({type(exc).__name__})"
            ) from None
        if type(result) is not bool:
            raise StoreError("Supabase returned an invalid cleanup acknowledgement")
        return result


@dataclass
class CleanupSummary:
    scanned: int = 0
    closed_jobs_deleted: int = 0
    inactive_jobs_archived: int = 0
    open_skipped_descriptions_cleared: int = 0
    resumes_deleted: int = 0
    pending_resume_retries: int = 0
    conflicts: int = 0
    errors: int = 0


def _record_error(
    recorder: RunRecorder,
    summary: CleanupSummary,
    job_id: str,
    rule: str,
    exc: Exception,
) -> None:
    summary.errors += 1
    recorder.write(
        {
            "event": "cleanup_item",
            "job_id": job_id,
            "rule": rule,
            "status": "error",
            "error": str(exc),
        }
    )


def run_cleanup(
    store: CleanupStore,
    config: CleanupConfig,
    recorder: RunRecorder,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> CleanupSummary:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    closed_cutoff = current - timedelta(days=config.closed_new_or_skipped_days)
    inactive_cutoff = current - timedelta(days=config.applied_rejected_silent_days)
    skipped_cutoff = current - timedelta(days=config.open_skipped_description_days)
    rows = store.rows()
    summary = CleanupSummary(scanned=len(rows))

    # A database deletion/archive can commit before Storage responds. These
    # private path records make failed object removal safe to retry tomorrow.
    try:
        pending = store.pending_deleted_resumes()
    except StoreError as exc:
        summary.errors += 1
        recorder.write(
            {"event": "cleanup_storage_retry", "status": "error", "error": str(exc)}
        )
        pending = []
    summary.pending_resume_retries = len(pending) + sum(
        row.get("resume_status") == "archived" and bool(row.get("resume_path"))
        for row in rows
    )
    if not dry_run:
        for tombstone in pending:
            path = tombstone.get("resume_path")
            try:
                if not isinstance(path, str):
                    continue
                store.delete_resume(path)
                if store.finish_deleted_resume(
                    tombstone["source_id"], tombstone["external_id"], path
                ):
                    summary.resumes_deleted += 1
            except Exception as exc:
                _record_error(
                    recorder,
                    summary,
                    str(tombstone.get("source_id", "unknown")),
                    "retry_deleted_resume",
                    exc,
                )
        for row in rows:
            path = row.get("resume_path")
            if (
                row.get("resume_status") != "archived"
                or row.get("status") not in {"applied", "rejected", "silent"}
                or not isinstance(path, str)
            ):
                continue
            try:
                store.delete_resume(path)
                if store.clear_archived_resume(row["id"], path):
                    summary.resumes_deleted += 1
                else:
                    summary.conflicts += 1
            except Exception as exc:
                _record_error(
                    recorder, summary, row["id"], "retry_archived_resume", exc
                )

    for row in rows:
        status = row.get("status")
        closed_at = timestamp(row.get("closed_at"))
        status_changed_at = timestamp(row.get("status_changed_at"))

        if (
            status in {"new", "skipped"}
            and closed_at is not None
            and closed_at < closed_cutoff
        ):
            if dry_run:
                summary.closed_jobs_deleted += 1
                continue
            try:
                deleted, path = store.delete_closed(row, closed_cutoff)
                if not deleted:
                    summary.conflicts += 1
                    continue
                summary.closed_jobs_deleted += 1
                if path:
                    store.delete_resume(path)
                    if store.finish_deleted_resume(
                        row["source_id"], row["external_id"], path
                    ):
                        summary.resumes_deleted += 1
            except Exception as exc:
                _record_error(recorder, summary, row["id"], "delete_closed", exc)
            continue

        if (
            status in {"applied", "rejected", "silent"}
            and row.get("resume_status") != "archived"
        ):
            if status_changed_at is None or status_changed_at >= inactive_cutoff:
                continue
            if dry_run:
                summary.inactive_jobs_archived += 1
                continue
            try:
                if not store.archive(row, inactive_cutoff):
                    summary.conflicts += 1
                    continue
                summary.inactive_jobs_archived += 1
                path = row.get("resume_path")
                if isinstance(path, str):
                    store.delete_resume(path)
                    if store.clear_archived_resume(row["id"], path):
                        summary.resumes_deleted += 1
                    else:
                        summary.conflicts += 1
            except Exception as exc:
                _record_error(recorder, summary, row["id"], "archive_inactive", exc)
            continue

        if (
            status == "skipped"
            and row.get("closed_at") is None
            and status_changed_at is not None
            and status_changed_at < skipped_cutoff
        ):
            if dry_run:
                summary.open_skipped_descriptions_cleared += 1
                continue
            try:
                if store.clear_skipped_descriptions(row, skipped_cutoff):
                    summary.open_skipped_descriptions_cleared += 1
            except Exception as exc:
                _record_error(
                    recorder, summary, row["id"], "clear_open_skipped_description", exc
                )

    record = {"event": "cleanup_run", "dry_run": dry_run, **summary.__dict__}
    recorder.write(record)
    return summary


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(
        description="Apply configured job and resume retention"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "data/cleanup.yaml")
    parser.add_argument(
        "--report-file", type=Path, default=REPO_ROOT / "data/cleanup-runs.jsonl"
    )
    args = parser.parse_args(argv)
    store: SupabaseStore | None = None
    try:
        config = CleanupConfig.load(args.config)
        recorder = RunRecorder(args.report_file)
        with HTTPClient() as http:
            store = SupabaseStore.from_environment(http)
            summary = run_cleanup(
                SupabaseCleanupStore(store.client),
                config,
                recorder,
                dry_run=args.dry_run,
            )
        label = "dry-run" if args.dry_run else "complete"
        print(
            f"Cleanup {label}: scanned {summary.scanned}; "
            f"closed new/skipped deleted {summary.closed_jobs_deleted}; "
            f"applied/rejected/silent archived {summary.inactive_jobs_archived}; "
            f"open skipped descriptions cleared "
            f"{summary.open_skipped_descriptions_cleared}; "
            f"resumes deleted {summary.resumes_deleted}; "
            f"pending resume retries {summary.pending_resume_retries}; "
            f"conflicts {summary.conflicts}; errors {summary.errors}"
        )
        return 1 if summary.conflicts or summary.errors else 0
    except (OSError, ValueError, StoreError) as exc:
        message = (
            str(exc)
            if isinstance(exc, (ValueError, StoreError))
            else type(exc).__name__
        )
        print(f"Cleanup failed: {message}")
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
