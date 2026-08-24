"""PostgreSQL control-data repository for research topics and run receipts."""

from __future__ import annotations

from typing import Any, Sequence

from research_pulse.topics.contracts import ActiveRunConflict, ScheduledRunExists, TopicRepository
from research_pulse.topics.models import ProductionRun, ResearchTopic, RunStatus, RunTrigger, SAFE_ERROR_SUMMARIES


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_topics (
    topic_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    daily_limit INTEGER NOT NULL DEFAULT 3 CHECK (daily_limit BETWEEN 1 AND 3),
    last_successful_discovery_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS production_runs (
    run_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES research_topics(topic_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'partial_failed', 'failed')),
    paper_limit INTEGER NOT NULL CHECK (paper_limit BETWEEN 1 AND 3),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    published_count INTEGER NOT NULL DEFAULT 0 CHECK (published_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    error_code TEXT,
    error_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    run_trigger TEXT NOT NULL DEFAULT 'manual' CHECK (run_trigger IN ('initial', 'manual', 'scheduled')),
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    scheduled_for TIMESTAMPTZ,
    CHECK ((run_trigger = 'scheduled' AND scheduled_for IS NOT NULL) OR (run_trigger <> 'scheduled' AND scheduled_for IS NULL)),
    CHECK (published_count + failed_count <= candidate_count)
);

ALTER TABLE research_topics ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE research_topics ADD COLUMN IF NOT EXISTS daily_limit INTEGER NOT NULL DEFAULT 3;
ALTER TABLE research_topics ADD COLUMN IF NOT EXISTS last_successful_discovery_at TIMESTAMPTZ;
ALTER TABLE production_runs ADD COLUMN IF NOT EXISTS run_trigger TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE production_runs ADD COLUMN IF NOT EXISTS window_start TIMESTAMPTZ;
ALTER TABLE production_runs ADD COLUMN IF NOT EXISTS window_end TIMESTAMPTZ;
ALTER TABLE production_runs ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS production_runs_one_active_topic_idx
    ON production_runs (topic_id)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS production_runs_topic_created_idx
    ON production_runs (topic_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS production_runs_one_schedule_topic_idx
    ON production_runs (topic_id, scheduled_for)
    WHERE run_trigger = 'scheduled';
"""


class PostgresTopicRepository(TopicRepository):
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def initialize(self) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA_SQL)

    def create_topic_with_run(self, topic: ResearchTopic, run: ProductionRun) -> None:
        if topic.topic_id != run.topic_id:
            raise ValueError("topic and run must share topic_id")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO research_topics (
                        topic_id, name, query, domain, created_at, enabled,
                        daily_limit, last_successful_discovery_at
                    ) VALUES (
                        %(topic_id)s, %(name)s, %(query)s, %(domain)s, %(created_at)s,
                        %(enabled)s, %(daily_limit)s, %(last_successful_discovery_at)s
                    )
                    """,
                    _topic_values(topic),
                )
                cursor.execute(_INSERT_RUN_SQL, _run_values(run))

    def list_topics_with_latest_run(self) -> Sequence[tuple[ResearchTopic, ProductionRun | None]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        t.topic_id, t.name, t.query, t.domain, t.created_at AS topic_created_at,
                        t.enabled, t.daily_limit, t.last_successful_discovery_at,
                        r.run_id, r.status, r.paper_limit, r.candidate_count,
                        r.published_count, r.failed_count, r.error_code, r.error_summary,
                        r.created_at AS run_created_at, r.started_at, r.finished_at,
                        r.run_trigger, r.window_start, r.window_end, r.scheduled_for
                    FROM research_topics t
                    LEFT JOIN LATERAL (
                        SELECT * FROM production_runs candidate
                        WHERE candidate.topic_id = t.topic_id
                        ORDER BY candidate.created_at DESC, candidate.run_id DESC
                        LIMIT 1
                    ) r ON TRUE
                    ORDER BY t.created_at DESC, t.topic_id DESC
                    """
                )
                rows = cursor.fetchall()
        return [(_topic_from_row(row), _run_from_joined_row(row)) for row in rows]

    def list_enabled_topics(self) -> Sequence[ResearchTopic]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT topic_id, name, query, domain, created_at, enabled,
                           daily_limit, last_successful_discovery_at
                    FROM research_topics WHERE enabled = TRUE
                    ORDER BY created_at, topic_id
                    """
                )
                rows = cursor.fetchall()
        return [_topic_from_plain_row(row) for row in rows]

    def get_topic(self, topic_id: str) -> ResearchTopic | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT topic_id, name, query, domain, created_at, enabled,
                           daily_limit, last_successful_discovery_at
                    FROM research_topics WHERE topic_id = %s
                    """,
                    (topic_id,),
                )
                row = cursor.fetchone()
        return _topic_from_plain_row(row) if row else None

    def get_run(self, run_id: str) -> ProductionRun | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(_SELECT_RUN_SQL + " WHERE run_id = %s", (run_id,))
                row = cursor.fetchone()
        return _run_from_plain_row(row) if row else None

    def create_run(self, run: ProductionRun) -> None:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_INSERT_RUN_SQL, _run_values(run))
        except self._unique_violation() as error:
            constraint = getattr(error.diag, "constraint_name", None)
            if constraint == "production_runs_one_active_topic_idx":
                active = self._get_active_run(run.topic_id)
                if active is None:
                    raise
                raise ActiveRunConflict(active) from error
            if constraint == "production_runs_one_schedule_topic_idx" and run.scheduled_for is not None:
                existing = self._get_scheduled_run(run.topic_id, run.scheduled_for)
                if existing is None:
                    raise
                raise ScheduledRunExists(existing) from error
            raise

    def update_topic(self, topic: ResearchTopic) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE research_topics SET
                        enabled = %(enabled)s,
                        daily_limit = %(daily_limit)s,
                        last_successful_discovery_at = %(last_successful_discovery_at)s
                    WHERE topic_id = %(topic_id)s
                    """,
                    _topic_values(topic),
                )
                if cursor.rowcount != 1:
                    raise LookupError("research topic not found")

    def save_run(self, run: ProductionRun) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE production_runs SET
                        status = %(status)s,
                        candidate_count = %(candidate_count)s,
                        published_count = %(published_count)s,
                        failed_count = %(failed_count)s,
                        error_code = %(error_code)s,
                        error_summary = %(error_summary)s,
                        started_at = %(started_at)s,
                        finished_at = %(finished_at)s
                    WHERE run_id = %(run_id)s
                    """,
                    _run_values(run),
                )
                if cursor.rowcount != 1:
                    raise LookupError("production run not found")

    def finish_run(
        self,
        run: ProductionRun,
        *,
        discovery_succeeded: bool,
        discovery_watermark,
    ) -> None:
        if not run.status.terminal:
            raise ValueError("finish_run requires a terminal run")
        if discovery_succeeded and discovery_watermark is None:
            raise ValueError("successful discovery requires a watermark")
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE production_runs SET
                        status = %(status)s,
                        candidate_count = %(candidate_count)s,
                        published_count = %(published_count)s,
                        failed_count = %(failed_count)s,
                        error_code = %(error_code)s,
                        error_summary = %(error_summary)s,
                        started_at = %(started_at)s,
                        finished_at = %(finished_at)s
                    WHERE run_id = %(run_id)s
                    """,
                    _run_values(run),
                )
                if cursor.rowcount != 1:
                    raise LookupError("production run not found")
                if discovery_succeeded:
                    cursor.execute(
                        """
                        UPDATE research_topics
                        SET last_successful_discovery_at = %s
                        WHERE topic_id = %s
                          AND (last_successful_discovery_at IS NULL OR last_successful_discovery_at <= %s)
                        """,
                        (discovery_watermark, run.topic_id, discovery_watermark),
                    )

    def reconcile_active_runs(self) -> int:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE production_runs SET
                        status = 'failed',
                        error_code = 'process_restarted',
                        error_summary = %s,
                        finished_at = now()
                    WHERE status IN ('queued', 'running')
                    """,
                    (SAFE_ERROR_SUMMARIES["process_restarted"],),
                )
                return cursor.rowcount

    def _get_active_run(self, topic_id: str) -> ProductionRun | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    _SELECT_RUN_SQL + " WHERE topic_id = %s AND status IN ('queued', 'running') LIMIT 1",
                    (topic_id,),
                )
                row = cursor.fetchone()
        return _run_from_plain_row(row) if row else None

    def _get_scheduled_run(self, topic_id: str, scheduled_for) -> ProductionRun | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    _SELECT_RUN_SQL + " WHERE topic_id = %s AND scheduled_for = %s AND run_trigger = 'scheduled' LIMIT 1",
                    (topic_id, scheduled_for),
                )
                row = cursor.fetchone()
        return _run_from_plain_row(row) if row else None

    def _connection(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError("psycopg is not installed; install requirements-rag.txt.") from error
        return psycopg.connect(self.database_url, row_factory=dict_row)

    @staticmethod
    def _unique_violation():
        from psycopg.errors import UniqueViolation

        return UniqueViolation


_INSERT_RUN_SQL = """
INSERT INTO production_runs (
    run_id, topic_id, status, paper_limit, candidate_count, published_count,
    failed_count, error_code, error_summary, created_at, started_at, finished_at,
    run_trigger, window_start, window_end, scheduled_for
) VALUES (
    %(run_id)s, %(topic_id)s, %(status)s, %(paper_limit)s, %(candidate_count)s,
    %(published_count)s, %(failed_count)s, %(error_code)s, %(error_summary)s,
    %(created_at)s, %(started_at)s, %(finished_at)s, %(run_trigger)s,
    %(window_start)s, %(window_end)s, %(scheduled_for)s
)
"""

_SELECT_RUN_SQL = """
SELECT run_id, topic_id, status, paper_limit, candidate_count, published_count,
       failed_count, error_code, error_summary, created_at, started_at, finished_at,
       run_trigger, window_start, window_end, scheduled_for
FROM production_runs
"""


def _topic_values(topic: ResearchTopic) -> dict[str, Any]:
    return {
        "topic_id": topic.topic_id,
        "name": topic.name,
        "query": topic.query,
        "domain": topic.domain,
        "created_at": topic.created_at,
        "enabled": topic.enabled,
        "daily_limit": topic.daily_limit,
        "last_successful_discovery_at": topic.last_successful_discovery_at,
    }


def _run_values(run: ProductionRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "topic_id": run.topic_id,
        "status": run.status.value,
        "paper_limit": run.limit,
        "candidate_count": run.candidate_count,
        "published_count": run.published_count,
        "failed_count": run.failed_count,
        "error_code": run.error_code,
        "error_summary": run.error_summary,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "run_trigger": run.trigger.value,
        "window_start": run.window_start,
        "window_end": run.window_end,
        "scheduled_for": run.scheduled_for,
    }


def _topic_from_row(row: dict[str, Any]) -> ResearchTopic:
    return ResearchTopic(
        topic_id=row["topic_id"],
        name=row["name"],
        query=row["query"],
        domain=row["domain"],
        created_at=row["topic_created_at"],
        enabled=row["enabled"],
        daily_limit=row["daily_limit"],
        last_successful_discovery_at=row["last_successful_discovery_at"],
    )


def _topic_from_plain_row(row: dict[str, Any]) -> ResearchTopic:
    return ResearchTopic(
        topic_id=row["topic_id"],
        name=row["name"],
        query=row["query"],
        domain=row["domain"],
        created_at=row["created_at"],
        enabled=row["enabled"],
        daily_limit=row["daily_limit"],
        last_successful_discovery_at=row["last_successful_discovery_at"],
    )


def _run_from_joined_row(row: dict[str, Any]) -> ProductionRun | None:
    if row["run_id"] is None:
        return None
    values = dict(row)
    values["created_at"] = values.pop("run_created_at")
    return _run_from_plain_row(values)


def _run_from_plain_row(row: dict[str, Any]) -> ProductionRun:
    return ProductionRun(
        run_id=row["run_id"],
        topic_id=row["topic_id"],
        status=RunStatus(row["status"]),
        limit=row["paper_limit"],
        candidate_count=row["candidate_count"],
        published_count=row["published_count"],
        failed_count=row["failed_count"],
        error_code=row["error_code"],
        error_summary=row["error_summary"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        trigger=RunTrigger(row["run_trigger"]),
        window_start=row["window_start"],
        window_end=row["window_end"],
        scheduled_for=row["scheduled_for"],
    )
