"""Test-only RPC store backed by real PostgreSQL via a JSON-lines Node bridge."""

import json
import subprocess
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.models import FetchCounts, Job, SourceConfig
from pipeline.storage import job_record


class PostgresEngine:
    def __init__(
        self,
        node: str,
        bridge: Path,
        module: Path,
        migration: Path,
        *,
        dashboard: bool = False,
        stage6: bool = False,
    ) -> None:
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            [
                node,
                str(bridge),
                str(module),
                str(migration),
                str(migration.with_name("20261010000100_stage3_careers_monitor.sql")),
                str(migration.with_name("20261012000100_stage3_sync_safe_updates.sql")),
                str(migration.with_name("20261012000200_stage4_resume_tailoring.sql")),
                *(
                    [
                        str(
                            migration.with_name(
                                "20261014000100_stage5_private_dashboard.sql"
                            )
                        )
                    ]
                    if dashboard
                    else []
                ),
                *(
                    [
                        str(
                            migration.with_name(
                                "20261019000100_stage6_notifications_health.sql"
                            )
                        )
                    ]
                    if stage6
                    else []
                ),
                *(
                    [
                        str(
                            migration.with_name(
                                "20261019000200_stage6_include_email_only.sql"
                            )
                        )
                    ]
                    if stage6
                    else []
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        assert self.process.stdout
        ready = self.process.stdout.readline()
        if not ready:
            assert self.process.stderr
            raise RuntimeError(self.process.stderr.read())
        assert json.loads(ready)["ready"]

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        with self.lock:
            assert self.process.stdin and self.process.stdout
            self.process.stdin.write(
                json.dumps({"sql": sql, "params": params or []}) + "\n"
            )
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("PostgreSQL test engine stopped")
            response = json.loads(line)
            if "error" in response:
                raise RuntimeError(response["error"])
            return response["result"]["rows"]

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        self.process.wait(timeout=15)


class PostgresStore:
    def __init__(self, db: PostgresEngine) -> None:
        self.db = db

    def sync_sources(self, sources: list[SourceConfig]) -> dict[tuple[str, str], str]:
        rows = self.db.query(
            "select public.radar_sync_sources($1::jsonb) as result",
            [json.dumps([asdict(source) for source in sources])],
        )[0]["result"]
        return {(row["ats"], row["slug"]): row["id"] for row in rows}

    def start_run(self, source_id: str, run_id: str, started_at: datetime) -> None:
        self.db.query(
            "select public.radar_start_run($1::uuid,$2::uuid,$3::timestamptz)",
            [source_id, run_id, started_at.isoformat()],
        )

    def monitor_state(self, source_id: str) -> dict[str, Any]:
        return self.db.query(
            "select public.radar_monitor_state($1::uuid) as result", [source_id]
        )[0]["result"]

    def complete_monitor(
        self,
        run_id: str,
        jobs: list[Job],
        observed_at: datetime,
        duration_ms: int,
        state: dict[str, Any],
        expected_hash: str | None,
        metadata: dict[str, Any],
    ) -> FetchCounts:
        result = self.db.query(
            "select public.radar_commit_monitor("
            "$1::uuid,$2::jsonb,$3::timestamptz,$4::bigint,"
            "$5::jsonb,$6::text,$7::jsonb) as result",
            [
                run_id,
                json.dumps([job_record(j) for j in jobs]),
                observed_at.isoformat(),
                duration_ms,
                json.dumps(state),
                expected_hash,
                json.dumps(metadata),
            ],
        )[0]["result"]
        return FetchCounts(**result)

    def run_metadata(self, run_id: str, metadata: dict[str, Any]) -> None:
        self.db.query(
            "select public.radar_run_metadata($1::uuid,$2::jsonb)",
            [run_id, json.dumps(metadata)],
        )

    def complete_run(
        self, run_id: str, jobs: list[Job], observed_at: datetime, duration_ms: int
    ) -> FetchCounts:
        result = self.db.query(
            "select public.radar_commit_snapshot("
            "$1::uuid,$2::jsonb,$3::timestamptz,$4::bigint) as result",
            [
                run_id,
                json.dumps([job_record(job) for job in jobs]),
                observed_at.isoformat(),
                duration_ms,
            ],
        )[0]["result"]
        return FetchCounts(**result)

    def fail_run(
        self,
        run_id: str,
        error: str,
        duration_ms: int,
        completed_at: datetime,
        jobs_found: int = 0,
    ) -> None:
        self.db.query(
            "select public.radar_fail_run("
            "$1::uuid,$2::text,$3::bigint,$4::timestamptz,$5::integer)",
            [run_id, error, duration_ms, completed_at.isoformat(), jobs_found],
        )

    def close(self) -> None:
        # The pytest session owns the shared engine, not an individual CLI invocation.
        pass
