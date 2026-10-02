"""Safe, persist-first event projection for conversational turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence


TurnEventType = Literal[
    "turn_started", "route_decided", "tool_started", "tool_completed",
    "assistant_delta", "turn_completed", "turn_failed", "turn_cancelled",
]

_FORBIDDEN_KEYS = frozenset({
    "api_key", "token", "secret", "password", "prompt", "content",
    "reasoning", "chain_of_thought", "stack", "traceback", "path",
})


@dataclass(frozen=True)
class UnsequencedTurnEvent:
    event_type: TurnEventType
    summary: str
    stable_ids: Mapping[str, str] = field(default_factory=dict)
    counters: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.summary.strip() or len(self.summary) > 500:
            raise ValueError("turn event summary must be bounded")
        keys = {str(key).lower() for key in (*self.stable_ids, *self.counters)}
        if keys & _FORBIDDEN_KEYS:
            raise ValueError("unsafe turn event field")
        if any(value < 0 for value in self.counters.values()):
            raise ValueError("turn event counters must not be negative")


@dataclass(frozen=True)
class TurnEvent:
    sequence_no: int
    event_type: TurnEventType
    summary: str
    stable_ids: Mapping[str, str] = field(default_factory=dict)
    counters: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sequence_no < 1:
            raise ValueError("turn event sequence must be positive")
        UnsequencedTurnEvent(
            self.event_type,
            self.summary,
            self.stable_ids,
            self.counters,
        )


def project_turn_events(events: Sequence[UnsequencedTurnEvent]) -> tuple[TurnEvent, ...]:
    """Assign deterministic contiguous numbers for an in-memory projection."""
    return tuple(
        TurnEvent(index, event.event_type, event.summary, dict(event.stable_ids), dict(event.counters))
        for index, event in enumerate(events, start=1)
    )

