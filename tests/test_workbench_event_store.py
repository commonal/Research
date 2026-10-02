from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.run_events import AttemptEventStore, UnsequencedEvent
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class WorkbenchAttemptEventStoreTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            session_id="session-1",
            research_question="事件并发",
            title="事件",
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        ))
        attempt_ids = iter(("attempt-1", "attempt-2"))
        self.coordinator = ResearchRunCoordinator(
            self.repository,
            run_id_factory=lambda: "run-1",
            attempt_id_factory=lambda: next(attempt_ids),
        )
        self.attempt = self.coordinator.create(CreateRunCommand(
            "session-1", "事件是否可靠？", {}, {"tool_calls": 40}
        )).current_attempt
        self.store = AttemptEventStore(self.repository)

    def tearDown(self) -> None:
        self.connection.close()

    def test_concurrent_append_assigns_one_contiguous_sequence(self) -> None:
        def append(index: int):
            return self.store.append(
                self.attempt.attempt_id,
                self.attempt.generation,
                UnsequencedEvent("tool_completed", f"工具 {index} 完成"),
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            persisted = tuple(pool.map(append, range(30)))

        history = self.store.read_after(self.attempt.attempt_id, after_sequence=0)
        self.assertEqual(tuple(range(1, 31)), tuple(item.sequence_no for item in history))
        self.assertEqual(30, len({item.event_id for item in persisted}))
        self.assertEqual(
            tuple(range(21, 31)),
            tuple(item.sequence_no for item in self.store.read_after(
                self.attempt.attempt_id, after_sequence=20
            )),
        )

    def test_subscriber_observes_event_only_after_it_is_readable(self) -> None:
        observations: list[tuple[int, tuple[int, ...]]] = []

        def subscriber(event) -> None:
            readable = self.store.read_after(event.attempt_id, after_sequence=0)
            observations.append((event.sequence_no, tuple(item.sequence_no for item in readable)))

        self.store.subscribe(subscriber)
        persisted = self.store.append(
            self.attempt.attempt_id,
            self.attempt.generation,
            UnsequencedEvent("run_started", "开始"),
        )

        self.assertEqual(1, persisted.sequence_no)
        self.assertEqual([(1, (1,))], observations)

    def test_subscriber_failure_does_not_hide_committed_event(self) -> None:
        def disconnected(_event) -> None:
            raise ConnectionError("subscriber disconnected")

        self.store.subscribe(disconnected)
        persisted = self.store.append(
            self.attempt.attempt_id,
            self.attempt.generation,
            UnsequencedEvent("phase_changed", "进入检索"),
        )

        recovered = self.store.read_after(
            self.attempt.attempt_id,
            after_sequence=persisted.sequence_no - 1,
        )
        self.assertEqual((persisted,), recovered)

    def test_persistence_failure_is_never_published(self) -> None:
        class FailingRepository:
            def append_attempt_event(self, _attempt_id, _generation, _event):
                raise sqlite3.OperationalError("disk unavailable")

        observed: list[object] = []
        store = AttemptEventStore(FailingRepository())
        store.subscribe(observed.append)

        with self.assertRaises(sqlite3.OperationalError):
            store.append("attempt-1", 0, UnsequencedEvent("tool_started", "开始"))
        self.assertEqual([], observed)

    def test_run_projection_keeps_attempt_sequences_independent_and_stable(self) -> None:
        first = self.store.append(
            self.attempt.attempt_id,
            self.attempt.generation,
            UnsequencedEvent("run_started", "第一次执行"),
        )
        self.coordinator.record_attempt_outcome(
            self.attempt.attempt_id,
            AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=self.attempt.generation,
        )
        continued = self.coordinator.continue_run(
            "run-1",
            expected_attempt_id=self.attempt.attempt_id,
            idempotency_key="continue-events",
        )
        second_attempt = continued.current_attempt
        second = self.store.append(
            second_attempt.attempt_id,
            second_attempt.generation,
            UnsequencedEvent("run_started", "第二次执行"),
        )

        projection = self.store.read_run("run-1")
        self.assertEqual((1, 1), tuple(event.sequence_no for event in projection))
        self.assertEqual((first.event_id, second.event_id), tuple(event.event_id for event in projection))


if __name__ == "__main__":
    import unittest

    unittest.main()
