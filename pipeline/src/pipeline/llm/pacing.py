"""Thread-safe per-run token reservations, reconciled with provider usage."""

import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from pipeline.http import HTTPClient


@dataclass(eq=False)
class Reservation:
    tokens: int
    finished_at: float | None = None


class TokenBudget:
    def __init__(
        self, limit: int, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        if limit <= 0:
            raise ValueError("LLM_MAX_TOKENS_PER_MINUTE must be a positive integer")
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self._lock = threading.Lock()
        self._reservations: list[Reservation] = []
        self._cooldown_until = 0.0

    def reserve(self, tokens: int) -> Reservation:
        if not 0 < tokens <= self.limit:
            raise ValueError(
                "LLM prompt exceeds LLM_MAX_TOKENS_PER_MINUTE; "
                "reduce input or configure a larger supported budget"
            )
        while True:
            with self._lock:
                now = self.clock()
                self._reservations = [
                    r
                    for r in self._reservations
                    if r.finished_at is None or r.finished_at + 60 > now
                ]
                cooldown = max(0.0, self._cooldown_until - now)
                if (
                    not cooldown
                    and sum(r.tokens for r in self._reservations) + tokens <= self.limit
                ):
                    reservation = Reservation(tokens)
                    self._reservations.append(reservation)
                    return reservation
                expirations = [
                    r.finished_at + 60 - now
                    for r in self._reservations
                    if r.finished_at is not None
                ]
                delay = max(cooldown, min(expirations, default=0.1))
            # Sleep outside the lock so concurrent requests can settle usage.
            self.sleep(min(60.0, max(0.01, delay)))

    def finish(self, reservation: Reservation, actual_tokens: int | None) -> None:
        with self._lock:
            if actual_tokens is not None:
                reservation.tokens = max(0, actual_tokens)
            reservation.finished_at = self.clock()

    def reject(self, reservation: Reservation, delay: float) -> None:
        with self._lock:
            self._reservations.remove(reservation)
            self._cooldown_until = max(self._cooldown_until, self.clock() + delay)


_registry: WeakKeyDictionary[HTTPClient, dict[int, TokenBudget]] = WeakKeyDictionary()
_registry_lock = threading.Lock()


class CallBudget:
    """Thread-safe hard cap on provider HTTP attempts for one pipeline run."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("LLM_MAX_CALLS_PER_RUN must be a positive integer")
        self.limit = limit
        self._count = 0
        self._lock = threading.Lock()

    def claim(self) -> None:
        with self._lock:
            if self._count >= self.limit:
                raise ValueError(
                    "LLM_MAX_CALLS_PER_RUN reached; remaining extraction/tailoring "
                    "is deferred to the next run"
                )
            self._count += 1


_call_budgets: WeakKeyDictionary[HTTPClient, dict[int, CallBudget]] = (
    WeakKeyDictionary()
)
_active_run_budget: CallBudget | None = None


def shared_budget(http: HTTPClient, limit: int) -> TokenBudget:
    # Careers pages create clients lazily per changed source. Their common HTTP
    # session must own one budget, including concurrent sources and retries.
    with _registry_lock:
        budgets = _registry.setdefault(http, {})
        if limit not in budgets:
            budgets[limit] = TokenBudget(limit, http.clock, http.sleep)
        return budgets[limit]


def shared_call_budget(http: HTTPClient) -> CallBudget:
    global _active_run_budget
    configured = os.environ.get("LLM_MAX_CALLS_PER_RUN", "50")
    try:
        limit = int(configured)
    except ValueError as exc:
        raise ValueError("LLM_MAX_CALLS_PER_RUN must be a positive integer") from exc
    with _registry_lock:
        if _active_run_budget is not None:
            if _active_run_budget.limit != limit:
                raise ValueError("LLM_MAX_CALLS_PER_RUN changed during a run")
            return _active_run_budget
        budgets = _call_budgets.setdefault(http, {})
        if limit not in budgets:
            budgets[limit] = CallBudget(limit)
        return budgets[limit]


def begin_run_call_budget() -> None:
    """Share one provider-call cap across all clients in run_all."""
    global _active_run_budget
    configured = os.environ.get("LLM_MAX_CALLS_PER_RUN", "50")
    try:
        limit = int(configured)
    except ValueError as exc:
        raise ValueError("LLM_MAX_CALLS_PER_RUN must be a positive integer") from exc
    with _registry_lock:
        _active_run_budget = CallBudget(limit)


def end_run_call_budget() -> None:
    global _active_run_budget
    with _registry_lock:
        _active_run_budget = None


def reset_call_budget() -> None:
    """Clear any active or client-scoped call budgets before a standalone run."""
    global _active_run_budget
    with _registry_lock:
        _call_budgets.clear()
        _active_run_budget = None


def prompt_tokens(system: str, content: str) -> int:
    # Model tokenizers differ. UTF-8 bytes/3 plus framing headroom is a conservative
    # estimate for normal prose/JSON, not an exact model-specific tokenizer.
    # Completion capacity is reserved in full; actual usage reconciles each call.
    return math.ceil((len(system.encode()) + len(content.encode())) / 3) + 256
