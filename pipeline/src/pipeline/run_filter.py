"""Reclassify stored jobs without fetching sources or changing their contents."""

import argparse
from collections import Counter
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

from pipeline.detect_ats import REPO_ROOT
from pipeline.detector import RunRecorder
from pipeline.filters import JobFilter
from pipeline.http import HTTPClient
from pipeline.models import Job
from pipeline.storage import StoreError, SupabaseStore


class FilterStore(Protocol):
    def rows(self) -> list[dict[str, Any]]: ...
    def update(self, row: dict[str, Any], job: Job) -> bool: ...


class SupabaseFilterStore:
    def __init__(self, store: SupabaseStore) -> None:
        self.store = store

    def rows(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        columns = "id," + ",".join(field.name for field in fields(Job))
        while True:
            try:
                rows = (
                    self.store.client.table("jobs")
                    .select(columns)
                    .order("id")
                    .range(len(result), len(result) + 499)
                    .execute()
                    .data
                )
            except Exception as exc:
                raise StoreError(
                    "Stored-job read failed (" + type(exc).__name__ + ")"
                ) from None
            result.extend(rows)
            if len(rows) < 500:
                return result

    def update(self, row: dict[str, Any], job: Job) -> bool:
        try:
            # Guard against concurrent fetches/reclassification. Only filter fields
            # are written; descriptions, resume and notification state stay intact.
            rows = (
                self.store.client.table("jobs")
                .update(
                    {
                        "filter_result": job.filter_result,
                        "filter_reason": job.filter_reason,
                    }
                )
                .eq("id", row["id"])
                .eq("content_hash", row["content_hash"])
                .eq("filter_result", row["filter_result"])
                .eq("filter_reason", row["filter_reason"])
                .execute()
                .data
            )
            return bool(rows)
        except Exception as exc:
            raise StoreError(
                "Stored-job filter update failed (" + type(exc).__name__ + ")"
            ) from None


def stored_job(row: dict[str, Any]) -> Job:
    values = {field.name: row[field.name] for field in fields(Job) if field.name in row}
    for key in ("posted_at", "first_seen_at"):
        if isinstance(values.get(key), str):
            values[key] = datetime.fromisoformat(values[key])
    return Job(**values)


@dataclass
class FilterSummary:
    scanned: int
    category_changed: int
    reason_only: int
    conflicts: int
    failures: int
    before: dict[str, int]
    after: dict[str, int]
    transitions: dict[str, int]


def run_filter(
    store: FilterStore,
    filters: JobFilter,
    recorder: RunRecorder,
    *,
    apply: bool = False,
) -> FilterSummary:
    rows = store.rows()
    before = Counter(row["filter_result"] for row in rows)
    after = before.copy()
    transitions: Counter[str] = Counter()
    reason_only = conflicts = failures = 0
    for row in rows:
        job = filters.classify(stored_job(row))
        old = row["filter_result"]
        if (old, row["filter_reason"]) == (job.filter_result, job.filter_reason):
            continue
        event = {
            "event": "filter_job",
            "job_id": row["id"],
            "before": old,
            "after": job.filter_result,
        }
        try:
            if apply and not store.update(row, job):
                conflicts += 1
                recorder.write({**event, "status": "conflict"})
                continue
        except StoreError as exc:
            failures += 1
            recorder.write({**event, "status": "error", "error": str(exc)})
            continue
        if old != job.filter_result:
            transitions[f"{old} -> {job.filter_result}"] += 1
            after[old] -= 1
            after[job.filter_result] += 1
        else:
            reason_only += 1
        recorder.write({**event, "status": "updated" if apply else "preview"})
    summary = FilterSummary(
        len(rows),
        sum(transitions.values()),
        reason_only,
        conflicts,
        failures,
        dict(before),
        dict(after),
        dict(transitions),
    )
    recorder.write({"event": "filter_run", "applied": apply, **summary.__dict__})
    return summary


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(
        description="Reclassify stored jobs with editable YAML rules"
    )
    parser.add_argument("--filters", type=Path, default=REPO_ROOT / "data/filters.yaml")
    parser.add_argument(
        "--apply", action="store_true", help="Write filter fields; otherwise preview"
    )
    parser.add_argument(
        "--report-file", type=Path, default=REPO_ROOT / "data/filter-runs.jsonl"
    )
    args = parser.parse_args(argv)
    store: SupabaseStore | None = None
    try:
        filters = JobFilter(args.filters)
        with HTTPClient() as http:
            store = SupabaseStore.from_environment(http)
            summary = run_filter(
                SupabaseFilterStore(store),
                filters,
                RunRecorder(args.report_file),
                apply=args.apply,
            )
        print(
            f"Filter {'applied' if args.apply else 'preview'}: {summary.scanned} jobs; "
            f"{summary.category_changed} changed category; "
            f"{summary.reason_only} reason-only updates; "
            f"{summary.conflicts} conflicts; {summary.failures} errors"
        )
        for transition, count in sorted(summary.transitions.items()):
            print(f"{transition}: {count}")
        print(f"Categories: {summary.after}")
        return 1 if summary.conflicts or summary.failures else 0
    except (OSError, ValueError, StoreError) as exc:
        print(
            "Filter run failed: "
            + (str(exc) if isinstance(exc, StoreError) else type(exc).__name__)
        )
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
