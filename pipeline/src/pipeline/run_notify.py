"""Send new-job and separately throttled health emails through Resend."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from pipeline.detect_ats import REPO_ROOT
from pipeline.detector import RunRecorder
from pipeline.http import HTTPClient
from pipeline.notifications import (
    NotificationError,
    ResendMailer,
    discover_health_issues,
    dry_run_jobs,
    health_email,
    health_idempotency_key,
    mark_health_alerted,
    notification_email,
    send_pending_notification,
    sync_health_state,
)
from pipeline.storage import StoreError, SupabaseStore


def _dashboard_url() -> str:
    value = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise NotificationError("Set DASHBOARD_URL to the private dashboard URL")
    return value + "/jobs?new=1"


def _dry_health(
    client: Any, now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_map = discover_health_issues(client, now)
    records = client.table("health_events").select("*").execute().data or []
    by_key = {str(row.get("issue_key")): row for row in records}
    active, recovered = [], []
    for key, issue in active_map.items():
        old = by_key.get(key)
        if (
            old is None
            or not old.get("last_alerted_at")
            or datetime.fromisoformat(
                str(old["last_alerted_at"]).replace("Z", "+00:00")
            )
            <= now - timedelta(hours=24)
        ):
            active.append(
                {
                    "issue_key": key,
                    **issue,
                    "opened_at": (old or {}).get("opened_at", now.isoformat()),
                }
            )
    for key, row in by_key.items():
        if row.get("resolved_at") is None and key not in active_map:
            last_alerted = row.get("last_alerted_at")
            cooldown_passed = not last_alerted or datetime.fromisoformat(
                str(last_alerted).replace("Z", "+00:00")
            ) <= now - timedelta(hours=24)
            if not row.get("recovered_notice_sent_at") and cooldown_passed:
                recovered.append({**row, "resolved_at": now.isoformat()})
    return active, recovered


def _record(recorder: RunRecorder, **record: Any) -> None:
    recorder.write(
        {"event": "notify_run", "at": datetime.now(UTC).isoformat(), **record}
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Send new-job and health emails")
    parser.add_argument(
        "--dry-run", action="store_true", help="print emails without sending or writing"
    )
    parser.add_argument("--grace-minutes", type=int, default=None)
    parser.add_argument(
        "--report-file", type=Path, default=REPO_ROOT / "data/notify-runs.jsonl"
    )
    args = parser.parse_args(argv)
    grace = (
        args.grace_minutes
        if args.grace_minutes is not None
        else int(os.environ.get("NOTIFY_RESUME_GRACE_MINUTES", "30"))
    )
    if not 0 <= grace <= 1440:
        print("Notification setup failed: grace period must be 0 to 1440 minutes")
        return 2

    store: SupabaseStore | None = None
    mailer: ResendMailer | None = None
    recorder = RunRecorder(args.report_file)
    failed = False
    try:
        with HTTPClient() as http:
            store = SupabaseStore.from_environment(http)
            now = datetime.now(UTC)
            if args.dry_run:
                jobs, check_fit_count = dry_run_jobs(store.client, grace)
                if jobs:
                    subject, body = notification_email(
                        jobs, _dashboard_url(), check_fit_count
                    )
                    print(f"DRY RUN — {subject}\n{body}")
                else:
                    print("no new jobs")
                active, recovered = _dry_health(store.client, now)
                if active or recovered:
                    subject, body = health_email(active, recovered)
                    print(f"DRY RUN — {subject}\n{body}")
                _record(
                    recorder,
                    dry_run=True,
                    new_jobs=len(jobs),
                    check_fit_count=check_fit_count,
                    health_issues=len(active),
                    recovered=len(recovered),
                )
                return 0

            # The DB claim is durable; if sending or finalization fails it is
            # reused on the next invocation with the same payload and key.
            mailer_box: list[ResendMailer] = []

            def send_job_email(subject: str, body: str, key: str) -> None:
                if not mailer_box:
                    mailer_box.append(ResendMailer.from_environment())
                mailer_box[0].send(subject, body, key)

            try:
                sent_count = send_pending_notification(
                    store.client, grace, _dashboard_url, send_job_email
                )
                if sent_count is not None:
                    mailer = mailer_box[0]
                    print(f"Sent new-job email for {sent_count} jobs")
                    _record(
                        recorder,
                        dry_run=False,
                        new_jobs=sent_count,
                        status="sent",
                    )
                else:
                    print("no new jobs")
                    _record(recorder, dry_run=False, new_jobs=0, status="no_new_jobs")
            except Exception:
                failed = True
                _record(
                    recorder,
                    dry_run=False,
                    new_jobs=None,
                    status="error",
                    error="job email delivery/finalization failed",
                )
                print("New-job email failed; jobs remain unnotified and will retry")
            if mailer_box and mailer is None:
                mailer = mailer_box[0]

            # Health evaluation is independent, so a job-email problem cannot
            # suppress a source or pipeline warning.
            current = discover_health_issues(store.client, now)
            alert_active, alert_recovered = sync_health_state(
                store.client, current, now
            )
            if alert_active or alert_recovered:
                mailer = mailer or ResendMailer.from_environment()
                subject, body = health_email(alert_active, alert_recovered)
                key = health_idempotency_key(alert_active, alert_recovered)
                try:
                    mailer.send(subject, body, key)
                    mark_health_alerted(
                        store.client, alert_active, alert_recovered, now
                    )
                    print("Sent Job Radar health warning")
                except Exception:
                    failed = True
                    print("Health email failed; alert state remains due for retry")
                _record(
                    recorder,
                    dry_run=False,
                    health_issues=len(alert_active),
                    recovered=len(alert_recovered),
                    status="sent" if not failed else "error",
                )
            else:
                print("No health alerts due")
        return 1 if failed else 0
    except (OSError, ValueError, StoreError, NotificationError) as exc:
        _record(
            recorder,
            dry_run=args.dry_run,
            status="setup_error",
            error=str(exc)
            if isinstance(exc, (ValueError, NotificationError, StoreError))
            else type(exc).__name__,
        )
        print(f"Notification setup failed: {exc}")
        return 2
    except Exception as exc:
        _record(
            recorder,
            dry_run=args.dry_run,
            status="error",
            error=type(exc).__name__,
        )
        print(f"Notification run failed: {type(exc).__name__}")
        return 1
    finally:
        if mailer:
            mailer.close()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
