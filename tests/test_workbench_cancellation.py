from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.cancellation import AttemptCancelled, CancellationRegistry
from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class WorkbenchCancellationTests(TestCase):
    def test_cancel_current_signals_only_expected_attempt_and_blocks_new_work(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteWorkbenchRepository(connection)
        repository.create(ResearchSession(
            "session-1", "取消传播", "取消", datetime(2026, 9, 4, tzinfo=UTC)
        ))
        registry = CancellationRegistry()
        coordinator = ResearchRunCoordinator(
            repository,
            run_id_factory=lambda: "run-1",
            attempt_id_factory=lambda: "attempt-1",
            cancellation_registry=registry,
        )
        created = coordinator.create(CreateRunCommand(
            "session-1", "问题", {}, {"tool_calls": 2}
        ))
        token = registry.for_attempt(created.current_attempt.attempt_id)

        coordinator.cancel_current(
            created.run_id,
            expected_attempt_id=created.current_attempt.attempt_id,
            idempotency_key="cancel-1",
        )

        self.assertTrue(token.cancelled)
        with self.assertRaises(AttemptCancelled):
            token.raise_if_cancelled()
        other = registry.for_attempt("attempt-other")
        self.assertFalse(other.cancelled)


if __name__ == "__main__":
    import unittest

    unittest.main()
