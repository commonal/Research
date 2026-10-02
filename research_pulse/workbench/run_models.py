"""Framework-free contracts for durable workbench research runs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class AttemptTransitionError(ValueError):
    """Raised when an immutable attempt is moved out of a terminal state."""


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    AWAITING_USER = "awaiting_user"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    ABANDONED = "abandoned"


TERMINAL_ATTEMPT_STATUSES = frozenset({
    AttemptStatus.COMPLETED,
    AttemptStatus.AWAITING_USER,
    AttemptStatus.BUDGET_EXHAUSTED,
    AttemptStatus.CANCELLED,
    AttemptStatus.RETRYABLE_FAILURE,
    AttemptStatus.TERMINAL_FAILURE,
    AttemptStatus.ABANDONED,
})

CONTINUABLE_ATTEMPT_STATUSES = frozenset({
    AttemptStatus.BUDGET_EXHAUSTED,
    AttemptStatus.CANCELLED,
    AttemptStatus.RETRYABLE_FAILURE,
    AttemptStatus.ABANDONED,
})

_ATTEMPT_TRANSITIONS = {
    AttemptStatus.QUEUED: {
        AttemptStatus.RUNNING,
        AttemptStatus.CANCELLED,
        AttemptStatus.ABANDONED,
    },
    AttemptStatus.RUNNING: TERMINAL_ATTEMPT_STATUSES,
}


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class ResearchRun:
    run_id: str
    session_id: str
    question: str
    config: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.session_id.strip():
            raise ValueError("run and session ids must not be blank")
        if not self.question.strip():
            raise ValueError("question must not be blank")
        object.__setattr__(self, "config", _frozen_mapping(self.config))


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    run_id: str
    attempt_no: int
    status: AttemptStatus
    generation: int = 0
    input_snapshot: Mapping[str, object] = field(default_factory=dict)
    budgets: Mapping[str, int] = field(default_factory=dict)
    budget_used: Mapping[str, int] = field(default_factory=dict)
    safe_error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Compatibility projections for pre-Attempt runs are deliberately
    # read-only.  This flag keeps callers from mistaking a synthetic identity
    # for a worker-claimable, mutable database row.
    legacy: bool = False

    def __post_init__(self) -> None:
        if not self.attempt_id.strip() or not self.run_id.strip():
            raise ValueError("attempt and run ids must not be blank")
        if self.attempt_no < 1 or self.generation < 0:
            raise ValueError("attempt number must be positive and generation non-negative")
        object.__setattr__(self, "input_snapshot", _frozen_mapping(self.input_snapshot))
        object.__setattr__(self, "budgets", MappingProxyType(dict(self.budgets)))
        object.__setattr__(self, "budget_used", MappingProxyType(dict(self.budget_used)))

    def transition(self, status: AttemptStatus, *, safe_error: str | None = None) -> "Attempt":
        allowed = _ATTEMPT_TRANSITIONS.get(self.status, frozenset())
        if status not in allowed:
            raise AttemptTransitionError(
                f"illegal attempt transition: {self.status.value} -> {status.value}"
            )
        return replace(self, status=status, safe_error=safe_error)


@dataclass(frozen=True)
class AttemptOutcome:
    attempt_id: str
    status: AttemptStatus
    safe_error: str | None = None
    diagnostic_id: str | None = None
    budget_used: Mapping[str, int] = field(default_factory=dict)
    final_draft: str | None = None

    def __post_init__(self) -> None:
        if not self.attempt_id.strip():
            raise ValueError("attempt id must not be blank")
        if self.status not in TERMINAL_ATTEMPT_STATUSES:
            raise ValueError("attempt outcome status must be terminal")
        object.__setattr__(self, "budget_used", MappingProxyType(dict(self.budget_used)))


@dataclass(frozen=True)
class AttemptLease:
    attempt_id: str
    worker_id: str
    generation: int
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.attempt_id.strip() or not self.worker_id.strip():
            raise ValueError("attempt and worker ids must not be blank")
        if self.generation < 1:
            raise ValueError("claimed attempt generation must be positive")


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    session_id: str
    question: str
    current_attempt: Attempt
    attempt_history: tuple[Attempt, ...]
    config: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.attempt_history or self.attempt_history[-1] != self.current_attempt:
            raise ValueError("current attempt must be the last attempt in history")
        object.__setattr__(self, "config", _frozen_mapping(self.config))

    @property
    def status(self) -> AttemptStatus:
        return self.current_attempt.status

    @property
    def current_budget_used(self) -> Mapping[str, int]:
        return self.current_attempt.budget_used

    @property
    def cumulative_budget_used(self) -> Mapping[str, int]:
        totals: dict[str, int] = {}
        for attempt in self.attempt_history:
            for dimension, value in attempt.budget_used.items():
                totals[dimension] = totals.get(dimension, 0) + value
        return MappingProxyType(totals)
