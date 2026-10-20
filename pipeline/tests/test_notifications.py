from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from test_filters import FILTERS, example_job
from test_storage_sql import prepared, snapshot, source_ids

from pipeline.dedupe import hash_job
from pipeline.filters import JobFilter
from pipeline.notifications import (
    NotificationError,
    ResendMailer,
    health_email,
    health_idempotency_key,
    mark_health_alerted,
    notification_email,
    send_pending_notification,
    sync_health_state,
)


def test_no_jobs_produces_no_email() -> None:
    with pytest.raises(ValueError, match="without included jobs"):
        notification_email([], "https://radar.example/jobs?new=1")
    with pytest.raises(ValueError, match="without included jobs"):
        notification_email(
            [{"filter_result": "maybe"}], "https://radar.example/jobs?new=1"
        )


def test_empty_claim_sends_nothing_and_does_not_require_dashboard() -> None:
    class EmptyRPC:
        def execute(self) -> Any:
            return type("Result", (), {"data": None})()

    class EmptyClient:
        def rpc(self, name: str, params: dict[str, Any]) -> EmptyRPC:
            assert name == "radar_claim_notification"
            assert params["p_resume_grace_minutes"] == 30
            return EmptyRPC()

    def should_not_send(*_args: Any) -> None:
        pytest.fail("An empty notification claim must never call Resend")

    assert (
        send_pending_notification(
            EmptyClient(),
            30,
            lambda: pytest.fail("No dashboard needed"),
            should_not_send,
        )
        is None
    )


def test_job_email_caps_list_and_escapes_content() -> None:
    jobs = [
        {
            "company": "Acme <script>",
            "title": f"Backend Engineer {index}",
            "location": "Lagos & Remote",
            "filter_result": "include",
        }
        for index in range(17)
    ]
    jobs.append(
        {
            "company": "Maybe Co",
            "title": "Maybe Role",
            "location": "Remote",
            "filter_result": "maybe",
        }
    )
    subject, body = notification_email(jobs, "https://radar.example/jobs?new=1", 3)
    assert subject == "17 new jobs found (17 strong matches)"
    assert "Maybe Role" not in body
    assert "3 more roles to check on the dashboard" in body
    assert body.count("<li>") == 16
    assert "+2 more" in body and "&lt;script&gt;" in body
    assert "<script>" not in body
    assert "description" not in body.lower()


def test_resend_is_mocked_and_uses_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "email_123"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    mailer = ResendMailer(
        "re_test_secret", "Radar <jobs@example.test>", "me@example.test", client
    )
    mailer.send("Subject", "<p>Message</p>", "job-notification/stable-id")
    request = requests[0]
    assert request.headers["Idempotency-Key"] == "job-notification/stable-id"
    assert request.headers["Authorization"] == "Bearer re_test_secret"
    assert b"<p>Message</p>" in request.content
    assert b"attachments" not in request.content
    client.close()


def test_resend_failure_is_reported_without_leaking_body() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, text="private response")
        )
    )
    mailer = ResendMailer(
        "re_test", "Radar <jobs@example.test>", "me@example.test", client
    )
    with pytest.raises(NotificationError, match="HTTP 500") as error:
        mailer.send("Subject", "private job facts", "stable-key")
    assert "private response" not in str(error.value)
    client.close()


def test_notification_sql_waits_for_resume_then_commits_exact_membership(
    stage6_store: Any,
) -> None:
    store = stage6_store
    source_id, _ = source_ids(store)
    maybe_job = JobFilter(FILTERS).classify(
        example_job(title="DevOps Engineer", external_id="check-fit")
    )
    snapshot(store, source_id, [prepared()])
    snapshot(
        store,
        source_id,
        [prepared(), prepared("new-job", "Backend Engineer II"), hash_job(maybe_job)],
        1,
    )
    store.db.query(
        "update public.jobs set first_seen_at=now() "
        "where external_id in ('new-job','check-fit')"
    )
    assert (
        store.db.query("select public.radar_claim_notification(30) as result")[0][
            "result"
        ]
        is None
    )
    store.db.query(
        "insert into public.health_events(issue_key,issue_type,details) "
        "values('test:one','source_failures','{}'::jsonb)"
    )
    store.db.query(
        "select public.radar_mark_health_alerted("
        "array['test:one']::text[],'{}'::text[],now())"
    )
    assert store.db.query(
        "select last_alerted_at from public.health_events where issue_key='test:one'"
    )[0]["last_alerted_at"]
    store.db.query("reset role")
    store.db.query("set role anon")
    with pytest.raises(RuntimeError):
        store.db.query("select * from public.health_events")
    store.db.query("reset role")
    store.db.query("set role service_role")

    store.db.query(
        "update public.jobs set resume_status='ready' where external_id='new-job'"
    )
    result = store.db.query("select public.radar_claim_notification(30) as result")[0][
        "result"
    ]
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["title"] == "Backend Engineer II"
    assert result["check_fit_count"] == 1
    retry = store.db.query("select public.radar_claim_notification(30) as result")[0][
        "result"
    ]
    assert retry["id"] == result["id"]
    assert retry["idempotency_key"] == result["idempotency_key"]
    assert (
        store.db.query(
            "select notified_at from public.jobs where external_id='new-job'"
        )[0]["notified_at"]
        is None
    )

    store.db.query("select public.radar_finish_notification($1::uuid)", [result["id"]])
    assert store.db.query(
        "select notified_at from public.jobs where external_id='new-job'"
    )[0]["notified_at"]
    assert (
        store.db.query("select count(*)::int as n from public.notification_jobs")[0][
            "n"
        ]
        == 2
    )
    assert (
        store.db.query(
            "select notified_at from public.jobs where external_id='check-fit'"
        )[0]["notified_at"]
        is None
    )
    assert (
        store.db.query("select public.radar_claim_notification(30) as result")[0][
            "result"
        ]
        is None
    )


def test_repeated_notification_pipeline_sends_once_and_creates_no_duplicate_jobs(
    stage6_store: Any,
) -> None:
    store = stage6_store
    source_id, _ = source_ids(store)
    snapshot(store, source_id, [prepared()])
    snapshot(
        store,
        source_id,
        [prepared(), prepared("email-job")],
        1,
    )
    store.db.query(
        "update public.jobs set first_seen_at=now(),resume_status='ready' "
        "where external_id='email-job'"
    )

    class RPC:
        def __init__(self, name: str, params: dict[str, Any]) -> None:
            self.name, self.params = name, params

        def execute(self) -> Any:
            if self.name == "radar_claim_notification":
                data = store.db.query(
                    "select public.radar_claim_notification($1) as result",
                    [self.params["p_resume_grace_minutes"]],
                )[0]["result"]
            else:
                data = store.db.query(
                    "select public.radar_finish_notification($1::uuid) as result",
                    [self.params["p_attempt_id"]],
                )[0]["result"]
            return type("Result", (), {"data": data})()

    class Client:
        def rpc(self, name: str, params: dict[str, Any]) -> RPC:
            return RPC(name, params)

    sent_keys: list[str] = []

    def send(_subject: str, _body: str, key: str) -> None:
        sent_keys.append(key)

    assert (
        send_pending_notification(
            Client(), 30, "https://radar.example/jobs?new=1", send
        )
        == 1
    )
    assert (
        send_pending_notification(
            Client(), 30, "https://radar.example/jobs?new=1", send
        )
        is None
    )
    assert len(sent_keys) == 1
    assert len(set(sent_keys)) == 1
    assert len(store.db.query("select * from public.jobs")) == 2
    assert (
        store.db.query("select count(*)::int as n from public.notification_batches")[0][
            "n"
        ]
        == 1
    )


class FakeQuery:
    def __init__(
        self, rows: list[dict[str, Any]], table_name: str = "health_events"
    ) -> None:
        self.rows = rows
        self.table_name = table_name
        self.mode = "select"
        self.payload: dict[str, Any] = {}
        self.key: str | None = None

    def select(self, _columns: str) -> FakeQuery:
        self.mode = "select"
        return self

    def upsert(self, payload: dict[str, Any], on_conflict: str) -> FakeQuery:
        assert on_conflict == "issue_key"
        self.mode, self.payload = "upsert", payload
        return self

    def update(self, payload: dict[str, Any]) -> FakeQuery:
        self.mode, self.payload = "update", payload
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:
        assert column == "issue_key"
        self.key = value
        return self

    def order(self, _column: str, desc: bool = False) -> FakeQuery:
        if desc:
            self.rows = list(reversed(self.rows))
        return self

    def limit(self, count: int) -> FakeQuery:
        self.rows = self.rows[:count]
        return self

    def execute(self) -> Any:
        if self.mode == "upsert":
            existing = next(
                (
                    row
                    for row in self.rows
                    if row["issue_key"] == self.payload["issue_key"]
                ),
                None,
            )
            if existing:
                existing.update(self.payload)
            else:
                self.rows.append(self.payload.copy())
        elif self.mode == "update":
            row = next((row for row in self.rows if row["issue_key"] == self.key), None)
            if row:
                row.update(self.payload)
        return type("Result", (), {"data": self.rows})()


class FakeClient:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []

    def table(self, name: str) -> FakeQuery:
        if name == "heartbeats":
            return FakeQuery(self.heartbeats, name)
        assert name == "health_events"
        return FakeQuery(self.events)

    def rpc(self, name: str, params: dict[str, Any]) -> Any:
        assert name == "radar_mark_health_alerted"

        class Result:
            data = None

            def execute(inner_self: Any) -> Any:
                for row in self.events:
                    if row["issue_key"] in params["p_active_keys"]:
                        row["last_alerted_at"] = params["p_alerted_at"]
                    if row["issue_key"] in params["p_recovered_keys"]:
                        row["recovered_notice_sent_at"] = params["p_alerted_at"]
                return inner_self

        return Result()


def test_health_alert_throttles_and_sends_recovery_once() -> None:
    client = FakeClient()
    now = datetime(2026, 10, 19, tzinfo=UTC)
    active_issue = {
        "source_failures:source-1": {
            "issue_type": "source_failures",
            "source_id": "source-1",
            "details": {"company": "Acme", "message": "Three failures."},
        }
    }
    active, recovered = sync_health_state(client, active_issue, now)
    assert len(active) == 1 and not recovered
    subject, body = health_email(active, recovered)
    assert subject == "Job Radar health warning" and "Acme" in body
    first_key = health_idempotency_key(active, recovered)
    mark_health_alerted(client, active, recovered, now)

    active, recovered = sync_health_state(
        client, active_issue, now + timedelta(hours=1)
    )
    assert not active and not recovered

    active, recovered = sync_health_state(client, {}, now + timedelta(hours=2))
    assert not active and not recovered

    active, recovered = sync_health_state(client, {}, now + timedelta(hours=25))
    assert not active and len(recovered) == 1
    assert health_idempotency_key(active, recovered) != first_key
    mark_health_alerted(client, active, recovered, now + timedelta(hours=25))
    active, recovered = sync_health_state(client, {}, now + timedelta(hours=26))
    assert not active and not recovered
