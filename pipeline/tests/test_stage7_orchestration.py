from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pipeline.heartbeat import record_heartbeat, stale_heartbeat_issue
from pipeline.llm.pacing import CallBudget


class HeartbeatQuery:
    def __init__(self, rows: list[dict[str, Any]], table: str) -> None:
        self.rows = rows
        self.table_name = table

    def insert(self, payload: dict[str, Any]) -> HeartbeatQuery:
        self.rows.append(payload)
        return self

    def select(self, _columns: str) -> HeartbeatQuery:
        return self

    def order(self, _column: str, desc: bool = False) -> HeartbeatQuery:
        if desc:
            self.rows.sort(key=lambda row: row["completed_at"], reverse=True)
        return self

    def limit(self, count: int) -> HeartbeatQuery:
        self.rows = self.rows[:count]
        return self

    def execute(self) -> Any:
        return type("Result", (), {"data": self.rows})()


class HeartbeatClient:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def table(self, name: str) -> HeartbeatQuery:
        assert name == "heartbeats"
        return HeartbeatQuery(self.rows, name)


def test_heartbeat_record_is_unique_per_run_and_stale_check() -> None:
    client = HeartbeatClient()
    now = datetime(2026, 10, 19, tzinfo=UTC)
    record_heartbeat(
        client,
        uuid4(),
        {"fetch": "ok"},
        now,
    )
    assert not stale_heartbeat_issue(client, now + timedelta(hours=2))
    assert stale_heartbeat_issue(client, now + timedelta(hours=4))


def test_missing_heartbeat_is_an_issue() -> None:
    assert stale_heartbeat_issue(HeartbeatClient())


def test_heartbeat_table_is_private_and_accepts_service_role(stage6_store: Any) -> None:
    store = stage6_store
    run_id = str(uuid4())
    result = store.db.query(
        "insert into public.heartbeats(run_id,steps) "
        "values($1::uuid,'{}'::jsonb) returning run_id",
        [run_id],
    )
    assert result[0]["run_id"] == run_id
    store.db.query("reset role")
    store.db.query("set role anon")
    try:
        try:
            store.db.query("select * from public.heartbeats")
        except RuntimeError:
            pass
        else:
            raise AssertionError("anonymous role must not read heartbeats")
    finally:
        store.db.query("reset role")
        store.db.query("set role service_role")


def test_llm_call_budget_is_a_hard_shared_limit() -> None:
    budget = CallBudget(1)
    budget.claim()
    try:
        budget.claim()
    except ValueError as exc:
        assert "LLM_MAX_CALLS_PER_RUN" in str(exc)
    else:
        raise AssertionError("second provider attempt should exceed the cap")


def test_run_all_continues_after_step_failure_and_writes_heartbeat(
    monkeypatch: Any, tmp_path: Any
) -> None:
    from pipeline import check_health, run_all, run_fetch, run_notify, run_tailor

    events: list[str] = []
    monkeypatch.setattr(run_fetch, "main", lambda _args: events.append("fetch") or 1)
    monkeypatch.setattr(run_tailor, "main", lambda _args: events.append("tailor") or 2)
    monkeypatch.setattr(run_notify, "main", lambda _args: events.append("notify") or 0)
    monkeypatch.setattr(
        check_health, "main", lambda _args: events.append("health") or 0
    )

    class FakeHttp:
        def __enter__(self) -> FakeHttp:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    class FakeStore:
        client = object()

        @classmethod
        def from_environment(cls, _http: Any) -> FakeStore:
            return cls()

        def close(self) -> None:
            pass

    stored: list[dict[str, str]] = []
    monkeypatch.setattr(run_all, "HTTPClient", FakeHttp)
    monkeypatch.setattr(run_all, "SupabaseStore", FakeStore)
    monkeypatch.setattr(
        run_all,
        "record_heartbeat",
        lambda _client, _run_id, steps, _now: stored.append(steps),
    )

    assert run_all.main(["--report-file", str(tmp_path / "run.jsonl")]) == 0
    assert events == ["fetch", "tailor", "notify", "health"]
    assert stored[0] == {
        "fetch": "failed:1",
        "tailor": "failed:2",
        "notify": "ok",
        "health": "ok",
    }


def test_run_all_dry_run_does_not_write_heartbeat(
    monkeypatch: Any, tmp_path: Any
) -> None:
    from pipeline import check_health, run_all, run_fetch, run_notify, run_tailor

    called: list[tuple[str, list[str]]] = []
    for name, module in (
        ("fetch", run_fetch),
        ("tailor", run_tailor),
        ("notify", run_notify),
        ("health", check_health),
    ):
        monkeypatch.setattr(
            module,
            "main",
            lambda args, stage=name: called.append((stage, args)) or 0,
        )
    monkeypatch.setattr(
        run_all,
        "record_heartbeat",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dry run wrote heartbeat")),
    )
    assert (
        run_all.main(["--dry-run", "--report-file", str(tmp_path / "run.jsonl")]) == 0
    )
    assert [stage for stage, _ in called] == ["fetch", "tailor", "notify", "health"]
    assert all("--dry-run" in args for _, args in called)


def test_run_all_stages_private_resume_files_and_removes_them(
    monkeypatch: Any, tmp_path: Any
) -> None:
    from pathlib import Path

    from pipeline import check_health, run_all, run_fetch, run_notify, run_tailor

    monkeypatch.setenv("RESUME_INPUTS_BUCKET", "radar-resumes")
    temp_paths: list[Path] = []

    class FakeHttp:
        def __enter__(self) -> FakeHttp:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    class FakeStore:
        client = object()

        @classmethod
        def from_environment(cls, _http: Any) -> FakeStore:
            return cls()

        def close(self) -> None:
            pass

    def stage_inputs(_store: Any, destination: Path) -> dict[str, Path]:
        temp_paths.append(destination)
        result: dict[str, Path] = {}
        for key, filename in (
            ("master_json", "master_resume.json"),
            ("master_docx", "master_resume.docx"),
            ("template", "resume_template.docx"),
            ("layout", "resume_layout.json"),
        ):
            path = destination / filename
            path.write_bytes(b"private test fixture")
            result[key] = path
        return result

    monkeypatch.setattr(run_all, "HTTPClient", FakeHttp)
    monkeypatch.setattr(run_all, "SupabaseStore", FakeStore)
    monkeypatch.setattr(run_all, "download_resume_inputs", stage_inputs)
    monkeypatch.setattr(run_all, "record_heartbeat", lambda *_args: None)
    for module in (run_fetch, run_notify, check_health):
        monkeypatch.setattr(module, "main", lambda _args: 0)

    def tailor(args: list[str]) -> int:
        assert "--master-json" in args and "--template" in args
        paths = [
            Path(args[args.index(flag) + 1])
            for flag in ("--master-json", "--master-docx", "--template", "--layout")
        ]
        assert all(path.is_file() for path in paths)
        return 0

    monkeypatch.setattr(run_tailor, "main", tailor)
    result = run_all.main(["--report-file", str(tmp_path / "run.jsonl")])

    assert result == 0
    assert len(temp_paths) == 1
    assert not temp_paths[0].exists()
