"""Stage 6 transactional job notifications and throttled source health alerts."""

from __future__ import annotations

import hashlib
import html
import json
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

import httpx
from supabase import Client


class NotificationError(RuntimeError):
    """A safe-to-display notification setup or delivery failure."""


def _job_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise NotificationError("Supabase returned invalid notification jobs")
    return [row for row in value if isinstance(row, dict)]


def notification_email(
    jobs: list[dict[str, Any]], dashboard_url: str
) -> tuple[str, str]:
    """Build a compact email; all provider content is escaped as plain text."""
    if not jobs:
        raise ValueError("Cannot build an empty job notification")
    count = len(jobs)
    strong = sum(row.get("filter_result") == "include" for row in jobs)
    subject = (
        f"{count} new job{'s' if count != 1 else ''} found ({strong} strong matches)"
    )
    rows = []
    for row in jobs[:15]:
        company = html.escape(str(row.get("company") or "Company"))
        title = html.escape(str(row.get("title") or "Role"))
        location = html.escape(str(row.get("location") or "Location not listed"))
        badge = "Include" if row.get("filter_result") == "include" else "Maybe"
        rows.append(
            f"<li><strong>{company}</strong> — {title}<br>{location} · {badge}</li>"
        )
    remaining = count - min(count, 15)
    if remaining:
        rows.append(f"<li>+{remaining} more</li>")
    href = html.escape(dashboard_url, quote=True)
    body = (
        "<h2>New jobs found</h2>"
        f"<p>{count} open roles are ready to review.</p><ul>" + "".join(rows) + "</ul>"
        f'<p><a href="{href}" style="display:inline-block;padding:12px 18px;'
        'background:#155eef;color:#fff;text-decoration:none;border-radius:6px">'
        "Open Job Radar</a></p>"
    )
    return subject, body


def health_email(
    active: list[dict[str, Any]], recovered: list[dict[str, Any]]
) -> tuple[str, str]:
    items: list[str] = []
    if active:
        items.append("<h3>Needs attention</h3><ul>")
        for row in active:
            label = html.escape(str(row.get("issue_type", "system")))
            details = row.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            company = html.escape(str(details.get("company", "")))
            message = html.escape(str(details.get("message", "")))
            title = f"{label}: {company}" if company else label
            items.append(f"<li><strong>{title}</strong> {message}</li>")
        items.append("</ul>")
    if recovered:
        items.append("<h3>Recovered</h3><ul>")
        for row in recovered:
            details = row.get("details") or {}
            company = (
                html.escape(str(details.get("company", "")))
                if isinstance(details, dict)
                else ""
            )
            label = html.escape(str(row.get("issue_type", "system")))
            items.append(f"<li>{label}: {company or 'issue cleared'}</li>")
        items.append("</ul>")
    return "Job Radar health warning", "<h2>Job Radar health warning</h2>" + "".join(
        items
    )


class ResendMailer:
    """Small Resend client using the documented idempotency header."""

    def __init__(
        self,
        api_key: str,
        from_email: str,
        to_email: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not from_email or not to_email:
            raise NotificationError(
                "Set RESEND_API_KEY, RESEND_FROM_EMAIL and NOTIFY_EMAIL"
            )
        self.api_key = api_key
        self.from_email = from_email
        self.to_email = to_email
        self.client = client or httpx.Client(timeout=20)
        self.owns_client = client is None

    @classmethod
    def from_environment(cls) -> ResendMailer:
        return cls(
            os.environ.get("RESEND_API_KEY", ""),
            os.environ.get("RESEND_FROM_EMAIL", ""),
            os.environ.get("NOTIFY_EMAIL", ""),
        )

    def send(self, subject: str, body: str, idempotency_key: str) -> None:
        try:
            response = self.client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Idempotency-Key": idempotency_key,
                },
                json={
                    "from": self.from_email,
                    "to": [self.to_email],
                    "subject": subject,
                    "html": body,
                },
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("id"), str):
                raise NotificationError("Resend returned an invalid success response")
        except NotificationError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise NotificationError(f"Resend email delivery failed{suffix}") from None

    def close(self) -> None:
        if self.owns_client:
            self.client.close()


def claim_notification(client: Client, grace_minutes: int) -> dict[str, Any] | None:
    result = (
        client.rpc(
            "radar_claim_notification", {"p_resume_grace_minutes": grace_minutes}
        )
        .execute()
        .data
    )
    if result is None:
        return None
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        raise NotificationError("Supabase returned an invalid notification claim")
    result["jobs"] = _job_rows(result.get("jobs"))
    return result


def finish_notification(client: Client, attempt_id: str) -> None:
    client.rpc("radar_finish_notification", {"p_attempt_id": attempt_id}).execute()


def send_pending_notification(
    client: Client,
    grace_minutes: int,
    dashboard_url: str | Callable[[], str],
    send: Callable[[str, str, str], None],
) -> int | None:
    """Send and commit one claimed batch, or return None without calling send."""
    attempt = claim_notification(client, grace_minutes)
    if attempt is None:
        return None
    url = dashboard_url() if callable(dashboard_url) else dashboard_url
    subject, body = notification_email(attempt["jobs"], url)
    send(subject, body, str(attempt["idempotency_key"]))
    finish_notification(client, str(attempt["id"]))
    return len(attempt["jobs"])


def dry_run_jobs(client: Client, grace_minutes: int) -> list[dict[str, Any]]:
    rows = (
        client.table("jobs")
        .select(
            "id,company,title,location_raw,filter_result,first_seen_at,resume_status"
        )
        .is_("notified_at", "null")
        .eq("backfill", False)
        .in_("filter_result", ["include", "maybe"])
        .is_("closed_at", "null")
        .order("first_seen_at", desc=True)
        .execute()
        .data
    )
    now = datetime.now(UTC)
    eligible = []
    for row in _job_rows(rows):
        if row.get("resume_status") in {"ready", "failed"}:
            eligible.append(row)
            continue
        first_seen = datetime.fromisoformat(
            str(row["first_seen_at"]).replace("Z", "+00:00")
        )
        if first_seen <= now - timedelta(minutes=grace_minutes):
            eligible.append(row)
    return eligible


def _median_positive(values: list[int]) -> float:
    return median([value for value in values if value > 0]) if any(values) else 0


def discover_health_issues(
    client: Client, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Evaluate current health from durable source and tailoring run records."""
    current = now or datetime.now(UTC)
    issues: dict[str, dict[str, Any]] = {}
    sources = _job_rows(
        client.table("sources")
        .select("id,company_name,source_type,consecutive_failures,last_success_at")
        .eq("enabled", True)
        .execute()
        .data
    )
    runs = _job_rows(
        client.table("source_runs")
        .select("source_id,status,jobs_found,started_at,metadata,completed_at")
        .order("started_at", desc=True)
        .limit(1000)
        .execute()
        .data
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[str(run.get("source_id"))].append(run)
    for source in sources:
        source_id = str(source["id"])
        company = str(source.get("company_name", "Source"))
        failures = int(source.get("consecutive_failures") or 0)
        if failures >= 3:
            key = f"source_failures:{source_id}"
            issues[key] = {
                "issue_type": "source_failures",
                "source_id": source_id,
                "details": {
                    "company": company,
                    "message": f"{failures} consecutive failures.",
                },
            }
        history = grouped.get(source_id, [])
        successful = [row for row in history if row.get("status") == "ok"]
        if len(history) >= 3 and all(
            row.get("status") == "ok" and int(row.get("jobs_found") or 0) == 0
            for row in history[:3]
        ):
            baseline = _median_positive(
                [int(row.get("jobs_found") or 0) for row in successful[3:10]]
            )
            if baseline > 0:
                key = f"source_zero:{source_id}"
                issues[key] = {
                    "issue_type": "source_zero_jobs",
                    "source_id": source_id,
                    "details": {
                        "company": company,
                        "message": (
                            "Three successful runs returned zero jobs after a "
                            "nonzero baseline."
                        ),
                    },
                }
        for run in history[:5]:
            metadata = run.get("metadata") or {}
            warnings = (
                metadata.get("warnings", []) if isinstance(metadata, dict) else []
            )
            if any(
                "shrunk dramatically" in str(warning).lower() for warning in warnings
            ):
                key = f"page_shrink:{source_id}"
                issues[key] = {
                    "issue_type": "careers_page_shrunk",
                    "source_id": source_id,
                    "details": {
                        "company": company,
                        "message": (
                            "A careers-page snapshot shrank dramatically; the "
                            "previous baseline was retained."
                        ),
                    },
                }
                break

    ok_runs = [
        row for row in runs if row.get("status") == "ok" and row.get("completed_at")
    ]
    latest_ok = max(
        (
            datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
            for row in ok_runs
        ),
        default=None,
    )
    if latest_ok is None or latest_ok < current - timedelta(hours=3):
        message = "No successful source run has completed in more than 3 hours."
        if latest_ok:
            message = (
                f"Last successful source run completed at {latest_ok.isoformat()}."
            )
        issues["pipeline_stale"] = {
            "issue_type": "pipeline_stale",
            "source_id": None,
            "details": {"message": message},
        }

    tailoring = _job_rows(
        client.table("resume_runs")
        .select("status,completed_at")
        .not_.is_("completed_at", "null")
        .order("completed_at", desc=True)
        .limit(3)
        .execute()
        .data
    )
    if len(tailoring) == 3 and all(row.get("status") == "failed" for row in tailoring):
        issues["tailoring_failures"] = {
            "issue_type": "tailoring_failures",
            "source_id": None,
            "details": {
                "message": "The three most recent resume tailoring attempts failed."
            },
        }
    return issues


def _health_records(client: Client) -> list[dict[str, Any]]:
    return _job_rows(client.table("health_events").select("*").execute().data)


def sync_health_state(
    client: Client, active: dict[str, dict[str, Any]], now: datetime | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persist open/recovered issues and return alerts due now."""
    current = now or datetime.now(UTC)
    records = {str(row["issue_key"]): row for row in _health_records(client)}
    for key, issue in active.items():
        old = records.get(key)
        payload = {"issue_key": key, **issue, "resolved_at": None}
        if old and old.get("resolved_at") is None:
            payload["opened_at"] = old["opened_at"]
            payload["last_alerted_at"] = old.get("last_alerted_at")
            payload["recovered_notice_sent_at"] = None
        else:
            payload["opened_at"] = current.isoformat()
            payload["last_alerted_at"] = None
            payload["recovered_notice_sent_at"] = None
        client.table("health_events").upsert(payload, on_conflict="issue_key").execute()
    for key, old in records.items():
        if key not in active and old.get("resolved_at") is None:
            client.table("health_events").update(
                {"resolved_at": current.isoformat()}
            ).eq("issue_key", key).execute()

    refreshed = {str(row["issue_key"]): row for row in _health_records(client)}
    due_active, due_recovered = [], []
    for key in active:
        row = refreshed[key]
        alerted = row.get("last_alerted_at")
        if not alerted or datetime.fromisoformat(
            str(alerted).replace("Z", "+00:00")
        ) <= current - timedelta(hours=24):
            due_active.append(row)
    for key, row in refreshed.items():
        if row.get("resolved_at") and not row.get("recovered_notice_sent_at"):
            last_alerted = row.get("last_alerted_at")
            if not last_alerted or datetime.fromisoformat(
                str(last_alerted).replace("Z", "+00:00")
            ) <= current - timedelta(hours=24):
                due_recovered.append(row)
    return due_active, due_recovered


def health_idempotency_key(
    active: list[dict[str, Any]], recovered: list[dict[str, Any]]
) -> str:
    material = json.dumps(
        sorted((row["issue_key"], row.get("opened_at")) for row in active)
        + sorted((row["issue_key"], row.get("resolved_at")) for row in recovered),
        separators=(",", ":"),
    )
    return "health/" + hashlib.sha256(material.encode()).hexdigest()


def mark_health_alerted(
    client: Client,
    active: list[dict[str, Any]],
    recovered: list[dict[str, Any]],
    now: datetime | None = None,
) -> None:
    timestamp = (now or datetime.now(UTC)).isoformat()
    client.rpc(
        "radar_mark_health_alerted",
        {
            "p_active_keys": [row["issue_key"] for row in active],
            "p_recovered_keys": [row["issue_key"] for row in recovered],
            "p_alerted_at": timestamp,
        },
    ).execute()
