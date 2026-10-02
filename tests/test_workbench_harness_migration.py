from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from research_pulse.workbench.sqlite import (
    WorkbenchMigrationError,
    create_schema,
    open_workbench_database,
)
from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.tool_execution import (
    Retryability, SideEffectState, ToolErrorCode, ToolOutcome, ToolOutcomeStatus,
)


class WorkbenchHarnessMigrationTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def columns(self, table: str) -> set[str]:
        return {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def test_durable_harness_schema_contains_lease_outcome_and_journal_identity(self) -> None:
        tables = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("exploration_tool_outcomes", tables)
        self.assertTrue({"generation", "lease_owner", "lease_expires_at"}.issubset(
            self.columns("exploration_attempts")
        ))
        self.assertTrue({"generation", "tool_call_id", "resource_key"}.issubset(
            self.columns("exploration_operations")
        ))

    def test_tool_outcome_requires_existing_attempt_and_unique_call_identity(self) -> None:
        columns = self.columns("exploration_tool_outcomes")
        values = {
            "tool_call_id": "call-1",
            "attempt_id": "missing-attempt",
            "tool_name": "search_sources",
            "status": "terminal_failure",
            "retryability": "never",
            "side_effect_state": "none",
            "started_at": "2026-09-04T00:00:00+00:00",
            "finished_at": "2026-09-04T00:00:01+00:00",
        }
        selected = tuple(key for key in values if key in columns)
        placeholders = ", ".join("?" for _ in selected)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                f"INSERT INTO exploration_tool_outcomes ({', '.join(selected)}) VALUES ({placeholders})",
                tuple(values[key] for key in selected),
            )

    def test_tool_outcome_round_trip_and_duplicate_write_rolls_back(self) -> None:
        repository = SQLiteWorkbenchRepository(self.connection)
        repository.create(ResearchSession(
            "session-1", "结果持久化", "结果", datetime(2026, 9, 4, tzinfo=UTC)
        ))
        snapshot = ResearchRunCoordinator(
            repository,
            run_id_factory=lambda: "run-1",
            attempt_id_factory=lambda: "attempt-1",
        ).create(CreateRunCommand("session-1", "问题", {}, {"tool_calls": 2}))
        outcome = ToolOutcome(
            "call-1", snapshot.current_attempt.attempt_id, "search_sources",
            ToolOutcomeStatus.RETRYABLE_FAILURE, ToolErrorCode.SERVICE_UNAVAILABLE,
            Retryability.AUTOMATIC, SideEffectState.NONE,
            datetime(2026, 9, 4, tzinfo=UTC),
            datetime(2026, 9, 4, 0, 0, 1, tzinfo=UTC),
            safe_message="服务暂时不可用", diagnostic_id="diag-1",
        )

        repository.save_tool_outcome(outcome)
        self.assertEqual(outcome, repository.get_tool_outcome("attempt-1", "call-1"))
        with self.assertRaises(sqlite3.IntegrityError):
            repository.save_tool_outcome(outcome)
        self.assertEqual(outcome, repository.get_tool_outcome("attempt-1", "call-1"))


class WorkbenchMigrationFailureTests(TestCase):
    database_path = Path("tests/.workbench-broken-migration.sqlite3")

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm", ".pre-migration.bak"):
            self.database_path.with_name(f"{self.database_path.name}{suffix}").unlink(
                missing_ok=True
            )

    def _create_legacy_database(self) -> sqlite3.Connection:
        self.tearDown()
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                research_question TEXT,
                title TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                created_at TEXT NOT NULL,
                layout_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE exploration_runs (
                run_id TEXT,
                session_id TEXT NOT NULL,
                question_snapshot TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                previous_run_id TEXT,
                budget_json TEXT NOT NULL DEFAULT '{}',
                final_draft TEXT,
                safe_error TEXT,
                stage TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL
            );
            CREATE TABLE exploration_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO sessions VALUES (
                'session-old', '旧问题', '旧工作台', 'active',
                '2026-09-01T00:00:00+00:00', '{}'
            );
            """
        )
        return connection

    def _assert_original_is_readable_and_unmigrated(self, expected_runs: int) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                expected_runs,
                connection.execute("SELECT COUNT(*) FROM exploration_runs").fetchone()[0],
            )
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='exploration_attempts'"
            ).fetchone())
            self.assertEqual(0, connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def test_duplicate_legacy_identity_creates_backup_and_refuses_new_write_mode(self) -> None:
        connection = self._create_legacy_database()
        row = (
            "session-old", "重复身份", "{}", "failed", 1, "{}",
            "failed", "2026-09-01T00:00:00+00:00",
        )
        connection.executemany(
            """INSERT INTO exploration_runs (
                session_id, question_snapshot, config_json, status, attempt,
                budget_json, stage, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row, row),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(WorkbenchMigrationError, "duplicate legacy Run identity") as raised:
            open_workbench_database(self.database_path)

        self.assertTrue(raised.exception.backup_path.is_file())
        self._assert_original_is_readable_and_unmigrated(2)

    def test_damaged_event_rolls_back_and_preserves_original_bytes_for_reading(self) -> None:
        connection = self._create_legacy_database()
        connection.execute(
            """INSERT INTO exploration_runs VALUES (
                'run-old', 'session-old', '问题', '{}', 'failed', 1,
                NULL, '{}', NULL, '失败', 'failed', '2026-09-01T00:00:00+00:00'
            )"""
        )
        connection.execute(
            """INSERT INTO exploration_events (
                run_id, sequence_no, event_type, payload_json, created_at
            ) VALUES ('run-old', 1, 'run_failed', '{broken',
                      '2026-09-01T00:00:01+00:00')"""
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(WorkbenchMigrationError, "damaged event") as raised:
            open_workbench_database(self.database_path)

        backup = sqlite3.connect(raised.exception.backup_path)
        try:
            self.assertEqual("{broken", backup.execute(
                "SELECT payload_json FROM exploration_events"
            ).fetchone()[0])
        finally:
            backup.close()
        self._assert_original_is_readable_and_unmigrated(1)

    def test_mid_migration_ddl_failure_restores_schema_from_backup(self) -> None:
        connection = self._create_legacy_database()
        # SCHEMA_SQL creates research_workspaces before it reaches papers.  A
        # legacy view using that name forces a failure after migration writes
        # have started and proves restoration is not merely preflight refusal.
        connection.execute("CREATE VIEW papers AS SELECT 'legacy-paper' AS paper_id")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(WorkbenchMigrationError, "original database was restored"):
            open_workbench_database(self.database_path)

        restored = sqlite3.connect(self.database_path)
        try:
            self.assertIsNone(restored.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='research_workspaces'"
            ).fetchone())
            self.assertEqual(
                "legacy-paper", restored.execute("SELECT paper_id FROM papers").fetchone()[0]
            )
        finally:
            restored.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
