"""Daily scheduling adapters around the framework-free topic service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time
import logging
import re
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from research_pulse.topics.contracts import ActiveRunConflict, ScheduledRunExists
from research_pulse.topics.service import TopicRunService


LOGGER = logging.getLogger(__name__)
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = True
    daily_time: time = time(8, 0)
    timezone: str = "Asia/Shanghai"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "SchedulerConfig":
        enabled = _parse_bool(environment.get("RESEARCH_PULSE_SCHEDULER_ENABLED", "true"))
        daily_text = environment.get("RESEARCH_PULSE_DAILY_TIME", "08:00")
        if not _TIME_PATTERN.fullmatch(daily_text):
            raise RuntimeError("RESEARCH_PULSE_DAILY_TIME must use 24-hour HH:MM format")
        daily_time = time.fromisoformat(daily_text)
        timezone_name = environment.get("RESEARCH_PULSE_TIMEZONE", "Asia/Shanghai")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise RuntimeError("RESEARCH_PULSE_TIMEZONE must be a valid IANA timezone") from error
        return cls(enabled=enabled, daily_time=daily_time, timezone=timezone_name)

    def scheduled_slot(self, value: datetime) -> datetime:
        aware = _aware(value, "scheduler clock")
        local = aware.astimezone(ZoneInfo(self.timezone))
        return local.replace(
            hour=self.daily_time.hour,
            minute=self.daily_time.minute,
            second=0,
            microsecond=0,
        ).astimezone(UTC)


@dataclass(frozen=True)
class SchedulerStatus:
    enabled: bool
    timezone: str
    daily_time: str
    next_run_at: datetime | None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScheduleTickResult:
    scheduled_for: datetime
    accepted_run_ids: tuple[str, ...]
    skipped_topic_ids: tuple[str, ...]
    failed_topic_ids: tuple[str, ...]


class ScheduleCoordinator:
    """Create due runs without knowing how the scheduler or executor works."""

    def __init__(
        self,
        service: TopicRunService,
        *,
        submitter: Callable[[str], None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service = service
        self.submitter = submitter
        self.clock = clock

    def run_due(self, *, scheduled_for: datetime) -> ScheduleTickResult:
        slot = _aware(scheduled_for, "scheduled_for")
        window_end = _aware(self.clock(), "scheduler clock")
        accepted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for topic in self.service.list_enabled_topics():
            try:
                run = self.service.schedule(topic.topic_id, scheduled_for=slot, window_end=window_end)
            except (ActiveRunConflict, ScheduledRunExists):
                skipped.append(topic.topic_id)
                continue
            except Exception as error:
                failed.append(topic.topic_id)
                LOGGER.warning("schedule_submit_failed topic_id=%s error_type=%s", topic.topic_id, type(error).__name__)
                continue
            try:
                self.submitter(run.run_id)
            except Exception as error:
                failed.append(topic.topic_id)
                LOGGER.warning("schedule_execute_submit_failed topic_id=%s error_type=%s", topic.topic_id, type(error).__name__)
                continue
            accepted.append(run.run_id)
        return ScheduleTickResult(slot, tuple(accepted), tuple(skipped), tuple(failed))


class DailyScheduler:
    """Small APScheduler lifecycle wrapper with a stable, safe status surface."""

    JOB_ID = "research-pulse-daily-production"

    def __init__(
        self,
        config: SchedulerConfig,
        coordinator: ScheduleCoordinator,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        scheduler_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.coordinator = coordinator
        self.clock = clock
        self.scheduler_factory = scheduler_factory
        self._scheduler: Any | None = None

    def start(self) -> None:
        if not self.config.enabled or self._scheduler is not None:
            return
        if self.scheduler_factory is None:
            from apscheduler.schedulers.background import BackgroundScheduler

            factory = BackgroundScheduler
        else:
            factory = self.scheduler_factory
        from apscheduler.triggers.cron import CronTrigger

        scheduler = factory(timezone=ZoneInfo(self.config.timezone))
        trigger = CronTrigger(
            hour=self.config.daily_time.hour,
            minute=self.config.daily_time.minute,
            timezone=ZoneInfo(self.config.timezone),
        )
        scheduler.add_job(
            self._fire,
            trigger=trigger,
            id=self.JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        scheduler.start()
        self._scheduler = scheduler

    def _fire(self) -> None:
        self.coordinator.run_due(scheduled_for=self.config.scheduled_slot(self.clock()))

    def status(self) -> SchedulerStatus:
        next_run_at = None
        if self.config.enabled and self._scheduler is not None:
            job = self._scheduler.get_job(self.JOB_ID)
            next_run_at = getattr(job, "next_run_time", None) if job is not None else None
        return SchedulerStatus(
            enabled=self.config.enabled,
            timezone=self.config.timezone,
            daily_time=self.config.daily_time.strftime("%H:%M"),
            next_run_at=next_run_at,
        )

    def shutdown(self) -> None:
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            scheduler.shutdown(wait=False)


def build_scheduler_lifespan(scheduler: DailyScheduler, executor: Any):
    """Bind scheduler and worker-pool cleanup to the FastAPI lifecycle."""

    @asynccontextmanager
    async def lifespan(_app):
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()
            executor.shutdown(wait=False, cancel_futures=False)

    return lifespan


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("RESEARCH_PULSE_SCHEDULER_ENABLED must be true or false")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value
