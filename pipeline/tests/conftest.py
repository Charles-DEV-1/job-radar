import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline.http import HTTPClient
from pipeline.models import Company

FIXTURES = Path(__file__).parent / "fixtures"


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
