"""Public persistence seam for research topics and production runs."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from research_pulse.topics.models import ProductionRun, ResearchTopic


class TopicNotFoundError(LookupError):
    pass


class RunNotFoundError(LookupError):
    pass


class ActiveRunConflict(RuntimeError):
    def __init__(self, active_run: ProductionRun) -> None:
        super().__init__("the topic already has an active production run")
        self.active_run = active_run


class ScheduledRunExists(RuntimeError):
    def __init__(self, existing_run: ProductionRun) -> None:
        super().__init__("the topic already has a run for this scheduled time")
        self.existing_run = existing_run


class TopicRepository(Protocol):
    def initialize(self) -> None: ...

    def create_topic_with_run(self, topic: ResearchTopic, run: ProductionRun) -> None: ...

    def list_topics_with_latest_run(self) -> Sequence[tuple[ResearchTopic, ProductionRun | None]]: ...

    def list_enabled_topics(self) -> Sequence[ResearchTopic]: ...

    def get_topic(self, topic_id: str) -> ResearchTopic | None: ...

    def get_run(self, run_id: str) -> ProductionRun | None: ...

    def create_run(self, run: ProductionRun) -> None: ...

    def update_topic(self, topic: ResearchTopic) -> None: ...

    def save_run(self, run: ProductionRun) -> None: ...

    def finish_run(
        self,
        run: ProductionRun,
        *,
        discovery_succeeded: bool,
        discovery_watermark: datetime | None,
    ) -> None: ...

    def reconcile_active_runs(self) -> int: ...
