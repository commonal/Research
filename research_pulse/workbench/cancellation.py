"""Attempt-scoped cooperative cancellation shared by kernel and tools."""

from __future__ import annotations

from threading import Event, RLock


class AttemptCancelled(RuntimeError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise AttemptCancelled("attempt cancelled")


class CancellationRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._tokens: dict[str, CancellationToken] = {}

    def for_attempt(self, attempt_id: str) -> CancellationToken:
        if not attempt_id.strip():
            raise ValueError("attempt id must not be blank")
        with self._lock:
            return self._tokens.setdefault(attempt_id, CancellationToken())

    def cancel(self, attempt_id: str) -> None:
        self.for_attempt(attempt_id).cancel()
