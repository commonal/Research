from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.run_events import AttemptEventStore, UnsequencedEvent
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.models import ExplorationStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.tool_dispatcher import (
    Idempotency, ParallelPolicy, ToolDispatcher, ToolEffect, ToolPolicy,
    ToolPolicyRegistry,
)


class WorkbenchRecoveryReconciliationTests(TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database = Path(self.temp.name) / "workbench.db"
        self.now = datetime(2026, 9, 9, tzinfo=UTC)
        self.connection, self.repository, self.coordinator = self._open()
        self.repository.create(ResearchSession(
            session_id="session-1", research_question="恢复", title="恢复",
            created_at=self.now,
        ))
        self.coordinator.create(CreateRunCommand(
            "session-1", "验证重启恢复", {}, {"tool_calls": 2},
        ))
        self.lease = self.repository.claim_next_attempt(
            "worker-before-restart", now=self.now,
            lease_duration=timedelta(seconds=5),
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _open(self):
        connection = sqlite3.connect(self.database, check_same_thread=False)
        repository = SQLiteWorkbenchRepository(connection)
        attempt_ids = iter(("attempt-1", "attempt-2", "attempt-3"))
        coordinator = ResearchRunCoordinator(
            repository, run_id_factory=lambda: "run-1",
            attempt_id_factory=lambda: next(attempt_ids), clock=lambda: self.now,
        )
        return connection, repository, coordinator

    @staticmethod
    def _dispatcher(repository, generation: int, handler) -> ToolDispatcher:
        registry = ToolPolicyRegistry()
        registry.register("add_evidence", handler, policy=ToolPolicy(
            ToolEffect.WRITE, Idempotency.OPERATION_KEYED, 1, 0,
            lambda _args: ("workspace:workspace-1",), ParallelPolicy.SERIAL,
        ))
        return ToolDispatcher(
            registry, operation_journal=repository,
            attempt_generation=generation,
        )

    def _restart(self) -> None:
        self.connection.close()
        self.connection, self.repository, self.coordinator = self._open()

    def test_committed_operation_without_event_replays_receipt_after_restart(self) -> None:
        operation_id = "attempt-1:call-1"
        identity = dict(
            expected_generation=self.lease.generation, tool_call_id="call-1",
            resource_key="workspace:workspace-1", operation_kind="add_evidence",
        )
        self.repository.begin_coordinated_operation(
            operation_id, self.lease.attempt_id, **identity,
        )
        receipt = {"workspace_revision": 2, "patch_id": "patch-1"}
        self.repository.settle_coordinated_operation(
            operation_id, self.lease.attempt_id,
            expected_generation=self.lease.generation,
            status="committed", receipt=receipt,
        )
        self.assertEqual((), self.repository.list_attempt_events(self.lease.attempt_id))
        self._restart()
        calls: list[int] = []

        result = self._dispatcher(
            self.repository, self.lease.generation,
            lambda: calls.append(1) or {"workspace_revision": 3},
        ).execute(
            attempt_id=self.lease.attempt_id, tool_call_id="call-1",
            tool_name="add_evidence", arguments={},
        )

        self.assertEqual("succeeded", result.outcome.status.value)
        self.assertEqual(receipt, result.ephemeral_value)
        self.assertEqual([], calls)
        self.assertEqual((), self.repository.list_attempt_events(self.lease.attempt_id))

    def test_started_operation_becomes_effect_unknown_without_reexecution(self) -> None:
        self.repository.begin_coordinated_operation(
            "attempt-1:call-unknown", self.lease.attempt_id,
            expected_generation=self.lease.generation,
            tool_call_id="call-unknown", resource_key="workspace:workspace-1",
            operation_kind="add_evidence",
        )
        self._restart()
        calls: list[int] = []

        result = self._dispatcher(
            self.repository, self.lease.generation,
            lambda: calls.append(1) or {"workspace_revision": 2},
        ).execute(
            attempt_id=self.lease.attempt_id, tool_call_id="call-unknown",
            tool_name="add_evidence", arguments={},
        )

        self.assertEqual("effect_unknown", result.outcome.status.value)
        self.assertEqual("unknown", result.outcome.side_effect_state.value)
        self.assertEqual("never", result.outcome.retryability.value)
        self.assertEqual([], calls)

    def test_persisted_event_survives_unsettled_attempt_and_lease_recovery(self) -> None:
        persisted = AttemptEventStore(self.repository).append(
            self.lease.attempt_id, self.lease.generation,
            UnsequencedEvent("tool_completed", "结果已经持久化"),
        )
        self._restart()

        abandoned = self.repository.abandon_expired_attempts(
            now=self.now + timedelta(seconds=6),
        )

        self.assertEqual((self.lease.attempt_id,), abandoned)
        recovered_events = self.repository.list_attempt_events(self.lease.attempt_id)
        self.assertEqual((persisted,), recovered_events)
        snapshot = self.coordinator.inspect("run-1")
        self.assertEqual(AttemptStatus.ABANDONED, snapshot.current_attempt.status)
        self.assertIn("无法确认", snapshot.current_attempt.safe_error or "")

    def test_startup_reconciles_nonexpired_running_attempt_after_process_restart(self) -> None:
        abandoned = self.repository.abandon_running_attempts(
            now=self.now + timedelta(seconds=1),
        )

        self.assertEqual((self.lease.attempt_id,), abandoned)
        snapshot = self.coordinator.inspect("run-1")
        self.assertEqual(AttemptStatus.ABANDONED, snapshot.current_attempt.status)
        run = self.repository.get_exploration_run("run-1")
        self.assertIsNotNone(run)
        self.assertEqual(ExplorationStatus.FAILED, run.status)
        self.assertIn("进程重启", snapshot.current_attempt.safe_error or "")

        continued = self.coordinator.continue_run(
            "run-1",
            expected_attempt_id=self.lease.attempt_id,
            idempotency_key="continue-after-process-restart",
        )
        self.assertEqual(2, continued.current_attempt.attempt_no)
        self.assertEqual(AttemptStatus.QUEUED, continued.current_attempt.status)


if __name__ == "__main__":
    import unittest

    unittest.main()
