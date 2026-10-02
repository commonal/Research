"""SQLite persistence for workbench metadata; source material never lives here."""

from __future__ import annotations

import sqlite3
import threading
import json
import shutil
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from research_pulse.workbench.agent_runtime import SafeAgentEvent
from research_pulse.workbench.models import CandidateFinding, ExplorationRun, ExplorationStatus
from research_pulse.workbench.run_models import Attempt, AttemptLease, AttemptStatus
from research_pulse.workbench.run_events import PersistedEvent, UnsequencedEvent
from research_pulse.workbench.tool_execution import (
    OperationEffectUnknownError,
    Retryability,
    SideEffectState,
    ToolErrorCode,
    ToolOutcome,
    ToolOutcomeStatus,
)
from research_pulse.workbench.sessions import ResearchSession, SessionLifecycle
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    TurnAuditRecord,
)
from research_pulse.workbench.turn_events import TurnEvent, UnsequencedTurnEvent


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    research_question TEXT,
    title TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    workspace_id TEXT,
    layout_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS research_workspaces (
    workspace_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '未命名研究工作区',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    source_identity TEXT NOT NULL UNIQUE,
    source_url TEXT,
    pdf_path TEXT,
    pdf_status TEXT NOT NULL DEFAULT 'absent',
    parse_status TEXT NOT NULL DEFAULT 'idle',
    material_root TEXT,
    safe_error TEXT
);

CREATE TABLE IF NOT EXISTS paper_sources (
    source_alias TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS session_papers (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE RESTRICT,
    attached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, paper_id)
);

CREATE TABLE IF NOT EXISTS workspace_papers (
    workspace_id TEXT NOT NULL,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE RESTRICT,
    attached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, paper_id)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'none',
    generation_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_metadata (
    message_id TEXT PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    safe_error TEXT
);

CREATE TABLE IF NOT EXISTS message_contexts (
    context_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    paper_id TEXT REFERENCES papers(paper_id) ON DELETE SET NULL,
    scope TEXT NOT NULL,
    block_id TEXT,
    section_path TEXT,
    locator_json TEXT,
    truncated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turn_audits (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    client_request_id TEXT,
    capability TEXT NOT NULL,
    retrieval_plan TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    evidence_scope TEXT NOT NULL,
    reason TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    confidence REAL NOT NULL,
    decision_status TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    turn_status TEXT NOT NULL,
    run_id TEXT,
    attempt_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, client_request_id)
);

CREATE TABLE IF NOT EXISTS turn_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL REFERENCES turn_audits(turn_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS citation_refs (
    citation_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    paper_id TEXT REFERENCES papers(paper_id) ON DELETE SET NULL,
    block_id TEXT,
    status TEXT NOT NULL,
    locator_json TEXT
);

CREATE TABLE IF NOT EXISTS exploration_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    question_snapshot TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    previous_run_id TEXT REFERENCES exploration_runs(run_id) ON DELETE SET NULL,
    budget_json TEXT NOT NULL DEFAULT '{}',
    final_draft TEXT,
    safe_error TEXT,
    stage TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    UNIQUE (session_id, attempt)
);

CREATE TABLE IF NOT EXISTS exploration_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES exploration_runs(run_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS exploration_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES exploration_runs(run_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    safe_error TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    budget_json TEXT NOT NULL DEFAULT '{}',
    budget_used_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT,
    UNIQUE (run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS exploration_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL REFERENCES exploration_attempts(attempt_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, step_id)
);

CREATE TABLE IF NOT EXISTS exploration_operations (
    operation_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES exploration_attempts(attempt_id) ON DELETE CASCADE,
    operation_kind TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    tool_call_id TEXT,
    resource_key TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'committed', 'failed', 'effect_unknown')),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exploration_tool_outcomes (
    attempt_id TEXT NOT NULL REFERENCES exploration_attempts(attempt_id) ON DELETE CASCADE,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'succeeded', 'rejected', 'retryable_failure', 'terminal_failure',
        'cancelled', 'effect_unknown'
    )),
    error_code TEXT,
    retryability TEXT NOT NULL,
    side_effect_state TEXT NOT NULL,
    operation_id TEXT REFERENCES exploration_operations(operation_id) ON DELETE SET NULL,
    retry_after_seconds REAL,
    safe_message TEXT,
    diagnostic_id TEXT,
    bounded_result_reference TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    PRIMARY KEY (attempt_id, tool_call_id)
);

CREATE TABLE IF NOT EXISTS exploration_idempotency (
    run_id TEXT NOT NULL REFERENCES exploration_runs(run_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    attempt_id TEXT NOT NULL REFERENCES exploration_attempts(attempt_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, action, idempotency_key)
);

CREATE TABLE IF NOT EXISTS exploration_attempt_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL REFERENCES exploration_attempts(attempt_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (attempt_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS candidate_findings (
    finding_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES exploration_runs(run_id) ON DELETE CASCADE,
    claim TEXT NOT NULL,
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    read_block_ids_json TEXT NOT NULL DEFAULT '[]',
    source_authority_json TEXT NOT NULL DEFAULT '{}',
    citation_statuses_json TEXT NOT NULL DEFAULT '{}',
    verification_status TEXT NOT NULL DEFAULT 'candidate'
);

CREATE TABLE IF NOT EXISTS note_runs (
    note_run_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE RESTRICT,
    triggering_session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    previous_note_run_id TEXT REFERENCES note_runs(note_run_id) ON DELETE SET NULL,
    receipt_path TEXT,
    draft_path TEXT,
    knowledge_id TEXT,
    safe_error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_run_id TEXT NOT NULL REFERENCES note_runs(note_run_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (note_run_id, sequence_no)
);
"""


SCHEMA_VERSION = 1


class WorkbenchMigrationError(RuntimeError):
    """The on-disk database was left on its old readable schema."""

    def __init__(self, message: str, *, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _validate_pre_migration_data(connection: sqlite3.Connection) -> None:
    """Reject ambiguous identities and unreadable events before any schema write."""
    identity_checks = (
        ("exploration_runs", ("session_id", "attempt"), "legacy Run identity"),
        ("exploration_attempts", ("run_id", "attempt_no"), "Attempt identity"),
        ("exploration_events", ("run_id", "sequence_no"), "legacy event identity"),
        (
            "exploration_attempt_events",
            ("attempt_id", "sequence_no"),
            "Attempt event identity",
        ),
    )
    for table, columns, label in identity_checks:
        if not _table_exists(connection, table):
            continue
        column_list = ", ".join(columns)
        duplicate = connection.execute(
            f"SELECT {column_list}, COUNT(*) FROM {table} "
            f"GROUP BY {column_list} HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise WorkbenchMigrationError(f"duplicate {label}")

    event_tables = (
        ("exploration_events", "payload_json"),
        ("exploration_attempt_events", "payload_json"),
    )
    for table, payload_column in event_tables:
        if not _table_exists(connection, table):
            continue
        rows = connection.execute(
            f"SELECT sequence_no, event_type, {payload_column} FROM {table}"
        ).fetchall()
        for sequence_no, event_type, payload_json in rows:
            if sequence_no < 1 or not str(event_type).strip():
                raise WorkbenchMigrationError(f"damaged event in {table}")
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise WorkbenchMigrationError(f"damaged event in {table}") from exc
            if not isinstance(payload, dict) or not str(payload.get("summary", "")).strip():
                raise WorkbenchMigrationError(f"damaged event in {table}")


def _create_validated_backup(
    connection: sqlite3.Connection, database_path: Path
) -> Path:
    backup_path = database_path.with_name(f"{database_path.name}.pre-migration.bak")
    backup_path.unlink(missing_ok=True)
    backup = sqlite3.connect(backup_path)
    try:
        connection.backup(backup)
        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise WorkbenchMigrationError("migration backup failed integrity check")
    finally:
        backup.close()
    return backup_path


def _restore_migration_backup(database_path: Path, backup_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        database_path.with_name(f"{database_path.name}{suffix}").unlink(missing_ok=True)
    shutil.copy2(backup_path, database_path)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    migrations = {
        "papers": {"title": "TEXT"},
        "candidate_findings": {
            "source_authority_json": "TEXT NOT NULL DEFAULT '{}'",
            "citation_statuses_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "note_runs": {
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "previous_note_run_id": "TEXT",
            "draft_path": "TEXT",
            "stage": "TEXT NOT NULL DEFAULT 'queued'",
        },
        "sessions": {
            "workspace_id": "TEXT",
        },
        "exploration_attempts": {
            "generation": "INTEGER NOT NULL DEFAULT 0",
            "input_json": "TEXT NOT NULL DEFAULT '{}'",
            "budget_json": "TEXT NOT NULL DEFAULT '{}'",
            "budget_used_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_at": "TEXT",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
        },
        "exploration_operations": {
            "generation": "INTEGER NOT NULL DEFAULT 0",
            "tool_call_id": "TEXT",
            "resource_key": "TEXT",
        },
    }
    for table, columns in migrations.items():
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    # One-time data migration: lift legacy per-session paper links to the
    # workspace scope so sessions under the same workspace share papers.
    connection.execute(
        """
        INSERT OR IGNORE INTO workspace_papers (workspace_id, paper_id, attached_at)
        SELECT s.workspace_id, sp.paper_id, sp.attached_at
        FROM session_papers sp
        JOIN sessions s ON s.session_id = sp.session_id
        WHERE s.workspace_id IS NOT NULL
        """
    )
    connection.commit()


def open_workbench_database(path: str | Path) -> sqlite3.Connection:
    """Open a production metadata database at an explicit, configurable path."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI executes synchronous endpoints in a worker thread even though
    # lifespan owns the connection in the event-loop thread. The runtime is
    # still single-process/single-owner; short repository transactions bound
    # each cross-thread use.
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    backup_path: Path | None = None
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        has_schema = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
        ).fetchone() is not None
        if has_schema and schema_version < SCHEMA_VERSION:
            backup_path = _create_validated_backup(connection, database_path)
            _validate_pre_migration_data(connection)
        create_schema(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise WorkbenchMigrationError("migrated database failed integrity check")
        foreign_key_failure = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_failure is not None:
            raise WorkbenchMigrationError("migrated database has broken references")
        connection.commit()
        connection.execute("PRAGMA journal_mode = WAL")
        return connection
    except Exception as exc:
        connection.close()
        if backup_path is not None and backup_path.exists():
            _restore_migration_backup(database_path, backup_path)
        if isinstance(exc, WorkbenchMigrationError):
            raise WorkbenchMigrationError(str(exc), backup_path=backup_path) from exc
        raise WorkbenchMigrationError(
            "workbench migration failed; original database was restored",
            backup_path=backup_path,
        ) from exc


@dataclass(frozen=True)
class RecoverySummary:
    recovered_materials: int
    failed_materials: int
    failed_runs: int


def recover_interrupted_work(
    connection: sqlite3.Connection,
    *,
    material_is_complete: Callable[[str], bool],
) -> RecoverySummary:
    """Recover only transient states left behind by a stopped process."""
    interrupted = "进程重启前任务未完成，可重试"
    recovered_materials = 0
    failed_materials = 0
    with connection:
        rows = connection.execute(
            """
            SELECT paper_id, material_root FROM papers
            WHERE pdf_status = 'fetching' OR parse_status IN ('queued', 'parsing')
            """
        ).fetchall()
        for row in rows:
            material_root = row["material_root"]
            if material_root and material_is_complete(material_root):
                connection.execute(
                    """
                    UPDATE papers
                    SET pdf_status = 'ready', parse_status = 'ready', safe_error = NULL
                    WHERE paper_id = ?
                    """,
                    (row["paper_id"],),
                )
                recovered_materials += 1
            else:
                connection.execute(
                    """
                    UPDATE papers
                    SET pdf_status = CASE
                            WHEN pdf_status = 'fetching' THEN 'failed' ELSE pdf_status END,
                        parse_status = CASE
                            WHEN parse_status IN ('queued', 'parsing') THEN 'failed'
                            ELSE parse_status END,
                        safe_error = ?
                    WHERE paper_id = ?
                    """,
                    (interrupted, row["paper_id"]),
                )
                failed_materials += 1

        # Paper answers use a short-lived in-process worker.  A process stop
        # can therefore leave either the tiny queued window or the provider
        # call in ``generating``.  Both states are terminally recoverable and
        # must not make the browser poll forever after a restart.
        pending_message_ids = [
            row["message_id"]
            for row in connection.execute(
                "SELECT message_id FROM messages WHERE generation_status IN ('queued', 'generating')"
            ).fetchall()
        ]
        message_cursor = connection.execute(
            """
            UPDATE messages
            SET generation_status = 'failed'
            WHERE generation_status IN ('queued', 'generating')
            """
        )
        for message_id in pending_message_ids:
            connection.execute(
                "UPDATE message_metadata SET safe_error = ? WHERE message_id = ?",
                (interrupted, message_id),
            )
        exploration_cursor = connection.execute(
            """
            UPDATE exploration_runs SET status = 'failed', safe_error = ?
            WHERE status = 'running'
            """,
            (interrupted,),
        )
        note_cursor = connection.execute(
            """
            UPDATE note_runs SET status = 'failed', stage = 'failed', safe_error = ?
            WHERE status IN ('queued', 'generating')
            """,
            (interrupted,),
        )

    return RecoverySummary(
        recovered_materials=recovered_materials,
        failed_materials=failed_materials,
        failed_runs=(
            message_cursor.rowcount + exploration_cursor.rowcount + note_cursor.rowcount
        ),
    )


class SQLiteWorkbenchRepository:
    """Small repository implementing the public SessionRepository seam."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._lock = threading.RLock()
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        create_schema(self.connection)

    def __getattribute__(self, name):
        """Serialize shared-connection calls across FastAPI worker threads.

        The production app owns one SQLite connection, while synchronous
        endpoints may run concurrently in different worker threads. SQLite's
        connection object is not safe for overlapping cursor operations even
        with ``check_same_thread=False``; a re-entrant repository lock keeps
        each short transaction and read atomic without changing the public
        repository seam.
        """
        value = object.__getattribute__(self, name)
        if name.startswith("_") or name in {"connection"} or not callable(value):
            return value
        lock = object.__getattribute__(self, "_lock")
        def locked(*args, **kwargs):
            with lock:
                return value(*args, **kwargs)
        return locked

    def create(self, session: ResearchSession) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sessions (
                    session_id, research_question, title, lifecycle, created_at, workspace_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.research_question,
                    session.title,
                    session.lifecycle.value,
                    session.created_at.isoformat(),
                    session.workspace_id,
                ),
            )

    def _to_session(self, row: sqlite3.Row) -> ResearchSession:
        layout = json.loads(row["layout_json"])
        return ResearchSession(
            session_id=row["session_id"],
            research_question=row["research_question"],
            title=row["title"],
            lifecycle=SessionLifecycle(row["lifecycle"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            paper_panel_open=bool(layout.get("paper_panel_open", False)),
            active_paper_id=layout.get("active_paper_id"),
            workspace_id=row["workspace_id"],
        )

    def get(self, session_id: str) -> ResearchSession | None:
        row = self.connection.execute(
            """
            SELECT session_id, research_question, title, lifecycle, created_at, workspace_id, layout_json
            FROM sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return self._to_session(row)

    def list(self, lifecycle: SessionLifecycle | None = None) -> tuple[ResearchSession, ...]:
        if lifecycle is None:
            rows = self.connection.execute(
                "SELECT session_id FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT session_id FROM sessions WHERE lifecycle = ? ORDER BY created_at DESC",
                (lifecycle.value,),
            ).fetchall()
        return tuple(self.get(row["session_id"]) for row in rows)  # type: ignore[misc]

    def save(self, session: ResearchSession) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE sessions
                SET research_question = ?, title = ?, lifecycle = ?, workspace_id = ?, layout_json = ?
                WHERE session_id = ?
                """,
                (
                    session.research_question,
                    session.title,
                    session.lifecycle.value,
                    session.workspace_id,
                    json.dumps(
                        {
                            "paper_panel_open": session.paper_panel_open,
                            "active_paper_id": session.active_paper_id,
                        },
                        ensure_ascii=False,
                    ),
                    session.session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(session.session_id)

    def delete(self, session_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )

    def insert_chat_message(self, message: "ChatMessageRecord") -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO messages (
                    message_id, session_id, role, text, scope,
                    generation_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id, message.session_id, message.role, message.text,
                    message.scope, message.generation_status, message.created_at.isoformat(),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO message_metadata (message_id, metadata_json, safe_error)
                VALUES (?, ?, ?)
                """,
                (
                    message.message_id,
                    json.dumps(message.metadata, ensure_ascii=False),
                    message.safe_error,
                ),
            )

    def save_chat_message(self, message: "ChatMessageRecord") -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages SET text = ?, generation_status = ? WHERE message_id = ?
                """,
                (
                    message.text, message.generation_status, message.message_id,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO message_metadata (message_id, metadata_json, safe_error)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    metadata_json = excluded.metadata_json,
                    safe_error = excluded.safe_error
                """,
                (
                    message.message_id,
                    json.dumps(message.metadata, ensure_ascii=False),
                    message.safe_error,
                ),
            )

    def list_chat_messages(self, session_id: str) -> tuple["ChatMessageRecord", ...]:
        from research_pulse.workbench.chat import ChatMessageRecord

        rows = self.connection.execute(
            """
            SELECT m.message_id, m.session_id, m.role, m.text, m.scope,
                   m.generation_status, m.created_at,
                   COALESCE(mm.metadata_json, '{}') AS metadata_json, mm.safe_error
            FROM messages AS m
            LEFT JOIN message_metadata AS mm ON mm.message_id = m.message_id
            WHERE m.session_id = ? ORDER BY m.created_at, m.rowid
            """,
            (session_id,),
        ).fetchall()
        messages = tuple(
            ChatMessageRecord(
                message_id=row["message_id"], session_id=row["session_id"],
                role=row["role"], text=row["text"], scope=row["scope"],
                generation_status=row["generation_status"],
                created_at=datetime.fromisoformat(row["created_at"]),
                metadata=json.loads(row["metadata_json"] or "{}"),
                safe_error=row["safe_error"],
                citations=self.list_citations(row["message_id"]),
            )
            for row in rows
        )
        return messages

    def replace_citations(
        self, message_id: str, citations: tuple["CitationRecord", ...]
    ) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM citation_refs WHERE message_id = ?", (message_id,)
            )
            self.connection.executemany(
                """
                INSERT INTO citation_refs (
                    citation_id, message_id, paper_id, block_id, status, locator_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.citation_id, item.message_id, item.paper_id, item.block_id,
                        item.status,
                        json.dumps(item.locator, ensure_ascii=False) if item.locator else None,
                    )
                    for item in citations
                ],
            )

    def list_citations(self, message_id: str) -> tuple["CitationRecord", ...]:
        from research_pulse.workbench.citations import CitationRecord

        rows = self.connection.execute(
            """
            SELECT citation_id, message_id, paper_id, block_id, status, locator_json
            FROM citation_refs WHERE message_id = ? ORDER BY rowid
            """,
            (message_id,),
        ).fetchall()
        return tuple(
            CitationRecord(
                citation_id=row["citation_id"], message_id=row["message_id"],
                paper_id=row["paper_id"], block_id=row["block_id"],
                status=row["status"],
                locator=json.loads(row["locator_json"]) if row["locator_json"] else None,
            )
            for row in rows
        )

    def _session_workspace(self, session_id: str) -> str:
        session = self.get(session_id)
        if session is None:
            raise ValueError(f"session {session_id} not found")
        if session.workspace_id:
            self.ensure_workspace(session.workspace_id)
            return session.workspace_id
        # Legacy/directly-created sessions get a real workspace allocated lazily
        # (a genuine uuid, never derived from session_id).
        workspace_id = uuid.uuid4().hex
        self.save(replace(session, workspace_id=workspace_id))
        self.ensure_workspace(workspace_id)
        return workspace_id

    def ensure_workspace(self, workspace_id: str, title: str = "研究项目") -> str:
        with self.connection:
            self.connection.execute(
                "INSERT INTO research_workspaces (workspace_id, title, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(workspace_id) DO NOTHING",
                (workspace_id, title, datetime.now(UTC).isoformat()),
            )
        return workspace_id

    def get_workspace_title(self, workspace_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT title FROM research_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return row["title"] if row else None

    def rename_workspace(self, workspace_id: str, title: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE research_workspaces SET title = ? WHERE workspace_id = ?",
                (title, workspace_id),
            )

    def list_sessions_by_workspace(self, workspace_id: str) -> list[ResearchSession]:
        rows = self.connection.execute(
            """
            SELECT session_id, research_question, title, lifecycle, created_at, workspace_id, layout_json
            FROM sessions WHERE workspace_id = ? ORDER BY created_at
            """,
            (workspace_id,),
        ).fetchall()
        return [self._to_session(row) for row in rows]

    def delete_workspace(self, workspace_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM workspace_papers WHERE workspace_id = ?",
                (workspace_id,),
            )
            self.connection.execute(
                "DELETE FROM research_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            )

    def link_paper(self, session_id: str, paper_id: str) -> None:
        workspace_id = self._session_workspace(session_id)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO workspace_papers (workspace_id, paper_id) VALUES (?, ?)",
                (workspace_id, paper_id),
            )

    def list_paper_ids(self, session_id: str) -> tuple[str, ...]:
        workspace_id = self._session_workspace(session_id)
        rows = self.connection.execute(
            "SELECT paper_id FROM workspace_papers WHERE workspace_id = ? ORDER BY attached_at, paper_id",
            (workspace_id,),
        ).fetchall()
        return tuple(row["paper_id"] for row in rows)

    def unlink_paper(self, session_id: str, paper_id: str) -> None:
        workspace_id = self._session_workspace(session_id)
        with self.connection:
            self.connection.execute(
                "DELETE FROM workspace_papers WHERE workspace_id = ? AND paper_id = ?",
                (workspace_id, paper_id),
            )

    def has_paper_link(self, session_id: str, paper_id: str) -> bool:
        workspace_id = self._session_workspace(session_id)
        return (
            self.connection.execute(
                "SELECT 1 FROM workspace_papers WHERE workspace_id = ? AND paper_id = ?",
                (workspace_id, paper_id),
            ).fetchone()
            is not None
        )

    def insert_paper_stub(self, paper_id: str, source_identity: str) -> None:
        """Insert metadata-only paper identity; source bytes stay in Layer C."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO papers (paper_id, source_identity) VALUES (?, ?)",
                (paper_id, source_identity),
            )

    def upsert_paper(self, paper: "Paper") -> "Paper":
        from research_pulse.workbench.models import Paper

        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO papers (
                    paper_id, source_identity, source_url, pdf_path,
                    pdf_status, parse_status, material_root, safe_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paper.paper_id,
                    paper.source_identity,
                    paper.source_url,
                    paper.pdf_path,
                    paper.pdf_status.value,
                    paper.parse_status.value,
                    paper.material_root,
                    paper.safe_error,
                ),
            )
        if paper.title:
            with self.connection:
                self.connection.execute(
                    "UPDATE papers SET title = ? WHERE paper_id = ?",
                    (paper.title, paper.paper_id),
                )
        stored = self.get_paper(paper.paper_id)
        assert stored is not None
        return stored

    @staticmethod
    def _to_turn_audit(row: sqlite3.Row) -> TurnAuditRecord:
        return TurnAuditRecord(
            turn_id=row["turn_id"],
            session_id=row["session_id"],
            decision=CapabilityDecision(
                capability=row["capability"],
                retrieval_plan=row["retrieval_plan"],
                execution_mode=row["execution_mode"],
                allowed_tools=tuple(json.loads(row["allowed_tools_json"] or "[]")),
                evidence_scope=row["evidence_scope"],
                reason=row["reason"],
                rule_version=row["rule_version"],
                confidence=float(row["confidence"]),
                status=row["decision_status"],
            ),
            context_snapshot=json.loads(row["context_json"] or "{}"),
            client_request_id=row["client_request_id"],
            status=row["turn_status"],
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_turn_audit(self, turn_id: str) -> TurnAuditRecord | None:
        row = self.connection.execute(
            "SELECT * FROM turn_audits WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._to_turn_audit(row) if row is not None else None

    def create_or_get_turn_audit(self, record: TurnAuditRecord) -> TurnAuditRecord:
        """Insert a route decision once; repeated client requests return the original."""
        if record.client_request_id is not None:
            existing = self.connection.execute(
                "SELECT * FROM turn_audits WHERE session_id = ? AND client_request_id = ?",
                (record.session_id, record.client_request_id),
            ).fetchone()
            if existing is not None:
                return self._to_turn_audit(existing)
        existing = self.connection.execute(
            "SELECT * FROM turn_audits WHERE turn_id = ?", (record.turn_id,)
        ).fetchone()
        if existing is not None:
            return self._to_turn_audit(existing)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO turn_audits (
                    turn_id, session_id, client_request_id, capability, retrieval_plan,
                    execution_mode, allowed_tools_json, evidence_scope, reason,
                    rule_version, confidence, decision_status, context_json,
                    turn_status, run_id, attempt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.turn_id,
                    record.session_id,
                    record.client_request_id,
                    record.decision.capability,
                    record.decision.retrieval_plan,
                    record.decision.execution_mode,
                    json.dumps(list(record.decision.allowed_tools), ensure_ascii=False),
                    record.decision.evidence_scope,
                    record.decision.reason,
                    record.decision.rule_version,
                    record.decision.confidence,
                    record.decision.status,
                    json.dumps(dict(record.context_snapshot), ensure_ascii=False),
                    record.status,
                    record.run_id,
                    record.attempt_id,
                    record.created_at.isoformat(),
                ),
            )
        return record

    def save_turn_audit(self, record: TurnAuditRecord) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE turn_audits SET
                    turn_status = ?, run_id = ?, attempt_id = ?,
                    context_json = ?, reason = ?, confidence = ?, decision_status = ?
                WHERE turn_id = ?
                """,
                (
                    record.status,
                    record.run_id,
                    record.attempt_id,
                    json.dumps(dict(record.context_snapshot), ensure_ascii=False),
                    record.decision.reason,
                    record.decision.confidence,
                    record.decision.status,
                    record.turn_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(record.turn_id)

    def recover_interrupted_background_turns(self) -> int:
        """Close in-process paper turns that cannot resume after a restart.

        Durable exploration attempts have their own lease/recovery protocol.
        Paper answers, by contrast, are deliberately kept in a small process
        pool for fast acknowledgement.  Leaving their audit rows queued or
        running after a restart would make an idempotent retry return a stale
        handle forever, even though the associated message was already marked
        failed by :func:`recover_interrupted_work`.
        """
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE turn_audits
                SET turn_status = 'failed'
                WHERE execution_mode = 'background'
                  AND turn_status IN ('queued', 'running')
                """
            )
        return int(cursor.rowcount)

    def append_turn_event(self, turn_id: str, event: UnsequencedTurnEvent) -> TurnEvent:
        """Persist an event and let SQLite assign the next turn-local sequence."""
        with self.connection:
            exists = self.connection.execute(
                "SELECT 1 FROM turn_audits WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(turn_id)
            current = self.connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) FROM turn_events WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()[0]
            sequence_no = int(current) + 1
            self.connection.execute(
                """
                INSERT INTO turn_events (
                    turn_id, sequence_no, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    sequence_no,
                    event.event_type,
                    json.dumps(
                        {"summary": event.summary, "stable_ids": dict(event.stable_ids),
                         "counters": dict(event.counters)},
                        ensure_ascii=False,
                    ),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return TurnEvent(
            sequence_no, event.event_type, event.summary,
            dict(event.stable_ids), dict(event.counters),
        )

    def list_turn_events(self, turn_id: str) -> tuple[TurnEvent, ...]:
        rows = self.connection.execute(
            "SELECT sequence_no, event_type, payload_json FROM turn_events "
            "WHERE turn_id = ? ORDER BY sequence_no",
            (turn_id,),
        ).fetchall()
        return tuple(
            TurnEvent(
                int(row["sequence_no"]),
                row["event_type"],
                json.loads(row["payload_json"])["summary"],
                json.loads(row["payload_json"]).get("stable_ids", {}),
                json.loads(row["payload_json"]).get("counters", {}),
            )
            for row in rows
        )

    def update_paper_title(self, paper_id: str, title: str):
        with self.connection:
            self.connection.execute("UPDATE papers SET title = ? WHERE paper_id = ?", (title, paper_id))
        return self.get_paper(paper_id)

    @staticmethod
    def _paper_from_row(row: sqlite3.Row) -> "Paper":
        from research_pulse.workbench.models import Paper, ParseStatus, PdfStatus

        return Paper(
            paper_id=row["paper_id"],
            source_identity=row["source_identity"],
            title=row["title"],
            source_url=row["source_url"],
            pdf_path=row["pdf_path"],
            pdf_status=PdfStatus(row["pdf_status"]),
            parse_status=ParseStatus(row["parse_status"]),
            material_root=row["material_root"],
            safe_error=row["safe_error"],
        )

    def list_papers_by_parse_status(self, statuses: Sequence[str]) -> tuple["Paper", ...]:
        """Papers whose parse never finished (queued/parsing across restarts)."""
        placeholders = ",".join("?" for _ in statuses)
        rows = self.connection.execute(
            f"""
            SELECT paper_id, source_identity, title, source_url, pdf_path, pdf_status,
                   parse_status, material_root, safe_error
            FROM papers WHERE parse_status IN ({placeholders}) ORDER BY rowid
            """,
            list(statuses),
        ).fetchall()
        return tuple(self._paper_from_row(row) for row in rows)

    def get_paper(self, paper_id: str) -> "Paper | None":
        row = self.connection.execute(
            """
            SELECT paper_id, source_identity, title, source_url, pdf_path, pdf_status,
                   parse_status, material_root, safe_error
            FROM papers WHERE paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
        if row is None:
            return None
        return self._paper_from_row(row)

    def save_paper(self, paper: "Paper") -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE papers SET title = ?, source_url = ?, pdf_path = ?, pdf_status = ?,
                    parse_status = ?, material_root = ?, safe_error = ?
                WHERE paper_id = ?
                """,
                (
                    paper.title,
                    paper.source_url,
                    paper.pdf_path,
                    paper.pdf_status.value,
                    paper.parse_status.value,
                    paper.material_root,
                    paper.safe_error,
                    paper.paper_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(paper.paper_id)

    def add_paper_source(
        self,
        paper_id: str,
        source_alias: str,
        source_url: str | None = None,
    ) -> None:
        with self.connection:
            existing = self.connection.execute(
                "SELECT paper_id FROM paper_sources WHERE source_alias = ?",
                (source_alias,),
            ).fetchone()
            if existing is not None and existing["paper_id"] != paper_id:
                raise sqlite3.IntegrityError(
                    "source alias is already bound to different PDF content"
                )
            self.connection.execute(
                """
                INSERT INTO paper_sources (source_alias, paper_id, source_url)
                VALUES (?, ?, ?)
                ON CONFLICT(source_alias) DO UPDATE SET
                    source_url = COALESCE(excluded.source_url, paper_sources.source_url)
                """,
                (source_alias, paper_id, source_url),
            )

    def get_paper_by_source_alias(self, source_alias: str):
        row = self.connection.execute(
            "SELECT p.* FROM papers p JOIN paper_sources s ON s.paper_id = p.paper_id WHERE s.source_alias = ?",
            (source_alias,),
        ).fetchone()
        return self._paper_from_row(row) if row is not None else None

    def get_paper_by_source_url(self, source_url: str):
        """Resolve a paper by the canonical URL recorded during ingestion.

        arXiv imports have a stable source alias, but generic public PDF/DOI
        imports use a content-addressed paper identity and retain the URL in
        both ``papers`` and ``paper_sources``.  Keeping URL resolution here
        avoids making the API guess a hash that it does not own.
        """
        row = self.connection.execute(
            "SELECT * FROM papers WHERE source_url = ? LIMIT 1",
            (source_url,),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                """
                SELECT p.*
                FROM papers AS p
                JOIN paper_sources AS s ON s.paper_id = p.paper_id
                WHERE s.source_url = ?
                LIMIT 1
                """,
                (source_url,),
            ).fetchone()
        return self._paper_from_row(row) if row is not None else None

    def list_paper_sources(self, paper_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT source_alias FROM paper_sources WHERE paper_id = ? ORDER BY source_alias",
            (paper_id,),
        ).fetchall()
        return tuple(row["source_alias"] for row in rows)

    def list_exploration_runs(self, session_id: str) -> tuple[ExplorationRun, ...]:
        rows = self.connection.execute(
            """
            SELECT run_id, session_id, question_snapshot, attempt, status,
                   config_json, budget_json, previous_run_id, safe_error, final_draft,
                   created_at
            FROM exploration_runs
            WHERE session_id = ?
            ORDER BY attempt
            """,
            (session_id,),
        ).fetchall()
        return tuple(
            ExplorationRun(
                run_id=row["run_id"],
                session_id=row["session_id"],
                question_snapshot=row["question_snapshot"],
                attempt=row["attempt"],
                status=ExplorationStatus(row["status"]),
                config_snapshot=json.loads(row["config_json"]),
                budgets=json.loads(row["budget_json"]),
                previous_run_id=row["previous_run_id"],
                safe_error=row["safe_error"],
                final_draft=row["final_draft"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    def get_exploration_run(self, run_id: str) -> ExplorationRun | None:
        row = self.connection.execute(
            """
            SELECT run_id, session_id, question_snapshot, attempt, status,
                   config_json, budget_json, previous_run_id, safe_error, final_draft,
                   created_at
            FROM exploration_runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return ExplorationRun(
            run_id=row["run_id"],
            session_id=row["session_id"],
            question_snapshot=row["question_snapshot"],
            attempt=row["attempt"],
            status=ExplorationStatus(row["status"]),
            config_snapshot=json.loads(row["config_json"]),
            budgets=json.loads(row["budget_json"]),
            previous_run_id=row["previous_run_id"],
            safe_error=row["safe_error"],
            final_draft=row["final_draft"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def insert_exploration_run(self, run: ExplorationRun) -> None:
        """Persist a legacy/read-model run fixture without scheduling execution."""
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO exploration_runs (
                    run_id, session_id, question_snapshot, config_json, status,
                    attempt, previous_run_id, budget_json, final_draft,
                    safe_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.session_id,
                    run.question_snapshot,
                    json.dumps(run.config_snapshot, ensure_ascii=False),
                    run.status.value,
                    run.attempt,
                    run.previous_run_id,
                    json.dumps(run.budgets, ensure_ascii=False),
                    run.final_draft,
                    run.safe_error,
                    run.created_at.isoformat() if run.created_at else datetime.now(UTC).isoformat(),
                ),
            )

    def insert_coordinated_run(self, run: ExplorationRun, attempt: Attempt) -> None:
        """Atomically persist a logical run and its first queued attempt."""
        if attempt.run_id != run.run_id or attempt.attempt_no != 1:
            raise ValueError("first attempt must belong to the run and use number 1")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO exploration_runs (
                    run_id, session_id, question_snapshot, config_json, status,
                    attempt, previous_run_id, budget_json, final_draft,
                    safe_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.session_id,
                    run.question_snapshot,
                    json.dumps(run.config_snapshot, ensure_ascii=False),
                    run.status.value,
                    run.attempt,
                    run.previous_run_id,
                    json.dumps(run.budgets, ensure_ascii=False),
                    run.final_draft,
                    run.safe_error,
                    run.created_at.isoformat() if run.created_at else datetime.now(UTC).isoformat(),
                ),
            )
            self._insert_attempt_row(self.connection, attempt)

    @staticmethod
    def _insert_attempt_row(connection: sqlite3.Connection, attempt: Attempt) -> None:
        connection.execute(
            """
            INSERT INTO exploration_attempts (
                attempt_id, run_id, attempt_no, status, generation,
                input_json, budget_json, budget_used_json, created_at,
                started_at, finished_at, safe_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.attempt_id,
                attempt.run_id,
                attempt.attempt_no,
                attempt.status.value,
                attempt.generation,
                json.dumps(dict(attempt.input_snapshot), ensure_ascii=False),
                json.dumps(dict(attempt.budgets), ensure_ascii=False),
                json.dumps(dict(attempt.budget_used), ensure_ascii=False),
                attempt.created_at.isoformat() if attempt.created_at else datetime.now(UTC).isoformat(),
                attempt.started_at.isoformat() if attempt.started_at else None,
                attempt.finished_at.isoformat() if attempt.finished_at else None,
                attempt.safe_error,
            ),
        )

    @staticmethod
    def _attempt_status_from_storage(value: str) -> AttemptStatus:
        # Early coordinated-attempt builds stored the run-level value
        # ``failed`` before failure was split into retryable/terminal attempt
        # outcomes. Keep those rows immutable and project them conservatively
        # as retryable so existing sessions remain readable and continuable.
        return (
            AttemptStatus.RETRYABLE_FAILURE
            if value == "failed"
            else AttemptStatus(value)
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> Attempt:
        return Attempt(
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            attempt_no=row["attempt_no"],
            status=SQLiteWorkbenchRepository._attempt_status_from_storage(row["status"]),
            generation=row["generation"],
            input_snapshot=json.loads(row["input_json"] or "{}"),
            budgets=json.loads(row["budget_json"] or "{}"),
            budget_used=json.loads(row["budget_used_json"] or "{}"),
            safe_error=row["safe_error"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        )

    def list_coordinated_attempts(self, run_id: str) -> tuple[Attempt, ...]:
        rows = self.connection.execute(
            "SELECT * FROM exploration_attempts WHERE run_id = ? ORDER BY attempt_no",
            (run_id,),
        ).fetchall()
        run = self.get_exploration_run(run_id)
        if run is None:
            return ()
        attempts = tuple(self._attempt_from_row(row) for row in rows)
        if attempts and attempts[0].attempt_no == 1:
            return attempts
        return (self._legacy_attempt(run), *attempts)

    @staticmethod
    def _legacy_attempt_id(run_id: str) -> str:
        return f"legacy-run:{run_id}"

    @staticmethod
    def _attempt_status_for_legacy_run(status: ExplorationStatus) -> AttemptStatus:
        return {
            ExplorationStatus.QUEUED: AttemptStatus.QUEUED,
            ExplorationStatus.RUNNING: AttemptStatus.RUNNING,
            ExplorationStatus.COMPLETED: AttemptStatus.COMPLETED,
            ExplorationStatus.FAILED: AttemptStatus.TERMINAL_FAILURE,
            ExplorationStatus.CANCELLED: AttemptStatus.CANCELLED,
            ExplorationStatus.BUDGET_EXHAUSTED: AttemptStatus.BUDGET_EXHAUSTED,
            ExplorationStatus.AWAITING_USER_DECISION: AttemptStatus.AWAITING_USER,
        }[status]

    def _legacy_attempt(self, run: ExplorationRun) -> Attempt:
        """Project one old Run as one opaque Attempt without persisting guesses."""
        return Attempt(
            attempt_id=self._legacy_attempt_id(run.run_id),
            run_id=run.run_id,
            attempt_no=1,
            status=self._attempt_status_for_legacy_run(run.status),
            input_snapshot={},
            budgets=run.budgets,
            budget_used={},
            safe_error=run.safe_error,
            created_at=run.created_at,
            legacy=True,
        )

    def get_coordinated_attempt(self, attempt_id: str) -> Attempt | None:
        row = self.connection.execute(
            "SELECT * FROM exploration_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is not None:
            return self._attempt_from_row(row)
        prefix = "legacy-run:"
        if not attempt_id.startswith(prefix):
            return None
        run_id = attempt_id[len(prefix):]
        run = self.get_exploration_run(run_id)
        if run is None:
            return None
        first_real_attempt = self.connection.execute(
            "SELECT attempt_no FROM exploration_attempts WHERE run_id = ? "
            "ORDER BY attempt_no LIMIT 1", (run_id,)
        ).fetchone()
        if first_real_attempt is not None and first_real_attempt["attempt_no"] == 1:
            return None
        return self._legacy_attempt(run)

    def _list_legacy_attempt_events(
        self, run_id: str, *, after_sequence: int = 0
    ) -> tuple[PersistedEvent, ...]:
        rows = self.connection.execute(
            "SELECT event_id, sequence_no, event_type, payload_json, created_at "
            "FROM exploration_events WHERE run_id = ? AND sequence_no > ? "
            "ORDER BY sequence_no",
            (run_id, after_sequence),
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            result.append(PersistedEvent(
                event_id=row["event_id"],
                attempt_id=self._legacy_attempt_id(run_id),
                sequence_no=row["sequence_no"],
                generation=0,
                event_type=row["event_type"],
                summary=payload["summary"],
                payload={
                    "stable_ids": payload.get("stable_ids", {}),
                    "counters": payload.get("counters", {}),
                    "extras": payload.get("extras", []),
                },
                occurred_at=datetime.fromisoformat(row["created_at"]),
            ))
        return tuple(result)

    def append_attempt_event(
        self,
        attempt_id: str,
        generation: int,
        event: UnsequencedEvent,
    ) -> PersistedEvent:
        """Validate the live generation and allocate one durable sequence."""
        payload = json.dumps(
            {"summary": event.summary, "payload": dict(event.payload)},
            ensure_ascii=False,
        )
        with self.connection:
            attempt = self.connection.execute(
                "SELECT generation, status FROM exploration_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            if attempt["generation"] != generation:
                raise ValueError("attempt generation changed")
            if attempt["status"] not in {"queued", "running"}:
                raise ValueError("attempt generation changed or is terminal")
            current = self.connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) FROM exploration_attempt_events "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            sequence_no = current + 1
            cursor = self.connection.execute(
                """
                INSERT INTO exploration_attempt_events (
                    attempt_id, sequence_no, generation, event_type,
                    payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    sequence_no,
                    generation,
                    event.event_type,
                    payload,
                    event.occurred_at.isoformat(),
                ),
            )
        return PersistedEvent(
            event_id=cursor.lastrowid,
            attempt_id=attempt_id,
            sequence_no=sequence_no,
            generation=generation,
            event_type=event.event_type,
            summary=event.summary,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )

    def list_attempt_events(
        self, attempt_id: str, *, after_sequence: int = 0
    ) -> tuple[PersistedEvent, ...]:
        legacy = self.get_coordinated_attempt(attempt_id)
        if legacy is not None and legacy.legacy:
            return self._list_legacy_attempt_events(
                legacy.run_id, after_sequence=after_sequence
            )
        rows = self.connection.execute(
            "SELECT event_id, attempt_id, sequence_no, generation, event_type, "
            "payload_json, occurred_at FROM exploration_attempt_events "
            "WHERE attempt_id = ? AND sequence_no > ? ORDER BY sequence_no",
            (attempt_id, after_sequence),
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            result.append(PersistedEvent(
                event_id=row["event_id"],
                attempt_id=row["attempt_id"],
                sequence_no=row["sequence_no"],
                generation=row["generation"],
                event_type=row["event_type"],
                summary=payload["summary"],
                payload=payload.get("payload", {}),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
            ))
        return tuple(result)

    def list_run_attempt_events(self, run_id: str) -> tuple[PersistedEvent, ...]:
        rows = self.connection.execute(
            """
            SELECT e.event_id, e.attempt_id, e.sequence_no, e.generation,
                   e.event_type, e.payload_json, e.occurred_at
            FROM exploration_attempt_events e
            JOIN exploration_attempts a ON a.attempt_id = e.attempt_id
            WHERE a.run_id = ?
            ORDER BY a.attempt_no, e.sequence_no
            """,
            (run_id,),
        ).fetchall()
        attempts = self.list_coordinated_attempts(run_id)
        legacy_events = (
            self._list_legacy_attempt_events(run_id)
            if attempts and attempts[0].legacy else ()
        )
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            result.append(PersistedEvent(
                event_id=row["event_id"],
                attempt_id=row["attempt_id"],
                sequence_no=row["sequence_no"],
                generation=row["generation"],
                event_type=row["event_type"],
                summary=payload["summary"],
                payload=payload.get("payload", {}),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
            ))
        return (*legacy_events, *result)

    def claim_next_attempt(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> AttemptLease | None:
        if not worker_id.strip() or lease_duration.total_seconds() <= 0:
            raise ValueError("worker id and positive lease duration are required")
        expires_at = now + lease_duration
        with self.connection:
            row = self.connection.execute(
                """
                SELECT attempt_id, generation
                FROM exploration_attempts
                WHERE status = 'queued'
                   OR (status = 'running' AND lease_expires_at < ?)
                ORDER BY created_at, attempt_no
                LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            generation = int(row["generation"]) + 1
            cursor = self.connection.execute(
                """
                UPDATE exploration_attempts
                SET status = 'running', generation = ?, lease_owner = ?,
                    lease_expires_at = ?, started_at = COALESCE(started_at, ?)
                WHERE attempt_id = ? AND generation = ?
                  AND (status = 'queued' OR (status = 'running' AND lease_expires_at < ?))
                """,
                (
                    generation,
                    worker_id,
                    expires_at.isoformat(),
                    now.isoformat(),
                    row["attempt_id"],
                    row["generation"],
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
            self.connection.execute(
                "UPDATE exploration_runs SET status = 'running' "
                "WHERE run_id = (SELECT run_id FROM exploration_attempts WHERE attempt_id = ?)",
                (row["attempt_id"],),
            )
        return AttemptLease(row["attempt_id"], worker_id, generation, expires_at)

    def renew_attempt_lease(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        expected_generation: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> AttemptLease:
        if lease_duration.total_seconds() <= 0:
            raise ValueError("positive lease duration is required")
        expires_at = now + lease_duration
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE exploration_attempts
                SET lease_expires_at = ?
                WHERE attempt_id = ? AND status = 'running'
                  AND lease_owner = ? AND generation = ? AND lease_expires_at >= ?
                """,
                (
                    expires_at.isoformat(),
                    attempt_id,
                    worker_id,
                    expected_generation,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("attempt lease changed or expired")
        return AttemptLease(attempt_id, worker_id, expected_generation, expires_at)

    def abandon_expired_attempts(self, *, now: datetime) -> tuple[str, ...]:
        with self.connection:
            rows = self.connection.execute(
                "SELECT attempt_id, run_id FROM exploration_attempts "
                "WHERE status = 'running' AND lease_expires_at < ? "
                "ORDER BY created_at, attempt_no",
                (now.isoformat(),),
            ).fetchall()
            if not rows:
                return ()
            attempt_ids = tuple(row["attempt_id"] for row in rows)
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE exploration_attempts
                    SET status = 'abandoned', finished_at = ?, lease_owner = NULL,
                        lease_expires_at = NULL, safe_error = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    (now.isoformat(), "worker 租约到期，执行状态无法确认", row["attempt_id"]),
                )
                self.connection.execute(
                    "UPDATE exploration_runs SET status = 'failed', safe_error = ? WHERE run_id = ?",
                    ("worker 租约到期，执行状态无法确认", row["run_id"]),
                )
        return attempt_ids

    def abandon_running_attempts(
        self,
        *,
        now: datetime,
        safe_error: str = "进程重启，执行状态无法确认",
    ) -> tuple[str, ...]:
        """Reconcile Attempts left running by a previous application process.

        A lease is useful for fencing concurrent workers, but a local process
        restart can happen before that lease expires.  Startup must not expose
        that interval as a false ``running`` state: the old worker cannot be
        proven alive, so the Attempt is moved to the explicit, continuable
        ``abandoned`` terminal state and the Run is projected as failed.
        """
        if not safe_error.strip():
            raise ValueError("safe error must not be blank")
        with self.connection:
            rows = self.connection.execute(
                "SELECT attempt_id, run_id FROM exploration_attempts "
                "WHERE status = 'running' ORDER BY created_at, attempt_no"
            ).fetchall()
            if not rows:
                return ()
            attempt_ids = tuple(row["attempt_id"] for row in rows)
            for row in rows:
                self.connection.execute(
                    """
                    UPDATE exploration_attempts
                    SET status = 'abandoned', finished_at = ?, lease_owner = NULL,
                        lease_expires_at = NULL, safe_error = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    (now.isoformat(), safe_error, row["attempt_id"]),
                )
                self.connection.execute(
                    "UPDATE exploration_runs SET status = 'failed', safe_error = ? "
                    "WHERE run_id = ? AND status = 'running'",
                    (safe_error, row["run_id"]),
                )
        return attempt_ids

    def save_tool_outcome(self, outcome: ToolOutcome) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO exploration_tool_outcomes (
                    attempt_id, tool_call_id, tool_name, status, error_code,
                    retryability, side_effect_state, operation_id,
                    retry_after_seconds, safe_message, diagnostic_id,
                    bounded_result_reference, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.attempt_id,
                    outcome.tool_call_id,
                    outcome.tool_name,
                    outcome.status.value,
                    outcome.error_code.value if outcome.error_code else None,
                    outcome.retryability.value,
                    outcome.side_effect_state.value,
                    outcome.operation_id,
                    outcome.retry_after_seconds,
                    outcome.safe_message,
                    outcome.diagnostic_id,
                    outcome.bounded_result_reference,
                    outcome.started_at.isoformat(),
                    outcome.finished_at.isoformat(),
                ),
            )

    def get_tool_outcome(self, attempt_id: str, tool_call_id: str) -> ToolOutcome | None:
        row = self.connection.execute(
            "SELECT * FROM exploration_tool_outcomes WHERE attempt_id = ? AND tool_call_id = ?",
            (attempt_id, tool_call_id),
        ).fetchone()
        if row is None:
            return None
        return ToolOutcome(
            tool_call_id=row["tool_call_id"],
            attempt_id=row["attempt_id"],
            tool_name=row["tool_name"],
            status=ToolOutcomeStatus(row["status"]),
            error_code=ToolErrorCode(row["error_code"]) if row["error_code"] else None,
            retryability=Retryability(row["retryability"]),
            side_effect_state=SideEffectState(row["side_effect_state"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            safe_message=row["safe_message"],
            diagnostic_id=row["diagnostic_id"],
            operation_id=row["operation_id"],
            retry_after_seconds=row["retry_after_seconds"],
            bounded_result_reference=row["bounded_result_reference"],
        )

    def list_tool_outcomes(self, attempt_id: str) -> tuple[ToolOutcome, ...]:
        rows = self.connection.execute(
            "SELECT tool_call_id FROM exploration_tool_outcomes "
            "WHERE attempt_id = ? ORDER BY started_at, tool_call_id",
            (attempt_id,),
        ).fetchall()
        return tuple(
            outcome for row in rows
            if (outcome := self.get_tool_outcome(attempt_id, row["tool_call_id"])) is not None
        )

    def list_coordinated_operations(self, attempt_id: str) -> tuple[dict[str, object], ...]:
        rows = self.connection.execute(
            "SELECT operation_id, status, result_json FROM exploration_operations "
            "WHERE attempt_id = ? ORDER BY created_at, operation_id",
            (attempt_id,),
        ).fetchall()
        return tuple({
            "operation_id": row["operation_id"],
            "status": row["status"],
            "receipt": json.loads(row["result_json"] or "{}"),
        } for row in rows)

    def begin_coordinated_operation(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        expected_generation: int,
        tool_call_id: str,
        resource_key: str,
        operation_kind: str,
    ) -> dict[str, object] | None:
        """Reserve a side effect only for the currently leased generation."""
        if not all(value.strip() for value in (
            operation_id, attempt_id, tool_call_id, resource_key, operation_kind
        )):
            raise ValueError("operation identity fields must not be blank")
        now = datetime.now(UTC).isoformat()
        with self.connection:
            attempt = self.connection.execute(
                "SELECT generation, status FROM exploration_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if (
                attempt is None
                or attempt["generation"] != expected_generation
                or attempt["status"] != "running"
            ):
                raise ValueError("attempt generation changed or is not running")
            existing = self.connection.execute(
                """SELECT attempt_id, generation, tool_call_id, resource_key,
                          operation_kind, status, result_json
                   FROM exploration_operations WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                expected_identity = (
                    attempt_id, expected_generation, tool_call_id,
                    resource_key, operation_kind,
                )
                actual_identity = (
                    existing["attempt_id"], existing["generation"],
                    existing["tool_call_id"], existing["resource_key"],
                    existing["operation_kind"],
                )
                if actual_identity != expected_identity:
                    raise ValueError("operation identity does not match replay request")
                if existing["status"] == "committed":
                    return json.loads(existing["result_json"])
                if existing["status"] == "started":
                    raise OperationEffectUnknownError(operation_id)
                return None
            self.connection.execute(
                """
                INSERT INTO exploration_operations (
                    operation_id, attempt_id, operation_kind, generation,
                    tool_call_id, resource_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?)
                """,
                (
                    operation_id,
                    attempt_id,
                    operation_kind,
                    expected_generation,
                    tool_call_id,
                    resource_key,
                    now,
                    now,
                ),
            )
        return None

    def settle_coordinated_operation(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        expected_generation: int,
        status: str,
        receipt: dict[str, object] | None = None,
    ) -> None:
        """Settle a reserved side effect while its attempt lease is still valid."""
        if status not in {"committed", "failed", "effect_unknown"}:
            raise ValueError("operation settlement status is invalid")
        if status == "committed" and receipt is None:
            raise ValueError("committed operation requires a receipt")
        now = datetime.now(UTC).isoformat()
        result_json = json.dumps(receipt or {}, ensure_ascii=False)
        with self.connection:
            attempt = self.connection.execute(
                "SELECT generation, status FROM exploration_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if (
                attempt is None
                or attempt["generation"] != expected_generation
                or attempt["status"] != "running"
            ):
                raise ValueError("attempt generation changed or is not running")
            cursor = self.connection.execute(
                """
                UPDATE exploration_operations
                SET status = ?, result_json = ?, updated_at = ?
                WHERE operation_id = ? AND attempt_id = ? AND generation = ?
                  AND status = 'started'
                """,
                (
                    status, result_json, now, operation_id, attempt_id,
                    expected_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("operation is missing, already settled, or identity changed")

    def get_idempotent_attempt(
        self, run_id: str, action: str, idempotency_key: str
    ) -> str | None:
        row = self.connection.execute(
            "SELECT attempt_id FROM exploration_idempotency "
            "WHERE run_id = ? AND action = ? AND idempotency_key = ?",
            (run_id, action, idempotency_key),
        ).fetchone()
        return row[0] if row else None

    def continue_coordinated_run(
        self,
        run_id: str,
        *,
        expected_attempt_id: str,
        idempotency_key: str,
        attempt: Attempt,
    ) -> str:
        """Create one next attempt or return the attempt from an identical request."""
        with self.connection:
            existing = self.connection.execute(
                "SELECT attempt_id FROM exploration_idempotency "
                "WHERE run_id = ? AND action = 'continue' AND idempotency_key = ?",
                (run_id, idempotency_key),
            ).fetchone()
            if existing:
                return existing[0]
            current = self.connection.execute(
                "SELECT attempt_id, status FROM exploration_attempts "
                "WHERE run_id = ? ORDER BY attempt_no DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if current is None:
                run = self.get_exploration_run(run_id)
                if run is None:
                    raise KeyError(run_id)
                if expected_attempt_id != self._legacy_attempt_id(run_id):
                    raise ValueError("current attempt changed")
                if self._attempt_status_for_legacy_run(run.status) not in {
                    AttemptStatus.BUDGET_EXHAUSTED,
                    AttemptStatus.CANCELLED,
                    AttemptStatus.RETRYABLE_FAILURE,
                    AttemptStatus.ABANDONED,
                }:
                    raise ValueError("current attempt is not continuable")
            elif current["attempt_id"] != expected_attempt_id:
                raise ValueError("current attempt changed")
            elif self._attempt_status_from_storage(current["status"]) not in {
                AttemptStatus.BUDGET_EXHAUSTED,
                AttemptStatus.CANCELLED,
                AttemptStatus.RETRYABLE_FAILURE,
                AttemptStatus.ABANDONED,
            }:
                raise ValueError("current attempt is not continuable")
            self._insert_attempt_row(self.connection, attempt)
            self.connection.execute(
                "UPDATE exploration_runs SET status = 'queued', safe_error = NULL WHERE run_id = ?",
                (run_id,),
            )
            self.connection.execute(
                "INSERT INTO exploration_idempotency "
                "(run_id, action, idempotency_key, attempt_id, created_at) "
                "VALUES (?, 'continue', ?, ?, ?)",
                (run_id, idempotency_key, attempt.attempt_id, datetime.now(UTC).isoformat()),
            )
        return attempt.attempt_id

    def complete_coordinated_attempt(
        self,
        attempt_id: str,
        status: AttemptStatus,
        *,
        expected_generation: int,
        safe_error: str | None,
        budget_used: Mapping[str, int],
        final_draft: str | None = None,
        finished_at: datetime,
    ) -> None:
        if status not in {
            AttemptStatus.COMPLETED,
            AttemptStatus.AWAITING_USER,
            AttemptStatus.BUDGET_EXHAUSTED,
            AttemptStatus.CANCELLED,
            AttemptStatus.RETRYABLE_FAILURE,
            AttemptStatus.TERMINAL_FAILURE,
            AttemptStatus.ABANDONED,
        }:
            raise ValueError("attempt outcome must be terminal")
        run_status = {
            AttemptStatus.COMPLETED: "completed",
            AttemptStatus.AWAITING_USER: "awaiting_user_decision",
            AttemptStatus.BUDGET_EXHAUSTED: "budget_exhausted",
            AttemptStatus.CANCELLED: "cancelled",
            AttemptStatus.RETRYABLE_FAILURE: "failed",
            AttemptStatus.TERMINAL_FAILURE: "failed",
            AttemptStatus.ABANDONED: "failed",
        }[status]
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE exploration_attempts SET status = ?, safe_error = ?, budget_used_json = ?, finished_at = ? "
                "WHERE attempt_id = ? AND generation = ? AND status IN ('queued', 'running')",
                (
                    status.value,
                    safe_error,
                    json.dumps(dict(budget_used), ensure_ascii=False),
                    finished_at.isoformat(),
                    attempt_id,
                    expected_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("attempt generation changed, terminal, or missing")
            persisted_draft = final_draft if final_draft and final_draft.strip() else None
            self.connection.execute(
                "UPDATE exploration_runs SET status = ?, safe_error = ?, "
                "final_draft = COALESCE(?, final_draft) "
                "WHERE run_id = (SELECT run_id FROM exploration_attempts WHERE attempt_id = ?)",
                (run_status, safe_error, persisted_draft, attempt_id),
            )

    def cancel_coordinated_attempt(
        self,
        run_id: str,
        *,
        expected_attempt_id: str,
        idempotency_key: str,
        finished_at: datetime,
    ) -> None:
        with self.connection:
            existing = self.connection.execute(
                "SELECT attempt_id FROM exploration_idempotency "
                "WHERE run_id = ? AND action = 'cancel' AND idempotency_key = ?",
                (run_id, idempotency_key),
            ).fetchone()
            if existing:
                return
            current = self.connection.execute(
                "SELECT attempt_id, status FROM exploration_attempts "
                "WHERE run_id = ? ORDER BY attempt_no DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if current is None:
                raise KeyError(run_id)
            if current["attempt_id"] != expected_attempt_id:
                raise ValueError("current attempt changed")
            if current["status"] not in {"queued", "running"}:
                raise ValueError("current attempt is terminal")
            self.connection.execute(
                "UPDATE exploration_attempts SET status = 'cancelled', finished_at = ? "
                "WHERE attempt_id = ?",
                (finished_at.isoformat(), expected_attempt_id),
            )
            self.connection.execute(
                "UPDATE exploration_runs SET status = 'cancelled' WHERE run_id = ?",
                (run_id,),
            )
            self.connection.execute(
                "INSERT INTO exploration_idempotency "
                "(run_id, action, idempotency_key, attempt_id, created_at) "
                "VALUES (?, 'cancel', ?, ?, ?)",
                (run_id, idempotency_key, expected_attempt_id, datetime.now(UTC).isoformat()),
            )

    def list_exploration_events(self, run_id: str) -> tuple[SafeAgentEvent, ...]:
        """Project the authoritative Attempt event stream for legacy API readers."""
        return tuple(
            SafeAgentEvent(
                sequence_no=event.sequence_no,
                event_type=event.event_type,
                summary=event.summary,
                stable_ids=event.payload.get("stable_ids", {}),
                counters=event.payload.get("counters", {}),
                extras=tuple(event.payload.get("extras", ())),
                attempt_id=event.attempt_id,
            )
            for event in self.list_run_attempt_events(run_id)
        )

    def insert_candidate_finding(self, finding: CandidateFinding) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO candidate_findings (
                    finding_id, run_id, claim, source_ids_json, read_block_ids_json,
                    source_authority_json, citation_statuses_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding.finding_id,
                    finding.run_id,
                    finding.claim,
                    json.dumps(finding.source_ids),
                    json.dumps(finding.read_block_ids),
                    json.dumps(finding.source_authority),
                    json.dumps(finding.citation_statuses),
                ),
            )

    def list_candidate_findings(self, run_id: str) -> tuple[CandidateFinding, ...]:
        rows = self.connection.execute(
            """
            SELECT finding_id, run_id, claim, source_ids_json, read_block_ids_json,
                   source_authority_json, citation_statuses_json
            FROM candidate_findings WHERE run_id = ? ORDER BY rowid
            """,
            (run_id,),
        ).fetchall()
        return tuple(CandidateFinding(
            finding_id=row["finding_id"],
            run_id=row["run_id"],
            claim=row["claim"],
            source_ids=tuple(json.loads(row["source_ids_json"])),
            read_block_ids=tuple(json.loads(row["read_block_ids_json"])),
            source_authority=json.loads(row["source_authority_json"]),
            citation_statuses=json.loads(row["citation_statuses_json"]),
        ) for row in rows)

    def insert_note_run(self, run: "NoteRun") -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO note_runs (
                    note_run_id, paper_id, triggering_session_id, status, attempt,
                    previous_note_run_id, receipt_path, draft_path, knowledge_id, safe_error, stage, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.note_run_id, run.paper_id, run.triggering_session_id,
                    run.status.value, run.attempt, run.previous_note_run_id,
                    run.receipt_path, run.draft_path, run.knowledge_id, run.safe_error,
                    run.stage,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def save_note_run(self, run: "NoteRun") -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE note_runs SET status = ?, receipt_path = ?, draft_path = ?, knowledge_id = ?, safe_error = ?, stage = ?
                WHERE note_run_id = ?
                """,
                (run.status.value, run.receipt_path, run.draft_path, run.knowledge_id, run.safe_error, run.stage, run.note_run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run.note_run_id)

    def get_note_run(self, note_run_id: str) -> "NoteRun | None":
        from research_pulse.workbench.models import NoteRun, NoteRunStatus
        row = self.connection.execute(
            """SELECT note_run_id, paper_id, triggering_session_id, status, attempt,
                      previous_note_run_id, receipt_path, draft_path, knowledge_id, safe_error, stage, created_at
               FROM note_runs WHERE note_run_id = ?""",
            (note_run_id,),
        ).fetchone()
        if row is None:
            return None
        return NoteRun(
            note_run_id=row["note_run_id"], paper_id=row["paper_id"],
            triggering_session_id=row["triggering_session_id"],
            status=NoteRunStatus(row["status"]), attempt=row["attempt"],
            previous_note_run_id=row["previous_note_run_id"], receipt_path=row["receipt_path"],
            draft_path=row["draft_path"],
            knowledge_id=row["knowledge_id"], safe_error=row["safe_error"],
            stage=row["stage"],
            created_at=row["created_at"],
        )

    def list_note_runs(self, session_id: str) -> tuple["NoteRun", ...]:
        rows = self.connection.execute(
            "SELECT note_run_id FROM note_runs WHERE triggering_session_id = ? ORDER BY created_at, attempt",
            (session_id,),
        ).fetchall()
        return tuple(self.get_note_run(row["note_run_id"]) for row in rows)  # type: ignore[misc]

    def append_note_run_event(self, note_run_id: str, event: "SafeAgentEvent") -> None:
        payload = json.dumps(
            {
                "summary": event.summary,
                "stable_ids": event.stable_ids,
                "counters": event.counters,
            },
            ensure_ascii=False,
        )
        with self.connection:
            exists = self.connection.execute(
                "SELECT 1 FROM note_runs WHERE note_run_id = ?", (note_run_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(note_run_id)
            current = self.connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) FROM note_run_events WHERE note_run_id = ?",
                (note_run_id,),
            ).fetchone()[0]
            if event.sequence_no != current + 1:
                raise ValueError("note run event sequence must be contiguous and append-only")
            self.connection.execute(
                """
                INSERT INTO note_run_events (
                    note_run_id, sequence_no, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    note_run_id,
                    event.sequence_no,
                    event.event_type,
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_note_run_events(self, note_run_id: str) -> tuple["SafeAgentEvent", ...]:
        rows = self.connection.execute(
            """
            SELECT sequence_no, event_type, payload_json
            FROM note_run_events WHERE note_run_id = ? ORDER BY sequence_no
            """,
            (note_run_id,),
        ).fetchall()
        events = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            events.append(SafeAgentEvent(
                sequence_no=row["sequence_no"],
                event_type=row["event_type"],
                summary=payload["summary"],
                stable_ids=payload.get("stable_ids", {}),
                counters=payload.get("counters", {}),
            ))
        return tuple(events)
