"""Adapter-external hard budget authorization for research runs."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Callable, Literal

from research_pulse.workbench.agent_runtime import RunBudgets


RunStatus = Literal["running", "completed", "cancelled", "budget_exhausted"]


class RunTerminated(RuntimeError):
    def __init__(self, reason: str, dimension: str | None = None) -> None:
        super().__init__(reason if dimension is None else f"{reason}: {dimension}")
        self.reason = reason
        self.dimension = dimension


@dataclass(frozen=True)
class ModelReservation:
    reservation_id: int
    max_output_tokens: int


@dataclass(frozen=True)
class BudgetSnapshot:
    status: RunStatus
    stop_dimension: str | None
    model_rounds: int
    tool_calls: int
    block_reads: int
    elapsed_seconds: float
    input_tokens: int
    output_tokens: int
    recovery_attempts: int


class BudgetLedger:
    _APPLICATION_DIMENSIONS = frozenset({
        "model_rounds", "tool_calls", "block_reads", "input_tokens", "output_tokens",
    })

    def __init__(
        self,
        limits: RunBudgets,
        *,
        recovery_limit: int = 3,
        attempt_id: str | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if recovery_limit < 0:
            raise ValueError("recovery limit must not be negative")
        self.limits = limits
        self.recovery_limit = recovery_limit
        self.attempt_id = attempt_id
        self.clock = clock
        self.started_at = clock()
        self.status: RunStatus = "running"
        self.stop_dimension: str | None = None
        self.model_rounds = 0
        self.tool_calls = 0
        self.block_reads = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.recovery_attempts = 0
        self._next_reservation_id = 1
        self._active_reservations: dict[int, int] = {}
        self._lock = RLock()

    def authorize_model(
        self, input_tokens: int, max_output_tokens: int
    ) -> ModelReservation:
        with self._lock:
            self._ensure_running()
            if input_tokens < 0 or max_output_tokens < 0:
                raise ValueError("token charges must not be negative")
            self._check_projected("model_rounds", self.model_rounds + 1, self.limits.model_rounds)
            self._check_projected("input_tokens", self.input_tokens + input_tokens, self.limits.input_tokens)
            self._check_projected("output_tokens", self.output_tokens + max_output_tokens, self.limits.output_tokens)
            reservation = ModelReservation(self._next_reservation_id, max_output_tokens)
            self._next_reservation_id += 1
            self._active_reservations[reservation.reservation_id] = max_output_tokens
            self.model_rounds += 1
            self.input_tokens += input_tokens
            self.output_tokens += max_output_tokens
            return reservation

    def settle_model(
        self, reservation: ModelReservation, *, actual_output_tokens: int
    ) -> None:
        with self._lock:
            self._ensure_running()
            reserved = self._active_reservations.get(reservation.reservation_id)
            if reserved is None:
                raise ValueError("model reservation is unavailable")
            if actual_output_tokens < 0 or actual_output_tokens > reserved:
                raise ValueError("actual output exceeds reserved output budget")
            self.output_tokens -= reserved - actual_output_tokens
            del self._active_reservations[reservation.reservation_id]

    def authorize_tool(self, tool_name: str, *, block_reads: int = 0) -> None:
        with self._lock:
            self._ensure_running()
            if not tool_name.strip() or block_reads < 0:
                raise ValueError("tool charge is invalid")
            self._check_projected("tool_calls", self.tool_calls + 1, self.limits.tool_calls)
            self._check_projected("block_reads", self.block_reads + block_reads, self.limits.block_reads)
            self.tool_calls += 1
            self.block_reads += block_reads

    def authorize_recovery(self, reason: str) -> None:
        with self._lock:
            self._ensure_running()
            if not reason.strip():
                raise ValueError("recovery reason must not be blank")
            self._check_projected(
                "recovery_attempts", self.recovery_attempts + 1, self.recovery_limit
            )
            self.recovery_attempts += 1

    def request_cancel(self) -> None:
        with self._lock:
            if self.status == "running":
                self.status = "cancelled"
                self.stop_dimension = None

    def complete(self) -> None:
        with self._lock:
            self._ensure_running()
            self.status = "completed"

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            if self.status == "running":
                self._check_wall()
            return BudgetSnapshot(
                status=self.status,
                stop_dimension=self.stop_dimension,
                model_rounds=self.model_rounds,
                tool_calls=self.tool_calls,
                block_reads=self.block_reads,
                elapsed_seconds=max(0.0, self.clock() - self.started_at),
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                recovery_attempts=self.recovery_attempts,
            )

    def remaining(self, dimension: str) -> int:
        """Return the unreserved amount for one integer budget dimension.

        Tool adapters use this before accepting a variable-size batch.  A
        caller must be able to shrink a request to the remaining budget rather
        than discovering the overflow only after ``authorize_tool`` has marked
        the whole attempt as exhausted.
        """
        with self._lock:
            if not hasattr(self.limits, dimension):
                raise ValueError(f"unknown budget dimension: {dimension}")
            if self.limits.experimental_unbounded and dimension in self._APPLICATION_DIMENSIONS:
                # A large sentinel keeps existing adapters that use
                # ``remaining`` functional while making it clear that the
                # application budget is not the stopping authority in test
                # mode.  Wall-clock and provider limits still apply.
                return 2_147_483_647
            limit = getattr(self.limits, dimension)
            used = getattr(self, dimension)
            return max(0, int(limit) - int(used))

    def ensure_running(self) -> None:
        """Raise the lifecycle control signal when this attempt has stopped."""
        with self._lock:
            self._ensure_running()

    def _ensure_running(self) -> None:
        if self.status != "running":
            raise RunTerminated(self.status, self.stop_dimension)
        self._check_wall()

    def _check_wall(self) -> None:
        if self.clock() - self.started_at > self.limits.wall_seconds:
            self._exhaust("wall_seconds")

    def _check_projected(self, dimension: str, projected: int, limit: int) -> None:
        if self.limits.experimental_unbounded and dimension in self._APPLICATION_DIMENSIONS:
            return
        if projected > limit:
            self._exhaust(dimension)

    def _exhaust(self, dimension: str) -> None:
        self.status = "budget_exhausted"
        self.stop_dimension = dimension
        raise RunTerminated("budget_exhausted", dimension)


class BudgetEnforcer(BudgetLedger):
    """Backward-compatible name while callers migrate to Attempt-scoped ledgers."""
