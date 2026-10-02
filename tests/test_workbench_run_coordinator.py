from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.run_coordinator import (
    CreateRunCommand,
    ResearchRunCoordinator,
    RunConflictError,
)
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class ResearchRunCoordinatorTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            session_id="session-1",
            research_question="如何提高 Harness 鲁棒性？",
            title="Harness",
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        ))
        run_ids = iter(("run-1", "run-2"))
        attempt_ids = iter(("attempt-1", "attempt-2", "attempt-3"))
        self.coordinator = ResearchRunCoordinator(
            self.repository,
            run_id_factory=lambda: next(run_ids),
            attempt_id_factory=lambda: next(attempt_ids),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_continue_keeps_run_and_creates_new_immutable_attempt(self) -> None:
        created = self.coordinator.create(CreateRunCommand(
            session_id="session-1",
            question="分析 Harness",
            config={"profile": "assistant"},
            budgets={"tool_calls": 4},
        ))
        first = created.current_attempt
        self.coordinator.record_attempt_outcome(
            first.attempt_id,
            AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=first.generation,
            safe_error="工具预算已耗尽",
        )

        continued = self.coordinator.continue_run(
            created.run_id,
            expected_attempt_id=first.attempt_id,
            idempotency_key="continue-1",
        )

        self.assertEqual(created.run_id, continued.run_id)
        self.assertEqual("attempt-2", continued.current_attempt.attempt_id)
        self.assertEqual(2, continued.current_attempt.attempt_no)
        self.assertEqual(AttemptStatus.QUEUED, continued.current_attempt.status)
        self.assertEqual(
            (AttemptStatus.BUDGET_EXHAUSTED, AttemptStatus.QUEUED),
            tuple(item.status for item in continued.attempt_history),
        )
        self.assertEqual(
            AttemptStatus.BUDGET_EXHAUSTED,
            self.coordinator.inspect(created.run_id).attempt_history[0].status,
        )

    def test_duplicate_continue_is_idempotent_and_creates_one_attempt(self) -> None:
        created = self.coordinator.create(CreateRunCommand(
            "session-1", "问题", {}, {"tool_calls": 2}
        ))
        first_id = created.current_attempt.attempt_id
        self.coordinator.record_attempt_outcome(
            first_id, AttemptStatus.RETRYABLE_FAILURE, expected_generation=0
        )

        def continue_once():
            return self.coordinator.continue_run(
                created.run_id,
                expected_attempt_id=first_id,
                idempotency_key="same-request",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(lambda _: continue_once(), range(2)))

        self.assertEqual(
            ("attempt-2", "attempt-2"),
            tuple(result.current_attempt.attempt_id for result in results),
        )
        self.assertEqual(2, len(self.coordinator.inspect(created.run_id).attempt_history))

    def test_stale_cancel_cannot_cancel_newer_attempt(self) -> None:
        created = self.coordinator.create(CreateRunCommand(
            "session-1", "问题", {}, {"tool_calls": 2}
        ))
        first_id = created.current_attempt.attempt_id
        self.coordinator.record_attempt_outcome(
            first_id, AttemptStatus.BUDGET_EXHAUSTED, expected_generation=0
        )
        continued = self.coordinator.continue_run(
            created.run_id,
            expected_attempt_id=first_id,
            idempotency_key="continue",
        )

        with self.assertRaisesRegex(RunConflictError, "current attempt changed"):
            self.coordinator.cancel_current(
                created.run_id,
                expected_attempt_id=first_id,
                idempotency_key="stale-cancel",
            )

        self.assertEqual(
            AttemptStatus.QUEUED,
            self.coordinator.inspect(created.run_id).current_attempt.status,
        )
        self.assertEqual("attempt-2", continued.current_attempt.attempt_id)

    def test_cancel_current_returns_cancelled_snapshot(self) -> None:
        created = self.coordinator.create(CreateRunCommand(
            "session-1", "问题", {}, {"tool_calls": 2}
        ))

        cancelled = self.coordinator.cancel_current(
            created.run_id,
            expected_attempt_id=created.current_attempt.attempt_id,
            idempotency_key="cancel-1",
        )

        self.assertEqual(AttemptStatus.CANCELLED, cancelled.status)
        self.assertEqual(
            created.current_attempt.attempt_id,
            cancelled.current_attempt.attempt_id,
        )

    def test_snapshot_projects_current_and_cumulative_budget_without_new_run_card(self) -> None:
        created = self.coordinator.create(CreateRunCommand(
            "session-1", "问题", {}, {"tool_calls": 4, "recovery_attempts": 2}
        ))
        self.coordinator.record_attempt_outcome(
            created.current_attempt.attempt_id,
            AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=created.current_attempt.generation,
            budget_used={"tool_calls": 4, "recovery_attempts": 1},
        )
        continued = self.coordinator.continue_run(
            created.run_id,
            expected_attempt_id=created.current_attempt.attempt_id,
            idempotency_key="continue-budget",
        )
        self.coordinator.record_attempt_outcome(
            continued.current_attempt.attempt_id,
            AttemptStatus.COMPLETED,
            expected_generation=continued.current_attempt.generation,
            budget_used={"tool_calls": 2, "recovery_attempts": 1},
        )

        snapshot = self.coordinator.inspect(created.run_id)
        self.assertEqual(created.run_id, snapshot.run_id)
        self.assertEqual(2, len(snapshot.attempt_history))
        self.assertEqual({"tool_calls": 2, "recovery_attempts": 1}, dict(snapshot.current_budget_used))
        self.assertEqual({"tool_calls": 6, "recovery_attempts": 2}, dict(snapshot.cumulative_budget_used))

    def test_two_consecutive_budget_exhaustions_stop_automatic_continue(self) -> None:
        created = self.coordinator.create(CreateRunCommand(
            "session-1", "反复预算耗尽的问题", {}, {"tool_calls": 2}
        ))
        first = created.current_attempt
        self.coordinator.record_attempt_outcome(
            first.attempt_id,
            AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=first.generation,
        )
        second = self.coordinator.continue_run(
            created.run_id,
            expected_attempt_id=first.attempt_id,
            idempotency_key="continue-budget-1",
        )
        self.coordinator.record_attempt_outcome(
            second.current_attempt.attempt_id,
            AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=second.current_attempt.generation,
        )

        with self.assertRaisesRegex(RunConflictError, "连续预算耗尽"):
            self.coordinator.continue_run(
                created.run_id,
                expected_attempt_id=second.current_attempt.attempt_id,
                idempotency_key="continue-budget-2",
            )

        snapshot = self.coordinator.inspect(created.run_id)
        self.assertEqual(2, len(snapshot.attempt_history))
        self.assertTrue(all(
            attempt.status is AttemptStatus.BUDGET_EXHAUSTED
            for attempt in snapshot.attempt_history
        ))

    def test_legacy_failed_attempt_remains_readable_and_continuable_without_rewrite(self) -> None:
        created = self.coordinator.create(CreateRunCommand(
            "session-1", "旧版失败运行", {}, {"tool_calls": 2}
        ))
        self.connection.execute(
            "UPDATE exploration_attempts SET status = 'failed' WHERE attempt_id = ?",
            (created.current_attempt.attempt_id,),
        )
        self.connection.execute(
            "UPDATE exploration_runs SET status = 'failed' WHERE run_id = ?",
            (created.run_id,),
        )
        self.connection.commit()

        snapshot = self.coordinator.inspect(created.run_id)

        self.assertEqual(AttemptStatus.RETRYABLE_FAILURE, snapshot.current_attempt.status)
        self.assertEqual(
            "failed",
            self.connection.execute(
                "SELECT status FROM exploration_attempts WHERE attempt_id = ?",
                (created.current_attempt.attempt_id,),
            ).fetchone()[0],
        )
        continued = self.coordinator.continue_run(
            created.run_id,
            expected_attempt_id=created.current_attempt.attempt_id,
            idempotency_key="continue-legacy-failed",
        )
        self.assertEqual(AttemptStatus.QUEUED, continued.current_attempt.status)
        self.assertEqual(2, continued.current_attempt.attempt_no)


if __name__ == "__main__":
    import unittest

    unittest.main()
