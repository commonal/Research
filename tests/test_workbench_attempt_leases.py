from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest import TestCase

from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.run_events import AttemptEventStore, UnsequencedEvent
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.tool_dispatcher import (
    Idempotency, ParallelPolicy, ToolDispatcher, ToolEffect, ToolPolicy,
    ToolPolicyRegistry,
)


class WorkbenchAttemptLeaseTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            session_id="session-1",
            research_question="持久 worker",
            title="Worker",
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        ))
        attempt_ids = iter(("attempt-1", "attempt-2"))
        self.coordinator = ResearchRunCoordinator(
            self.repository,
            run_id_factory=lambda: "run-1",
            attempt_id_factory=lambda: next(attempt_ids),
        )
        self.coordinator.create(CreateRunCommand(
            "session-1", "测试 lease", {}, {"tool_calls": 2}
        ))
        self.now = datetime(2026, 9, 4, 1, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.connection.close()

    def test_concurrent_claim_has_one_winner_and_renewal_is_fenced(self) -> None:
        def claim(worker: str):
            return self.repository.claim_next_attempt(
                worker, now=self.now, lease_duration=timedelta(seconds=30)
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = tuple(pool.map(claim, ("worker-a", "worker-b")))

        winners = tuple(item for item in claims if item is not None)
        self.assertEqual(1, len(winners))
        lease = winners[0]
        self.assertEqual(1, lease.generation)
        renewed = self.repository.renew_attempt_lease(
            lease.attempt_id,
            worker_id=lease.worker_id,
            expected_generation=lease.generation,
            now=self.now + timedelta(seconds=10),
            lease_duration=timedelta(seconds=30),
        )
        self.assertEqual(self.now + timedelta(seconds=40), renewed.lease_expires_at)
        with self.assertRaisesRegex(ValueError, "lease changed"):
            self.repository.renew_attempt_lease(
                lease.attempt_id,
                worker_id="stale-worker",
                expected_generation=lease.generation,
                now=self.now + timedelta(seconds=11),
                lease_duration=timedelta(seconds=30),
            )

    def test_expired_lease_can_be_reclaimed_with_new_generation(self) -> None:
        first = self.repository.claim_next_attempt(
            "worker-a", now=self.now, lease_duration=timedelta(seconds=5)
        )
        self.assertIsNotNone(first)

        reclaimed = self.repository.claim_next_attempt(
            "worker-b",
            now=self.now + timedelta(seconds=6),
            lease_duration=timedelta(seconds=5),
        )

        self.assertIsNotNone(reclaimed)
        self.assertEqual(first.attempt_id, reclaimed.attempt_id)
        self.assertEqual(first.generation + 1, reclaimed.generation)
        self.assertEqual("worker-b", reclaimed.worker_id)

    def test_stale_worker_cannot_append_event_finish_or_start_operation(self) -> None:
        first = self.repository.claim_next_attempt(
            "worker-a", now=self.now, lease_duration=timedelta(seconds=5)
        )
        reclaimed = self.repository.claim_next_attempt(
            "worker-b",
            now=self.now + timedelta(seconds=6),
            lease_duration=timedelta(seconds=5),
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(reclaimed)

        with self.assertRaisesRegex(ValueError, "generation changed"):
            AttemptEventStore(self.repository).append(
                first.attempt_id,
                first.generation,
                UnsequencedEvent("tool_completed", "旧 worker 返回"),
            )
        with self.assertRaisesRegex(ValueError, "generation changed"):
            self.coordinator.record_attempt_outcome(
                first.attempt_id,
                AttemptStatus.COMPLETED,
                expected_generation=first.generation,
            )
        with self.assertRaisesRegex(ValueError, "generation changed"):
            self.repository.begin_coordinated_operation(
                "operation-stale",
                first.attempt_id,
                expected_generation=first.generation,
                tool_call_id="call-stale",
                resource_key="workspace:one",
                operation_kind="workspace_patch",
            )

    def test_cancelled_attempt_rejects_late_event_at_same_generation(self) -> None:
        lease = self.repository.claim_next_attempt(
            "worker-a", now=self.now, lease_duration=timedelta(seconds=30)
        )
        remote_started = Event()
        allow_remote_return = Event()

        def append_after_remote_return() -> str:
            remote_started.set()
            allow_remote_return.wait(timeout=1)
            try:
                AttemptEventStore(self.repository).append(
                    lease.attempt_id, lease.generation,
                    UnsequencedEvent("tool_completed", "远程调用晚返回"),
                )
            except ValueError as error:
                self.assertIn("terminal", str(error))
                return "rejected"
            return "persisted"

        with ThreadPoolExecutor(max_workers=1) as pool:
            callback = pool.submit(append_after_remote_return)
            self.assertTrue(remote_started.wait(timeout=1))
            self.coordinator.cancel_current(
                "run-1", expected_attempt_id=lease.attempt_id,
                idempotency_key="cancel-running",
            )
            allow_remote_return.set()
            self.assertEqual("rejected", callback.result(timeout=1))

        self.assertEqual((), self.repository.list_attempt_events(lease.attempt_id))

    def test_operation_receipt_is_generation_fenced_and_idempotently_replayed(self) -> None:
        lease = self.repository.claim_next_attempt(
            "worker-a", now=self.now, lease_duration=timedelta(seconds=30)
        )
        self.assertIsNotNone(lease)
        identity = {
            "expected_generation": lease.generation,
            "tool_call_id": "call-1",
            "resource_key": "workspace:workspace-1",
            "operation_kind": "workspace_write",
        }
        self.assertIsNone(self.repository.begin_coordinated_operation(
            "operation-1", lease.attempt_id, **identity
        ))
        receipt = {"workspace_revision": 2, "patch_id": "patch-1"}

        self.repository.settle_coordinated_operation(
            "operation-1", lease.attempt_id,
            expected_generation=lease.generation,
            status="committed", receipt=receipt,
        )

        self.assertEqual(receipt, self.repository.begin_coordinated_operation(
            "operation-1", lease.attempt_id, **identity
        ))
        with self.assertRaisesRegex(ValueError, "identity"):
            self.repository.begin_coordinated_operation(
                "operation-1", lease.attempt_id,
                **{**identity, "tool_call_id": "call-other"},
            )

    def test_workspace_write_replay_returns_receipt_without_reapplying_handler(self) -> None:
        lease = self.repository.claim_next_attempt(
            "worker-a", now=self.now, lease_duration=timedelta(seconds=30)
        )
        calls: list[int] = []

        def commit_patch() -> dict[str, object]:
            calls.append(1)
            return {"workspace_revision": 2, "patch_id": "patch-1"}

        registry = ToolPolicyRegistry()
        registry.register("add_evidence", commit_patch, policy=ToolPolicy(
            ToolEffect.WRITE, Idempotency.OPERATION_KEYED, 1, 0,
            lambda _args: ("workspace:workspace-1",), ParallelPolicy.SERIAL,
        ))
        dispatcher = ToolDispatcher(
            registry,
            operation_journal=self.repository,
            attempt_generation=lease.generation,
        )

        first = dispatcher.execute(
            attempt_id=lease.attempt_id, tool_call_id="call-1",
            tool_name="add_evidence", arguments={},
        )
        replay = dispatcher.execute(
            attempt_id=lease.attempt_id, tool_call_id="call-1",
            tool_name="add_evidence", arguments={},
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(first.ephemeral_value, replay.ephemeral_value)
        self.assertEqual(first.outcome.operation_id, replay.outcome.operation_id)

    def test_expired_worker_can_be_abandoned_then_continued_as_new_attempt(self) -> None:
        claimed = self.repository.claim_next_attempt(
            "worker-a", now=self.now, lease_duration=timedelta(seconds=5)
        )
        abandoned = self.repository.abandon_expired_attempts(
            now=self.now + timedelta(seconds=6)
        )

        self.assertEqual((claimed.attempt_id,), abandoned)
        snapshot = self.coordinator.inspect("run-1")
        self.assertEqual(AttemptStatus.ABANDONED, snapshot.current_attempt.status)
        continued = self.coordinator.continue_run(
            "run-1",
            expected_attempt_id=claimed.attempt_id,
            idempotency_key="continue-abandoned",
        )
        self.assertEqual(2, continued.current_attempt.attempt_no)
        self.assertEqual(AttemptStatus.QUEUED, continued.current_attempt.status)


if __name__ == "__main__":
    import unittest

    unittest.main()
