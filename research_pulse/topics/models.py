"""Framework-free research-topic and production-run domain objects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


MAX_TOPIC_NAME_LENGTH = 120
MAX_TOPIC_QUERY_LENGTH = 500
MAX_INITIAL_RUN_LIMIT = 3

SAFE_ERROR_SUMMARIES = {
    "process_restarted": "服务在任务完成前重启，请重新抓取。",
    "deepseek_not_configured": "尚未配置 DeepSeek，配置后可重新抓取。",
    "production_run_failed": "论文生产未完成，请稍后重试。",
    "candidate_failures": "部分候选未能通过处理或质量校验。",
}


class TopicValidationError(ValueError):
    """Raised when topic control data violates the public domain contract."""


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.PARTIAL_FAILED, self.FAILED}


class RunTrigger(StrEnum):
    INITIAL = "initial"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


@dataclass(frozen=True)
class ResearchTopic:
    topic_id: str
    name: str
    query: str
    domain: str
    created_at: datetime
    enabled: bool = True
    daily_limit: int = MAX_INITIAL_RUN_LIMIT
    last_successful_discovery_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        name: str,
        query: str,
        topic_id: str | None = None,
        created_at: datetime | None = None,
    ) -> "ResearchTopic":
        normalized_name = _required_text(name, "name", MAX_TOPIC_NAME_LENGTH)
        normalized_query = _required_text(query, "query", MAX_TOPIC_QUERY_LENGTH)
        stable_id = _required_text(topic_id or str(uuid4()), "topic_id", 200)
        return cls(
            topic_id=stable_id,
            name=normalized_name,
            query=normalized_query,
            domain=f"topic:{stable_id}",
            created_at=_aware(created_at or datetime.now(UTC)),
        )

    def update_settings(self, *, enabled: bool | None = None, daily_limit: int | None = None) -> "ResearchTopic":
        if enabled is not None and not isinstance(enabled, bool):
            raise TopicValidationError("enabled must be a boolean")
        next_limit = self.daily_limit if daily_limit is None else _daily_limit(daily_limit)
        return replace(self, enabled=self.enabled if enabled is None else enabled, daily_limit=next_limit)

    def advance_discovery_watermark(self, value: datetime) -> "ResearchTopic":
        watermark = _aware(value)
        if self.last_successful_discovery_at is not None and watermark < self.last_successful_discovery_at:
            raise TopicValidationError("discovery watermark cannot move backwards")
        return replace(self, last_successful_discovery_at=watermark)


@dataclass(frozen=True)
class ProductionRun:
    run_id: str
    topic_id: str
    status: RunStatus
    limit: int
    candidate_count: int
    published_count: int
    failed_count: int
    error_code: str | None
    error_summary: str | None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trigger: RunTrigger = RunTrigger.MANUAL
    window_start: datetime | None = None
    window_end: datetime | None = None
    scheduled_for: datetime | None = None

    @classmethod
    def queued(
        cls,
        *,
        topic_id: str,
        run_id: str | None = None,
        created_at: datetime | None = None,
        limit: int = MAX_INITIAL_RUN_LIMIT,
        trigger: RunTrigger = RunTrigger.MANUAL,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        scheduled_for: datetime | None = None,
    ) -> "ProductionRun":
        if not isinstance(trigger, RunTrigger):
            raise TopicValidationError("trigger must be a RunTrigger")
        normalized_limit = _daily_limit(limit)
        normalized_created = _aware(created_at or datetime.now(UTC))
        normalized_start = _aware(window_start) if window_start is not None else None
        normalized_end = _aware(window_end) if window_end is not None else normalized_created
        normalized_scheduled = _aware(scheduled_for) if scheduled_for is not None else None
        if normalized_start is not None and normalized_start > normalized_end:
            raise TopicValidationError("window_start cannot be later than window_end")
        if trigger is RunTrigger.SCHEDULED and normalized_scheduled is None:
            raise TopicValidationError("scheduled runs require scheduled_for")
        if trigger is not RunTrigger.SCHEDULED and normalized_scheduled is not None:
            raise TopicValidationError("only scheduled runs may set scheduled_for")
        return cls(
            run_id=_required_text(run_id or str(uuid4()), "run_id", 200),
            topic_id=_required_text(topic_id, "topic_id", 200),
            status=RunStatus.QUEUED,
            limit=normalized_limit,
            candidate_count=0,
            published_count=0,
            failed_count=0,
            error_code=None,
            error_summary=None,
            created_at=normalized_created,
            trigger=trigger,
            window_start=normalized_start,
            window_end=normalized_end,
            scheduled_for=normalized_scheduled,
        )

    def start(self, *, started_at: datetime | None = None) -> "ProductionRun":
        if self.status is not RunStatus.QUEUED:
            raise TopicValidationError("only queued runs can start")
        return replace(self, status=RunStatus.RUNNING, started_at=_aware(started_at or datetime.now(UTC)))

    def finish(
        self,
        *,
        status: RunStatus,
        candidate_count: int,
        published_count: int,
        failed_count: int,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> "ProductionRun":
        if self.status is not RunStatus.RUNNING:
            raise TopicValidationError("only running runs can finish")
        if not status.terminal:
            raise TopicValidationError("finish status must be terminal")
        if min(candidate_count, published_count, failed_count) < 0:
            raise TopicValidationError("run counts cannot be negative")
        if published_count + failed_count > candidate_count:
            raise TopicValidationError("published and failed counts cannot exceed candidates")
        safe_code = _optional_text(error_code, 80)
        if status is RunStatus.FAILED and safe_code is None:
            raise TopicValidationError("failed runs require a stable error code")
        if safe_code is not None and safe_code not in SAFE_ERROR_SUMMARIES:
            raise TopicValidationError("unknown error code")
        safe_summary = SAFE_ERROR_SUMMARIES.get(safe_code)
        return replace(
            self,
            status=status,
            candidate_count=candidate_count,
            published_count=published_count,
            failed_count=failed_count,
            error_code=safe_code,
            error_summary=safe_summary,
            finished_at=_aware(finished_at or datetime.now(UTC)),
        )


def _required_text(value: str, field: str, maximum: int) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise TopicValidationError(f"{field} is required")
    if len(normalized) > maximum:
        raise TopicValidationError(f"{field} must be at most {maximum} characters")
    return normalized


def _optional_text(value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized[:maximum] or None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise TopicValidationError("timestamps must include a timezone")
    return value


def _daily_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_INITIAL_RUN_LIMIT:
        raise TopicValidationError(f"daily_limit must be between 1 and {MAX_INITIAL_RUN_LIMIT}")
    return value
