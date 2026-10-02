from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.run_coordinator import ResearchRunCoordinator
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class LegacyAttemptProjectionTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                research_question TEXT,
                title TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                created_at TEXT NOT NULL,
                layout_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE exploration_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                question_snapshot TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                previous_run_id TEXT,
                budget_json TEXT NOT NULL DEFAULT '{}',
                final_draft TEXT,
                safe_error TEXT,
                stage TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                UNIQUE (session_id, attempt)
            );
            CREATE TABLE exploration_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES exploration_runs(run_id),
                sequence_no INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, sequence_no)
            );
            """
        )
        created_at = datetime(2026, 8, 30, tzinfo=UTC).isoformat()
        self.connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            ("session-old", "旧问题", "旧工作台", "active", created_at, "{}"),
        )
        self.connection.execute(
            """INSERT INTO exploration_runs (
                run_id, session_id, question_snapshot, config_json, status,
                attempt, budget_json, safe_error, stage, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "run-old", "session-old", "旧 Harness 为什么失败？", "{}",
                "budget_exhausted", 7, '{"tool_calls": 8}', "探索预算已耗尽",
                "failed", created_at,
            ),
        )
        for sequence_no, event_type, summary in (
            (4, "tool_completed", "已读取工作台状态"),
            (9, "run_failed", "探索预算已耗尽"),
        ):
            self.connection.execute(
                """INSERT INTO exploration_events (
                    run_id, sequence_no, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    "run-old",
                    sequence_no,
                    event_type,
                    json.dumps({"summary": summary, "counters": {"tool_calls": 1}}),
                    created_at,
                ),
            )
        self.connection.commit()
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.coordinator = ResearchRunCoordinator(
            self.repository, attempt_id_factory=lambda: "attempt-after-legacy"
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_old_run_projects_one_opaque_read_only_attempt_without_backfill(self) -> None:
        snapshot = self.coordinator.inspect("run-old")

        self.assertEqual(1, len(snapshot.attempt_history))
        self.assertEqual("legacy-run:run-old", snapshot.current_attempt.attempt_id)
        self.assertEqual(1, snapshot.current_attempt.attempt_no)
        self.assertTrue(snapshot.current_attempt.legacy)
        self.assertEqual(AttemptStatus.BUDGET_EXHAUSTED, snapshot.status)
        self.assertEqual({"tool_calls": 8}, dict(snapshot.current_attempt.budgets))
        self.assertEqual(
            0,
            self.connection.execute("SELECT COUNT(*) FROM exploration_attempts").fetchone()[0],
        )

        continued = self.coordinator.continue_run(
            "run-old",
            expected_attempt_id="legacy-run:run-old",
            idempotency_key="create-new-attempt",
        )
        self.assertEqual("attempt-after-legacy", continued.current_attempt.attempt_id)
        self.assertFalse(continued.current_attempt.legacy)
        self.assertEqual(2, continued.current_attempt.attempt_no)
        self.assertTrue(continued.attempt_history[0].legacy)
        self.assertEqual(
            1,
            self.connection.execute("SELECT COUNT(*) FROM exploration_attempts").fetchone()[0],
        )

    def test_old_events_keep_original_ids_and_sequence_numbers(self) -> None:
        before = self.connection.execute(
            "SELECT event_id, sequence_no, payload_json FROM exploration_events ORDER BY event_id"
        ).fetchall()

        by_attempt = self.repository.list_attempt_events("legacy-run:run-old")
        by_run = self.repository.list_run_attempt_events("run-old")

        self.assertEqual((4, 9), tuple(event.sequence_no for event in by_attempt))
        self.assertEqual(
            tuple(event.event_id for event in by_attempt),
            tuple(event.event_id for event in by_run),
        )
        self.assertEqual(
            (9,),
            tuple(
                event.sequence_no
                for event in self.repository.list_attempt_events(
                    "legacy-run:run-old", after_sequence=4
                )
            ),
        )
        after = self.connection.execute(
            "SELECT event_id, sequence_no, payload_json FROM exploration_events ORDER BY event_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
        self.assertEqual(
            0,
            self.connection.execute("SELECT COUNT(*) FROM exploration_attempt_events").fetchone()[0],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
