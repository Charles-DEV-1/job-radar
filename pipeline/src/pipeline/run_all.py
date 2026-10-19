"""Hourly orchestration entry point for the complete Job Radar pipeline."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from pipeline.detect_ats import REPO_ROOT
from pipeline.detector import RunRecorder
from pipeline.heartbeat import record_heartbeat
from pipeline.http import HTTPClient
from pipeline.llm.pacing import (
    begin_run_call_budget,
    end_run_call_budget,
    reset_call_budget,
)
from pipeline.storage import StoreError, SupabaseStore


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Run all Job Radar pipeline stages")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit", type=int, default=int(os.environ.get("RESUME_MAX_PER_RUN", "3"))
    )
    parser.add_argument("--include-maybe", action="store_true")
    parser.add_argument(
        "--report-file", type=Path, default=REPO_ROOT / "data/run-all-runs.jsonl"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 20:
        print("Orchestration setup failed: limit must be between 1 and 20")
        return 2

    from pipeline import check_health, run_fetch, run_notify, run_tailor

    run_id = uuid4()
    recorder = RunRecorder(args.report_file)
    steps: dict[str, str] = {}
    fetch_args = ["--dry-run"] if args.dry_run else []
    tailor_args = ["--limit", str(args.limit)]
    if args.include_maybe:
        tailor_args.append("--include-maybe")
    if args.dry_run:
        tailor_args.append("--dry-run")
    notify_args = ["--dry-run"] if args.dry_run else []
    health_args = ["--dry-run"] if args.dry_run else []

    previous_flag = os.environ.get("PIPELINE_RUN_ALL")
    os.environ["PIPELINE_RUN_ALL"] = "1"
    reset_call_budget()
    begin_run_call_budget()
    try:
        for name, action in (
            ("fetch", lambda: run_fetch.main(fetch_args)),
            ("tailor", lambda: run_tailor.main(tailor_args)),
            ("notify", lambda: run_notify.main(notify_args)),
            ("health", lambda: check_health.main(health_args)),
        ):
            try:
                code = action()
                steps[name] = "ok" if code == 0 else f"failed:{code}"
            except Exception as exc:
                # Never expose exception payloads, which may contain response data.
                steps[name] = "failed:" + type(exc).__name__
                print(f"{name} step failed ({type(exc).__name__}); continuing")
        if args.dry_run:
            print("Dry-run complete: no emails or database writes were made")
            recorder.write(
                {
                    "event": "run_all",
                    "run_id": str(run_id),
                    "dry_run": True,
                    "steps": steps,
                }
            )
            return 0

        try:
            with HTTPClient() as http:
                store = SupabaseStore.from_environment(http)
                try:
                    record_heartbeat(store.client, run_id, steps, datetime.now(UTC))
                finally:
                    store.close()
        except Exception as exc:
            safe = (
                str(exc)
                if isinstance(exc, (ValueError, StoreError))
                else type(exc).__name__
            )
            print(f"Pipeline heartbeat failed; run is unusable: {safe}")
            recorder.write(
                {
                    "event": "run_all",
                    "run_id": str(run_id),
                    "dry_run": False,
                    "steps": steps,
                    "heartbeat": "failed",
                    "error": type(exc).__name__,
                }
            )
            return 2
        recorder.write(
            {
                "event": "run_all",
                "run_id": str(run_id),
                "dry_run": False,
                "steps": steps,
                "heartbeat": "recorded",
            }
        )
        print("Pipeline run complete; heartbeat recorded")
        return 0
    finally:
        end_run_call_budget()
        if previous_flag is None:
            os.environ.pop("PIPELINE_RUN_ALL", None)
        else:
            os.environ["PIPELINE_RUN_ALL"] = previous_flag


if __name__ == "__main__":
    raise SystemExit(main())
