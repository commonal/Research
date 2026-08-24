"""Application service for creating topics and observing bounded production runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable, Protocol, Sequence
from uuid import uuid4

from research_pulse.topics.contracts import RunNotFoundError, TopicNotFoundError, TopicRepository
from research_pulse.topics.models import MAX_INITIAL_RUN_LIMIT, ProductionRun, ResearchTopic, RunStatus, RunTrigger


class ProductionBatchRunner(Protocol):
    def run(
        self,
        *,
        run_id: str,
        topic: str,
        domain: str,
        limit: int,
        window_start: datetime | None,
        window_end: datetime,
    ) -> dict[str, Any]: ...


class LangGraphProductionRunner:
    """Narrow adapter that keeps LangGraph outside the topic domain model."""

    def __init__(self, graph: Any) -> None:
        self.graph = graph

    def run(
        self,
        *,
        run_id: str,
        topic: str,
        domain: str,
        limit: int,
        window_start: datetime | None,
        window_end: datetime,
    ) -> dict[str, Any]:
        return self.graph.invoke(
            {
                "run_id": run_id,
                "topic": topic,
                "domain": domain,
                "limit": min(limit, MAX_INITIAL_RUN_LIMIT),
                "window_start": window_start,
                "window_end": window_end,
            },
            {"configurable": {"thread_id": f"production:{run_id}"}},
        )


class TopicRunService:
    def __init__(
        self,
        repository: TopicRepository,
        *,
        runner: ProductionBatchRunner | None,
        topic_id_factory: Callable[[], str] = lambda: str(uuid4()),
        run_id_factory: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.runner = runner
        self.topic_id_factory = topic_id_factory
        self.run_id_factory = run_id_factory
        self.clock = clock

    def create_topic(self, *, name: str, query: str) -> tuple[ResearchTopic, ProductionRun]:
        now = self.clock()
        topic = ResearchTopic.create(
            topic_id=self.topic_id_factory(),
            name=name,
            query=query,
            created_at=now,
        )
        run = ProductionRun.queued(
            run_id=self.run_id_factory(),
            topic_id=topic.topic_id,
            created_at=now,
            limit=MAX_INITIAL_RUN_LIMIT,
            trigger=RunTrigger.INITIAL,
            window_end=now,
        )
        self.repository.create_topic_with_run(topic, run)
        return topic, run

    def list_topics(self) -> Sequence[tuple[ResearchTopic, ProductionRun | None]]:
        return self.repository.list_topics_with_latest_run()

    def get_run(self, run_id: str) -> ProductionRun:
        run = self.repository.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def update_topic_settings(
        self,
        topic_id: str,
        *,
        enabled: bool | None = None,
        daily_limit: int | None = None,
    ) -> ResearchTopic:
        topic = self.repository.get_topic(topic_id)
        if topic is None:
            raise TopicNotFoundError(topic_id)
        updated = topic.update_settings(enabled=enabled, daily_limit=daily_limit)
        self.repository.update_topic(updated)
        return updated

    def list_enabled_topics(self) -> Sequence[ResearchTopic]:
        return self.repository.list_enabled_topics()

    def retry(self, topic_id: str) -> ProductionRun:
        topic = self.repository.get_topic(topic_id)
        if topic is None:
            raise TopicNotFoundError(topic_id)
        run = ProductionRun.queued(
            run_id=self.run_id_factory(),
            topic_id=topic.topic_id,
            created_at=self.clock(),
            limit=topic.daily_limit,
            trigger=RunTrigger.MANUAL,
            window_start=topic.last_successful_discovery_at,
            window_end=self.clock(),
        )
        self.repository.create_run(run)
        return run

    def schedule(self, topic_id: str, *, scheduled_for: datetime, window_end: datetime) -> ProductionRun:
        topic = self.repository.get_topic(topic_id)
        if topic is None:
            raise TopicNotFoundError(topic_id)
        run = ProductionRun.queued(
            run_id=self.run_id_factory(),
            topic_id=topic.topic_id,
            created_at=self.clock(),
            limit=topic.daily_limit,
            trigger=RunTrigger.SCHEDULED,
            window_start=topic.last_successful_discovery_at,
            window_end=window_end,
            scheduled_for=scheduled_for,
        )
        self.repository.create_run(run)
        return run

    def execute(self, run_id: str) -> ProductionRun:
        run = self.get_run(run_id)
        topic = self.repository.get_topic(run.topic_id)
        if topic is None:
            raise TopicNotFoundError(run.topic_id)
        running = run.start(started_at=self.clock())
        self.repository.save_run(running)
        if self.runner is None:
            return self._fail(running, "deepseek_not_configured")
        try:
            result = self.runner.run(
                run_id=running.run_id,
                topic=topic.query,
                domain=topic.domain,
                limit=min(running.limit, MAX_INITIAL_RUN_LIMIT),
                window_start=running.window_start,
                window_end=running.window_end or running.created_at,
            )
        except Exception:
            return self._fail(running, "production_run_failed")
        terminal = _terminal_from_result(running, result, self.clock())
        discovery_succeeded = result.get("discovery_succeeded") is True
        self.repository.finish_run(
            terminal,
            discovery_succeeded=discovery_succeeded,
            discovery_watermark=running.window_end if discovery_succeeded else None,
        )
        return terminal

    def _fail(self, running: ProductionRun, error_code: str) -> ProductionRun:
        failed = running.finish(
            status=RunStatus.FAILED,
            candidate_count=running.candidate_count,
            published_count=running.published_count,
            failed_count=running.failed_count,
            error_code=error_code,
            finished_at=self.clock(),
        )
        self.repository.finish_run(failed, discovery_succeeded=False, discovery_watermark=None)
        return failed


def _terminal_from_result(run: ProductionRun, result: dict[str, Any], finished_at: datetime) -> ProductionRun:
    receipts = result.get("receipts", [])
    candidate_ids = result.get("candidate_ids", [])
    candidate_count = max(len(candidate_ids), len(receipts))
    published_count = sum(receipt.get("status") == "published" for receipt in receipts)
    failed_count = sum(
        receipt.get("status") not in {"published", "skipped_duplicate"}
        for receipt in receipts
    )
    if failed_count and published_count:
        status = RunStatus.PARTIAL_FAILED
        error_code = "candidate_failures"
    elif failed_count:
        status = RunStatus.FAILED
        error_code = "candidate_failures"
    else:
        status = RunStatus.COMPLETED
        error_code = None
    return run.finish(
        status=status,
        candidate_count=candidate_count,
        published_count=published_count,
        failed_count=failed_count,
        error_code=error_code,
        finished_at=finished_at,
    )
