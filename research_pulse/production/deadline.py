"""Bound a single paper production run without leaking raw state to LangGraph."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
from typing import Iterator


class ProductionDeadlineExceeded(TimeoutError):
    """The current paper run has no provider or publication time remaining."""


@dataclass(frozen=True)
class ProductionDeadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> "ProductionDeadline":
        if seconds <= 0:
            raise ValueError("production deadline must be positive")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    def ensure_remaining(self) -> None:
        if self.remaining() <= 0:
            raise ProductionDeadlineExceeded("reading_timeout")


_CURRENT_DEADLINE: ContextVar[ProductionDeadline | None] = ContextVar(
    "research_pulse_production_deadline", default=None
)


def current_deadline() -> ProductionDeadline | None:
    return _CURRENT_DEADLINE.get()


@contextmanager
def production_deadline(seconds: float | None) -> Iterator[ProductionDeadline | None]:
    deadline = ProductionDeadline.after(seconds) if seconds is not None else None
    token = _CURRENT_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT_DEADLINE.reset(token)

