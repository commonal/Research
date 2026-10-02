from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.exploration import (
    ExplorationNotFoundError,
    ExplorationRetryError,
    ExplorationService,
)
from research_pulse.workbench.models import ExplorationStatus
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class WorkbenchExplorationServiceTests(TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            session_id="session-1",
            research_question="原问题",
            title="研究",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        ))
        ids = iter(("run-1", "run-2", "run-3"))
        self.service = ExplorationService(
            self.repository,
            run_id_factory=lambda: next(ids),
        )

    def tearDown(self):
        self.connection.close()

    def test_create_and_get_preserve_visible_input_snapshot(self):
        created = self.service.create(
            "session-1",
            "  如何治理长期记忆？  ",
            config_snapshot={"model": "deepseek-v4-flash"},
            budgets={"model_rounds": 8, "tool_calls": 16},
        )
        loaded = self.service.get("run-1")
        self.assertEqual(created, loaded)
        self.assertEqual(created.question_snapshot, "如何治理长期记忆？")
        self.assertEqual(created.attempt, 1)
        self.assertEqual(created.status, ExplorationStatus.QUEUED)
        self.assertEqual(created.config_snapshot, {"model": "deepseek-v4-flash"})

    def test_cancel_persists_terminal_status_through_attempt_coordinator(self):
        self.service.create("session-1", "问题", config_snapshot={}, budgets={})
        cancelled = self.service.cancel("run-1")
        self.assertEqual(cancelled.status, ExplorationStatus.CANCELLED)
        self.assertEqual(self.service.get("run-1").status, ExplorationStatus.CANCELLED)
        attempt = self.service.coordinator.inspect("run-1").current_attempt
        self.assertEqual(attempt.status, AttemptStatus.CANCELLED)

    def test_retry_keeps_one_run_and_creates_an_immutable_new_attempt(self):
        original = self.service.create("session-1", "问题", config_snapshot={"mode": "literature"}, budgets={"tool_calls": 16})
        snapshot = self.service.coordinator.inspect("run-1")
        self.service.coordinator.record_attempt_outcome(
            snapshot.current_attempt.attempt_id,
            AttemptStatus.RETRYABLE_FAILURE,
            expected_generation=0,
            safe_error="外部服务失败",
        )
        resumed = self.service.retry("run-1")
        self.assertEqual(resumed.run_id, "run-1")
        self.assertEqual(resumed.attempt, 1)
        self.assertEqual(resumed.previous_run_id, None)
        self.assertEqual(resumed.question_snapshot, original.question_snapshot)
        self.assertEqual(resumed.status, ExplorationStatus.QUEUED)
        self.assertEqual(tuple(run.run_id for run in self.repository.list_exploration_runs("session-1")), ("run-1",))
        attempts = self.service.coordinator.inspect("run-1").attempt_history
        self.assertEqual((1, 2), tuple(item.attempt_no for item in attempts))
        self.assertEqual("retryable_failure", attempts[0].status.value)
        self.assertEqual("queued", attempts[1].status.value)

    def test_missing_session_run_and_nonterminal_retry_fail_without_writes(self):
        with self.assertRaises(KeyError):
            self.service.create("missing", "问题", config_snapshot={}, budgets={})
        with self.assertRaises(ExplorationNotFoundError):
            self.service.get("missing")
        active = self.service.create("session-1", "问题", config_snapshot={}, budgets={})
        with self.assertRaises(ExplorationRetryError):
            self.service.retry(active.run_id)
        self.assertEqual(len(self.repository.list_exploration_runs("session-1")), 1)
