"""Persist-first projection of safe NoteRun events.

Mirrors the exploration event projector: events are append-only, sequenced,
and sanitised before they land in SQLite so no prompt, key, or block body is
ever persisted here. A per-context ``NoteRunContext`` lets the reading
provider's observer route each operation event to the correct run without
sharing mutable state between concurrently-executing note runs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import replace
import re
from typing import Protocol

from research_pulse.workbench.agent_runtime import SafeAgentEvent


class NoteRunEventRepository(Protocol):
    def append_note_run_event(self, note_run_id: str, event: SafeAgentEvent) -> None: ...
    def list_note_run_events(self, note_run_id: str) -> tuple[SafeAgentEvent, ...]: ...


class UnsafeNoteRunEventError(ValueError):
    pass


_SENSITIVE_MARKERS = (
    "api_key",
    "password",
    "chain_of_thought",
    "private prompt",
    "hidden prompt",
    "sk-",
)
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:\\|/(?:home|users|etc|var)/)", re.IGNORECASE)

# Guards against persisting a provider response/request body or filesystem path.
_FORBIDDEN_KEYS = frozenset({"content", "prompt", "body", "path", "input", "output"})


def _ensure_safe(event: SafeAgentEvent) -> None:
    values = (event.summary, *event.stable_ids.values())
    for value in values:
        lowered = value.lower()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS) or _ABSOLUTE_PATH.search(value):
            raise UnsafeNoteRunEventError("note run event contains sensitive data")
    keys = {key.lower() for key in (*event.stable_ids.keys(), *event.counters.keys())}
    if keys & _FORBIDDEN_KEYS:
        raise UnsafeNoteRunEventError("note run event carries a forbidden field")


class NoteRunEventProjector:
    def __init__(self, repository: NoteRunEventRepository) -> None:
        self.repository = repository
        self._subscribers: dict[str, list[Callable[[SafeAgentEvent], None]]] = defaultdict(list)

    def history(self, note_run_id: str) -> tuple[SafeAgentEvent, ...]:
        return self.repository.list_note_run_events(note_run_id)

    def subscribe(self, note_run_id: str, subscriber: Callable[[SafeAgentEvent], None]) -> None:
        self._subscribers[note_run_id].append(subscriber)

    def emit(
        self,
        note_run_id: str,
        event_type: str,
        summary: str,
        *,
        stable_ids: dict[str, str] | None = None,
        counters: dict[str, int] | None = None,
    ) -> None:
        """Publish a new event with an auto-assigned, append-only sequence."""
        existing = self.history(note_run_id)
        event = SafeAgentEvent(
            sequence_no=len(existing) + 1,
            event_type=event_type,  # type: ignore[arg-type]
            summary=summary,
            stable_ids=stable_ids or {},
            counters=counters or {},
        )
        self.publish(note_run_id, event)

    def publish(self, note_run_id: str, event: SafeAgentEvent) -> None:
        _ensure_safe(event)
        self.repository.append_note_run_event(note_run_id, event)
        for subscriber in tuple(self._subscribers.get(note_run_id, ())):
            subscriber(event)


class RunOperationContext:
    """Routes provider operation callbacks to the note run currently executing.

    A contextvar keeps concurrent executions isolated (each background task /
    thread sees only its own active note_run_id) without mutable shared state.
    """

    _active: ContextVar[str | None] = ContextVar("note_run_operation", default=None)

    @classmethod
    def note_run_id(cls) -> str | None:
        return cls._active.get()

    @classmethod
    def set(cls, note_run_id: str | None) -> None:
        cls._active.set(note_run_id)
