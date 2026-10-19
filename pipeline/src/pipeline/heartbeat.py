"""Persist and evaluate orchestration heartbeats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID


def record_heartbeat(
    client: Any,
    run_id: UUID,
    steps: dict[str, str],
    completed_at: datetime | None = None,
) -> None:
    client.table("heartbeats").insert(
        {
            "run_id": str(run_id),
            "completed_at": (completed_at or datetime.now(UTC)).isoformat(),
            "steps": steps,
        }
    ).execute()


def stale_heartbeat_issue(
    client: Any,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=3),
) -> dict[str, dict[str, Any]]:
    """Return a stable health issue if no successful heartbeat exists recently."""
    current = now or datetime.now(UTC)
    result = (
        client.table("heartbeats")
        .select("completed_at")
        .order("completed_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    last = rows[0].get("completed_at") if rows else None
    stale = not last
    if last:
        parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        stale = parsed < current - stale_after
    if not stale:
        return {}
    message = (
        "No successful pipeline heartbeat has been recorded."
        if not last
        else "The last successful pipeline heartbeat is more than three hours old."
    )
    return {
        "pipeline_heartbeat:stale": {
            "issue_type": "pipeline_heartbeat_stale",
            "source_id": None,
            "details": {"message": message},
        }
    }
