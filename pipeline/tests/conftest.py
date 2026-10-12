import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from postgres_helpers import PostgresEngine, PostgresStore

from pipeline.http import HTTPClient
from pipeline.models import Company

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def company() -> Company:
    return Company(
        "FairMoney", ("Fair Money",), "fairmoney.io", None, "fintech", "NG", True
    )


@pytest.fixture
def recorded() -> dict[str, Any]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURES.glob("*.json")
        if path.stem != "verification"
    }


@pytest.fixture
def make_http() -> Any:
    clients: list[HTTPClient] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
        retries: int = 0,
        clock: FakeClock | None = None,
    ) -> HTTPClient:
        timer = clock or FakeClock()
        client = HTTPClient(
            transport=httpx.MockTransport(handler),
            retries=retries,
            clock=timer,
            sleep=timer.sleep,
        )
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture(scope="session")
def pg_engine() -> Any:
    node = shutil.which("node")
    module = REPO / ".tools/pglite"
    if not node or not (module / "node_modules/@electric-sql/pglite").exists():
        pytest.skip(
            "Install the documented PGlite test dependency "
            "to run SQL integration checks"
        )
    db = PostgresEngine(
        node,
        Path(__file__).with_name("db_bridge.mjs"),
        module,
        REPO / "supabase/migrations/20261006000100_stage2_fetch_storage.sql",
    )
    yield db
    db.close()


@pytest.fixture
def pg_store(pg_engine: PostgresEngine) -> PostgresStore:
    pg_engine.query("reset role")
    pg_engine.query(
        "truncate public.source_monitor_state,public.job_sources,public.jobs,"
        "public.source_runs,public.sources"
    )
    pg_engine.query("set role service_role")
    return PostgresStore(pg_engine)
