"""Attempt-scoped persist-first event contracts and store."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from types import MappingProxyType


_FORBIDDEN_EVENT_KEYS = frozenset({
    "api_key", "token", "secret", "password", "checkpoint",
    "chain_of_thought", "reasoning", "prompt", "content", "path",
})


@dataclass(frozen=True)
class UnsequencedEvent:
    event_type: str
    summary: str
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.event_type.strip() or not self.summary.strip():
            raise ValueError("event type and summary are required")
        if len(self.summary) > 500:
            raise ValueError("event summary is too large")
        lowered = {str(key).lower() for key in self.payload}
        if lowered & _FORBIDDEN_EVENT_KEYS:
            raise ValueError("unsafe event field")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class PersistedEvent:
    event_id: int
    attempt_id: str
    sequence_no: int
    generation: int
    event_type: str
    summary: str
    payload: Mapping[str, object]
    occurred_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class AttemptEventStore:
    """Persist before publishing; persistence alone assigns event order."""

    def __init__(self, repository) -> None:
        self._repository = repository
        self._subscribers: list[Callable[[PersistedEvent], None]] = []
        self._subscriber_lock = RLock()

    def subscribe(self, subscriber: Callable[[PersistedEvent], None]) -> None:
        with self._subscriber_lock:
            self._subscribers.append(subscriber)

    def append(
        self,
        attempt_id: str,
        generation: int,
        event: UnsequencedEvent,
    ) -> PersistedEvent:
        persisted = self._repository.append_attempt_event(attempt_id, generation, event)
        with self._subscriber_lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(persisted)
            except Exception:
                # Delivery is an optimization over the durable cursor. A broken
                # live subscriber must recover with read_after and cannot change
                # the result of the already committed append.
                continue
        return persisted

    def read_after(
        self, attempt_id: str, *, after_sequence: int = 0
    ) -> tuple[PersistedEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        return self._repository.list_attempt_events(attempt_id, after_sequence=after_sequence)

    def read_run(self, run_id: str) -> tuple[PersistedEvent, ...]:
        return self._repository.list_run_attempt_events(run_id)
