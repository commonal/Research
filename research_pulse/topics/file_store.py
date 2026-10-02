"""File-backed control-data repository for research topics and run receipts.

Replaces ``PostgresTopicRepository`` for the note-only deployment: topic and
production-run state live in a single JSON document under the vault root (e.g.
``knowledge/.topics/state.json``) with no database.  The writer mirrors the
Postgres constraints (one active run per topic, one scheduled run per slot,
terminal-only finish) so ``TopicRunService`` behaves identically.

Persistence is atomic: data is written to a temp file, ``fsync``-ed, then
``os.replace``-d over the target so a crash mid-write never leaves a torn file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
import json
import os

from research_pulse.topics.contracts import ActiveRunConflict, ScheduledRunExists, TopicRepository
from research_pulse.topics.models import ProductionRun, ResearchTopic, RunStatus, RunTrigger, SAFE_ERROR_SUMMARIES


class FileTopicStore(TopicRepository):
    """A ``TopicRepository`` backed by one atomic JSON document."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._state_path = root / "state.json"
        self._topics: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}

    # -- persistence ---------------------------------------------------------

    def initialize(self) -> None:
        """Load the document if present; a missing document is an empty store."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self._state_path.exists():
            self._topics = {}
            self._runs = {}
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LookupError(f"topic state file is unreadable: {self._state_path}") from error
        self._topics = {str(key): value for key, value in raw.get("topics", {}).items()}
        self._runs = {str(key): value for key, value in raw.get("runs", {}).items()}

    def _flush(self) -> None:
        """Atomically persist the in-memory state document."""
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "topics": self._topics,
                "runs": self._runs,
            },
            ensure_ascii=False,
            indent=2,
        )
        tmp_path = self._state_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._state_path)

    # -- topics --------------------------------------------------------------

    def create_topic_with_run(self, topic: ResearchTopic, run: ProductionRun) -> None:
        if topic.topic_id != run.topic_id:
            raise ValueError("topic and run must share topic_id")
        self._require_no_active_run(topic.topic_id, ignore_run_id=run.run_id)
        self._topics[topic.topic_id] = _topic_to_dict(topic)
        self._runs[run.run_id] = _run_to_dict(run)
        self._flush()

    def list_topics_with_latest_run(self) -> Sequence[tuple[ResearchTopic, ProductionRun | None]]:
        ordered = sorted(self._topics.values(), key=lambda item: (item["created_at"], item["topic_id"]), reverse=True)
        result = []
        for topic_row in ordered:
            topic = _topic_from_dict(topic_row)
            run_rows = [row for row in self._runs.values() if row["topic_id"] == topic.topic_id]
            latest = (
                _run_from_dict(sorted(run_rows, key=lambda row: (row["created_at"], row["run_id"]), reverse=True)[0])
                if run_rows
                else None
            )
            result.append((topic, latest))
        return result

    def list_enabled_topics(self) -> Sequence[ResearchTopic]:
        return [
            _topic_from_dict(row)
            for row in sorted(self._topics.values(), key=lambda item: (item["created_at"], item["topic_id"]))
            if row["enabled"] is True
        ]

    def get_topic(self, topic_id: str) -> ResearchTopic | None:
        row = self._topics.get(topic_id)
        return _topic_from_dict(row) if row else None

    def update_topic(self, topic: ResearchTopic) -> None:
        if topic.topic_id not in self._topics:
            raise LookupError("research topic not found")
        self._topics[topic.topic_id] = _topic_to_dict(topic)
        self._flush()

    # -- runs -----------------------------------------------------------------

    def get_run(self, run_id: str) -> ProductionRun | None:
        row = self._runs.get(run_id)
        return _run_from_dict(row) if row else None

    def create_run(self, run: ProductionRun) -> None:
        # Scheduled-slot uniqueness first (mirrors the Postgres index): a second
        # run for the same slot must surface ScheduledRunExists, not the generic
        # active-run conflict.
        if run.scheduled_for is not None:
            existing = next(
                (row for row in self._runs.values()
                 if row["topic_id"] == run.topic_id
                 and row.get("scheduled_for") is not None
                 and row.get("scheduled_for") == _iso(run.scheduled_for)),
                None,
            )
            if existing is not None:
                raise ScheduledRunExists(_run_from_dict(existing))
        self._require_no_active_run(run.topic_id, ignore_run_id=run.run_id)
        self._runs[run.run_id] = _run_to_dict(run)
        self._flush()

    def save_run(self, run: ProductionRun) -> None:
        if run.run_id not in self._runs:
            raise LookupError("production run not found")
        self._runs[run.run_id] = _run_to_dict(run)
        self._flush()

    def finish_run(
        self,
        run: ProductionRun,
        *,
        discovery_succeeded: bool,
        discovery_watermark: Any,
    ) -> None:
        if not run.status.terminal:
            raise ValueError("finish_run requires a terminal run")
        if discovery_succeeded and discovery_watermark is None:
            raise ValueError("successful discovery requires a watermark")
        if run.run_id not in self._runs:
            raise LookupError("production run not found")
        self._runs[run.run_id] = _run_to_dict(run)
        if discovery_succeeded and discovery_watermark is not None:
            topic_row = self._topics.get(run.topic_id)
            if topic_row is not None:
                previous = topic_row.get("last_successful_discovery_at")
                if previous is None or previous <= _iso(discovery_watermark):
                    topic_row["last_successful_discovery_at"] = _iso(discovery_watermark)
                    self._topics[run.topic_id] = topic_row
        self._flush()

    def reconcile_active_runs(self) -> int:
        count = 0
        for run_id, row in list(self._runs.items()):
            if row["status"] in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
                self._runs[run_id] = {
                    **row,
                    "status": RunStatus.FAILED.value,
                    "error_code": "process_restarted",
                    "error_summary": SAFE_ERROR_SUMMARIES["process_restarted"],
                    "finished_at": _iso(datetime.now(UTC)),
                }
                count += 1
        if count:
            self._flush()
        return count

    # -- helpers --------------------------------------------------------------

    def _require_no_active_run(self, topic_id: str, *, ignore_run_id: str | None = None) -> None:
        active = next(
            (
                row for row in self._runs.values()
                if row["topic_id"] == topic_id
                and row["run_id"] != ignore_run_id
                and row["status"] in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}
            ),
            None,
        )
        if active is not None:
            raise ActiveRunConflict(_run_from_dict(active))


# -- serialization -------------------------------------------------------------


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _topic_to_dict(topic: ResearchTopic) -> dict[str, Any]:
    return {
        "topic_id": topic.topic_id,
        "name": topic.name,
        "query": topic.query,
        "domain": topic.domain,
        "created_at": _iso(topic.created_at),
        "enabled": topic.enabled,
        "daily_limit": topic.daily_limit,
        "last_successful_discovery_at": _iso(topic.last_successful_discovery_at) if topic.last_successful_discovery_at else None,
    }


def _topic_from_dict(row: dict[str, Any]) -> ResearchTopic:
    return ResearchTopic(
        topic_id=row["topic_id"],
        name=row["name"],
        query=row["query"],
        domain=row["domain"],
        created_at=datetime.fromisoformat(row["created_at"]),
        enabled=row["enabled"],
        daily_limit=row["daily_limit"],
        last_successful_discovery_at=datetime.fromisoformat(row["last_successful_discovery_at"]) if row.get("last_successful_discovery_at") else None,
    )


def _run_to_dict(run: ProductionRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "topic_id": run.topic_id,
        "status": run.status.value,
        "limit": run.limit,
        "candidate_count": run.candidate_count,
        "published_count": run.published_count,
        "failed_count": run.failed_count,
        "error_code": run.error_code,
        "error_summary": run.error_summary,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at) if run.started_at else None,
        "finished_at": _iso(run.finished_at) if run.finished_at else None,
        "trigger": run.trigger.value,
        "window_start": _iso(run.window_start) if run.window_start else None,
        "window_end": _iso(run.window_end) if run.window_end else None,
        "scheduled_for": _iso(run.scheduled_for) if run.scheduled_for else None,
    }


def _run_from_dict(row: dict[str, Any]) -> ProductionRun:
    return ProductionRun(
        run_id=row["run_id"],
        topic_id=row["topic_id"],
        status=RunStatus(row["status"]),
        limit=row["limit"],
        candidate_count=row["candidate_count"],
        published_count=row["published_count"],
        failed_count=row["failed_count"],
        error_code=row["error_code"],
        error_summary=row["error_summary"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row.get("started_at") else None,
        finished_at=datetime.fromisoformat(row["finished_at"]) if row.get("finished_at") else None,
        trigger=RunTrigger(row["trigger"]),
        window_start=datetime.fromisoformat(row["window_start"]) if row.get("window_start") else None,
        window_end=datetime.fromisoformat(row["window_end"]) if row.get("window_end") else None,
        scheduled_for=datetime.fromisoformat(row["scheduled_for"]) if row.get("scheduled_for") else None,
    )
