"""Health-only check used by the independent daily scheduler."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from pipeline.detect_ats import REPO_ROOT
from pipeline.detector import RunRecorder
from pipeline.http import HTTPClient
from pipeline.notifications import (
    ResendMailer,
    discover_health_issues,
    health_email,
    health_idempotency_key,
    mark_health_alerted,
    sync_health_state,
)
from pipeline.storage import StoreError, SupabaseStore


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Evaluate and report Job Radar health")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report-file", type=Path, default=REPO_ROOT / "data/health-runs.jsonl"
    )
    args = parser.parse_args(argv)
    store: SupabaseStore | None = None
    mailer: ResendMailer | None = None
    try:
        with HTTPClient() as http:
            store = SupabaseStore.from_environment(http)
            now = datetime.now(UTC)
            active = discover_health_issues(store.client, now)
            if args.dry_run:
                print(
                    f"Health dry-run: {len(active)} active issue(s); no writes or email"
                )
                RunRecorder(args.report_file).write(
                    {"event": "health_check", "dry_run": True, "issues": len(active)}
                )
                return 0
            alert_active, recovered = sync_health_state(store.client, active, now)
            if alert_active or recovered:
                mailer = ResendMailer.from_environment()
                subject, body = health_email(alert_active, recovered)
                mailer.send(
                    subject, body, health_idempotency_key(alert_active, recovered)
                )
                mark_health_alerted(store.client, alert_active, recovered, now)
            RunRecorder(args.report_file).write(
                {
                    "event": "health_check",
                    "dry_run": False,
                    "issues": len(active),
                    "alerts": len(alert_active),
                    "recovered": len(recovered),
                }
            )
            print(f"Health check complete: {len(active)} active issue(s)")
            return 0
    except (OSError, ValueError, StoreError) as exc:
        safe = (
            str(exc)
            if isinstance(exc, (ValueError, StoreError))
            else type(exc).__name__
        )
        print(f"Health check failed: {safe}")
        return 2
    finally:
        if mailer:
            mailer.close()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
