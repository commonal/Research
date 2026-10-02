from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from research_pulse.workbench.sessions import ResearchSession, SessionLifecycle
from research_pulse.workbench.models import ExplorationRun, ExplorationStatus
from research_pulse.workbench.models import Paper, ParseStatus, PdfStatus
from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sqlite import (
    SQLiteWorkbenchRepository,
    create_schema,
    open_workbench_database,
    recover_interrupted_work,
)


class WorkbenchSQLiteSchemaTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_schema_contains_every_planned_workbench_aggregate(self) -> None:
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertTrue(
            {
                "sessions",
                "papers",
                "paper_sources",
                "session_papers",
                "messages",
                "message_contexts",
                "citation_refs",
                "exploration_runs",
                "exploration_events",
                "candidate_findings",
                "note_runs",
            }.issubset(tables)
        )

    def test_session_repository_round_trip_and_workspace_paper_sharing(self) -> None:
        created_at = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        s1 = ResearchSession(
            session_id="session-1", research_question="问题", title="标题",
            created_at=created_at, workspace_id="workspace-1",
        )
        s2 = ResearchSession(
            session_id="session-2", research_question="问题2", title="标题2",
            created_at=created_at, workspace_id="workspace-1",
        )
        self.repository.create(s1)
        self.repository.create(s2)
        self.repository.insert_paper_stub("paper-1", "sha256:abc")
        self.repository.link_paper("session-1", "paper-1")

        # Papers are shared at the WORKSPACE scope (Stage 2), so both sessions
        # under the same workspace see the same paper list.
        self.assertEqual(self.repository.get("session-1"), s1)
        self.assertEqual(self.repository.list_paper_ids("session-1"), ("paper-1",))
        self.assertEqual(self.repository.list_paper_ids("session-2"), ("paper-1",))

        archived = ResearchSession(
            session_id="session-1", research_question="问题", title="标题",
            created_at=created_at, lifecycle=SessionLifecycle.ARCHIVED,
            workspace_id="workspace-1",
        )
        self.repository.save(archived)
        self.assertEqual(self.repository.get("session-1"), archived)

        # Deleting one session does NOT remove the workspace's shared paper.
        self.repository.delete("session-1")
        self.assertIsNone(self.repository.get("session-1"))
        self.assertEqual(self.repository.list_paper_ids("session-2"), ("paper-1",))
        paper_count = self.connection.execute(
            "SELECT COUNT(*) FROM papers WHERE paper_id = 'paper-1'"
        ).fetchone()[0]
        self.assertEqual(paper_count, 1)

    def test_sessions_in_different_workspaces_have_isolated_papers(self) -> None:
        created_at = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        for sid, wid in (("session-a", "workspace-a"), ("session-b", "workspace-b")):
            self.repository.create(ResearchSession(
                session_id=sid, research_question="q", title=sid,
                created_at=created_at, workspace_id=wid,
            ))
        self.repository.insert_paper_stub("paper-a", "sha256:aa")
        self.repository.insert_paper_stub("paper-b", "sha256:bb")
        self.repository.link_paper("session-a", "paper-a")
        self.repository.link_paper("session-b", "paper-b")
        self.assertEqual(self.repository.list_paper_ids("session-a"), ("paper-a",))
        self.assertEqual(self.repository.list_paper_ids("session-b"), ("paper-b",))

    def test_create_schema_is_idempotent(self) -> None:
        create_schema(self.connection)
        create_schema(self.connection)

    def test_foreign_keys_uniqueness_and_failed_write_rollback(self) -> None:
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            self.repository.link_paper("missing-session", "missing-paper")

        self.repository.insert_paper_stub("paper-1", "sha256:one")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.insert_paper_stub("paper-2", "sha256:one")

        count = self.connection.execute(
            "SELECT COUNT(*) FROM papers WHERE source_identity = 'sha256:one'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_paper_can_be_resolved_by_registered_source_url(self) -> None:
        paper = Paper(
            paper_id="paper-url",
            source_identity="sha256:url",
            source_url="https://papers.example.test/paper.pdf",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.READY,
        )
        self.repository.upsert_paper(paper)
        self.repository.add_paper_source(
            paper.paper_id,
            paper.source_identity,
            paper.source_url,
        )

        self.assertEqual(
            self.repository.get_paper_by_source_url(paper.source_url),
            paper,
        )

    def test_attempts_are_preserved_and_sessions_are_isolated(self) -> None:
        for session_id in ("session-a", "session-b"):
            self.repository.create(
                ResearchSession(
                    session_id=session_id,
                    research_question="同一个问题",
                    title=session_id,
                    created_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
                )
            )
        attempt_ids = iter(("attempt-a1", "attempt-a2"))
        coordinator = ResearchRunCoordinator(
            self.repository,
            run_id_factory=lambda: "run-a",
            attempt_id_factory=lambda: next(attempt_ids),
        )
        created = coordinator.create(CreateRunCommand("session-a", "同一个问题", {}, {}))
        coordinator.record_attempt_outcome(
            created.current_attempt.attempt_id,
            AttemptStatus.RETRYABLE_FAILURE,
            expected_generation=created.current_attempt.generation,
        )
        coordinator.continue_run(
            created.run_id,
            expected_attempt_id=created.current_attempt.attempt_id,
            idempotency_key="continue-a",
        )

        self.assertEqual(
            tuple(run.run_id for run in self.repository.list_exploration_runs("session-a")),
            ("run-a",),
        )
        self.assertEqual(self.repository.list_exploration_runs("session-b"), ())
        self.assertEqual(
            (1, 2),
            tuple(attempt.attempt_no for attempt in coordinator.inspect("run-a").attempt_history),
        )

    def test_recovery_marks_interrupted_states_and_recovers_complete_material(self) -> None:
        now = "2026-08-31T12:00:00+00:00"
        self.repository.create(
            ResearchSession(
                session_id="session-recovery",
                research_question="恢复测试",
                title="恢复测试",
                created_at=datetime.fromisoformat(now),
            )
        )
        self.connection.executemany(
            """
            INSERT INTO papers (
                paper_id, source_identity, pdf_status, parse_status, material_root
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                ("paper-complete", "sha256:complete", "fetching", "parsing", "complete"),
                ("paper-missing", "sha256:missing", "fetching", "parsing", "missing"),
                ("paper-stable", "sha256:stable", "ready", "ready", "stable"),
            ),
        )
        self.connection.execute(
            """
            INSERT INTO messages VALUES (
                'message-running', 'session-recovery', 'assistant', '', 'none',
                'generating', ?
            )
            """,
            (now,),
        )
        self.connection.execute(
            """
            INSERT INTO exploration_runs (
                run_id, session_id, question_snapshot, status, attempt, created_at
            ) VALUES ('run-running', 'session-recovery', '问题', 'running', 1, ?)
            """,
            (now,),
        )
        self.connection.execute(
            """
            INSERT INTO note_runs (
                note_run_id, paper_id, triggering_session_id, status, created_at
            ) VALUES ('note-running', 'paper-stable', 'session-recovery', 'generating', ?)
            """,
            (now,),
        )
        self.connection.commit()

        summary = recover_interrupted_work(
            self.connection,
            material_is_complete=lambda material_root: material_root == "complete",
        )

        papers = {
            row["paper_id"]: (row["pdf_status"], row["parse_status"])
            for row in self.connection.execute(
                "SELECT paper_id, pdf_status, parse_status FROM papers"
            )
        }
        self.assertEqual(papers["paper-complete"], ("ready", "ready"))
        self.assertEqual(papers["paper-missing"], ("failed", "failed"))
        self.assertEqual(papers["paper-stable"], ("ready", "ready"))
        self.assertEqual(
            self.connection.execute(
                "SELECT generation_status FROM messages WHERE message_id = 'message-running'"
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM exploration_runs WHERE run_id = 'run-running'"
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM note_runs WHERE note_run_id = 'note-running'"
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(summary.recovered_materials, 1)
        self.assertEqual(summary.failed_materials, 1)
        self.assertEqual(summary.failed_runs, 3)

    def test_recovery_also_closes_queued_chat_and_preserves_existing_error(self) -> None:
        now = "2026-08-31T12:00:00+00:00"
        self.repository.create(
            ResearchSession(
                session_id="session-queued-recovery",
                research_question="恢复排队回答",
                title="恢复排队回答",
                created_at=datetime.fromisoformat(now),
            )
        )
        self.connection.executemany(
            """
            INSERT INTO messages (
                message_id, session_id, role, text, scope, generation_status, created_at
            ) VALUES (?, ?, 'assistant', '', 'selection', ?, ?)
            """,
            (
                ("message-queued", "session-queued-recovery", "queued", now),
                ("message-failed", "session-queued-recovery", "failed", now),
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO message_metadata (message_id, metadata_json, safe_error)
            VALUES (?, '{}', ?)
            """,
            (("message-queued", None), ("message-failed", "模型生成失败，可重试")),
        )
        self.connection.commit()

        summary = recover_interrupted_work(
            self.connection,
            material_is_complete=lambda _material_root: False,
        )

        self.assertEqual(
            self.connection.execute(
                "SELECT generation_status FROM messages WHERE message_id = 'message-queued'"
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT safe_error FROM message_metadata WHERE message_id = 'message-queued'"
            ).fetchone()[0],
            "进程重启前任务未完成，可重试",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT safe_error FROM message_metadata WHERE message_id = 'message-failed'"
            ).fetchone()[0],
            "模型生成失败，可重试",
        )
        self.assertEqual(summary.failed_runs, 1)

    def test_recovery_closes_queued_note_runs_after_process_restart(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            repository = SQLiteWorkbenchRepository(connection)
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "INSERT INTO sessions (session_id, title, lifecycle, created_at, layout_json) VALUES (?, ?, ?, ?, ?)",
                ("session-note-queued", "笔记", "active", now, "{}"),
            )
            connection.execute(
                "INSERT INTO papers (paper_id, source_identity, pdf_status, parse_status) VALUES (?, ?, ?, ?)",
                ("paper-note-queued", "sha256:queued", "ready", "ready"),
            )
            connection.execute(
                "INSERT INTO note_runs (note_run_id, paper_id, triggering_session_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
                ("note-queued", "paper-note-queued", "session-note-queued", "queued", now),
            )
            connection.commit()

            summary = recover_interrupted_work(connection, material_is_complete=lambda _: False)

            self.assertEqual(summary.failed_runs, 1)
            row = connection.execute(
                "SELECT status, stage, safe_error FROM note_runs WHERE note_run_id = ?",
                ("note-queued",),
            ).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["stage"], "failed")
            self.assertIn("进程重启", row["safe_error"])
        finally:
            connection.close()


class WorkbenchSQLiteFileTests(TestCase):
    def test_file_database_uses_configured_path_wal_and_foreign_keys(self) -> None:
        database_path = Path("tests/.workbench-configurable-path-test.sqlite3")
        sidecars = (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        )
        for path in sidecars:
            path.unlink(missing_ok=True)
        try:
            connection = open_workbench_database(database_path)
            try:
                self.assertTrue(database_path.is_file())
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                connection.close()
        finally:
            for path in sidecars:
                path.unlink(missing_ok=True)
