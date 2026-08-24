from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import TestCase
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from research_pulse.scheduling import DailyScheduler, ScheduleCoordinator, SchedulerConfig, build_scheduler_lifespan
from research_pulse.production.pipeline import ProductionService
from research_pulse.topics.models import ProductionRun, ResearchTopic, RunTrigger
from research_pulse.topics.service import LangGraphProductionRunner, TopicRunService
from research_pulse.workflows.production import ProductionGraphDependencies, build_production_graph
from tests.test_production_pipeline import _Extractor, _Finder, _Judge, _Parser, _Publisher, _Registry, _candidate
from tests.test_topics import NOW, _MemoryRepository, _Runner


class SchedulerConfigTests(TestCase):
    def test_defaults_disable_override_and_invalid_values(self) -> None:
        defaults = SchedulerConfig.from_environment({})
        disabled = SchedulerConfig.from_environment({"RESEARCH_PULSE_SCHEDULER_ENABLED": "false"})

        self.assertTrue(defaults.enabled)
        self.assertEqual(defaults.daily_time.isoformat(timespec="minutes"), "08:00")
        self.assertEqual(defaults.timezone, "Asia/Shanghai")
        self.assertFalse(disabled.enabled)
        for environment in (
            {"RESEARCH_PULSE_SCHEDULER_ENABLED": "maybe"},
            {"RESEARCH_PULSE_DAILY_TIME": "8:00"},
            {"RESEARCH_PULSE_TIMEZONE": "Mars/Olympus"},
        ):
            with self.subTest(environment=environment), self.assertRaises(RuntimeError):
                SchedulerConfig.from_environment(environment)

    def test_scheduled_slot_uses_configured_timezone(self) -> None:
        config = SchedulerConfig.from_environment({})
        slot = config.scheduled_slot(datetime(2026, 8, 22, 1, tzinfo=UTC))

        self.assertEqual(slot, datetime(2026, 8, 22, 0, tzinfo=UTC))


class ScheduleCoordinatorTests(TestCase):
    def test_only_enabled_topics_submit_and_duplicate_slot_is_skipped(self) -> None:
        repository = _MemoryRepository()
        enabled = ResearchTopic.create(name="Enabled", query="agent", topic_id="enabled", created_at=NOW)
        paused = ResearchTopic.create(name="Paused", query="rag", topic_id="paused", created_at=NOW).update_settings(enabled=False)
        repository.topics = {enabled.topic_id: enabled, paused.topic_id: paused}
        service = TopicRunService(
            repository,
            runner=_Runner(),
            run_id_factory=lambda: f"run-{len(repository.runs) + 1}",
            clock=lambda: NOW,
        )
        submitted: list[str] = []
        coordinator = ScheduleCoordinator(service, submitter=submitted.append, clock=lambda: NOW)

        first = coordinator.run_due(scheduled_for=NOW)
        second = coordinator.run_due(scheduled_for=NOW)

        self.assertEqual(first.accepted_run_ids, ("run-1",))
        self.assertEqual(submitted, ["run-1"])
        self.assertEqual(second.skipped_topic_ids, (enabled.topic_id,))
        self.assertNotIn(paused.topic_id, first.skipped_topic_ids)

    def test_one_topic_failure_does_not_stop_later_topics(self) -> None:
        first = ResearchTopic.create(name="First", query="one", topic_id="first", created_at=NOW)
        second = ResearchTopic.create(name="Second", query="two", topic_id="second", created_at=NOW)

        class Service:
            def list_enabled_topics(self):
                return [first, second]

            def schedule(self, topic_id, *, scheduled_for, window_end):
                if topic_id == "first":
                    raise RuntimeError("provider secret")
                return ProductionRun.queued(
                    run_id="run-second",
                    topic_id=topic_id,
                    created_at=NOW,
                    trigger=RunTrigger.SCHEDULED,
                    scheduled_for=scheduled_for,
                    window_end=window_end,
                )

        submitted: list[str] = []
        result = ScheduleCoordinator(Service(), submitter=submitted.append, clock=lambda: NOW).run_due(scheduled_for=NOW)

        self.assertEqual(result.failed_topic_ids, ("first",))
        self.assertEqual(result.accepted_run_ids, ("run-second",))
        self.assertEqual(submitted, ["run-second"])

    def test_due_topic_runs_the_same_quality_gated_graph_and_publishes(self) -> None:
        repository = _MemoryRepository()
        topic = ResearchTopic.create(name="Memory", query="agent memory", topic_id="topic-e2e", created_at=NOW)
        repository.topics[topic.topic_id] = topic
        publisher = _Publisher()
        production_service = ProductionService(_Parser(), _Extractor(), publisher, _Registry(), _Judge())
        graph = build_production_graph(
            ProductionGraphDependencies(_Finder([_candidate("scheduled-paper")]), production_service),
            checkpointer=MemorySaver(),
        )
        service = TopicRunService(
            repository,
            runner=LangGraphProductionRunner(graph),
            run_id_factory=lambda: "scheduled-run",
            clock=lambda: NOW,
        )
        coordinator = ScheduleCoordinator(service, submitter=lambda run_id: service.execute(run_id), clock=lambda: NOW)

        result = coordinator.run_due(scheduled_for=NOW)

        self.assertEqual(result.accepted_run_ids, ("scheduled-run",))
        stored = repository.get_run("scheduled-run")
        self.assertEqual(stored.status.value, "completed")
        self.assertEqual(stored.published_count, 1)
        self.assertEqual(repository.get_topic(topic.topic_id).last_successful_discovery_at, NOW)
        self.assertEqual([bundle.asset.knowledge_id for bundle in publisher.published], ["kp:arxiv:scheduled-paper"])


class _FakeScheduler:
    def __init__(self, **kwargs) -> None:
        self.job = None
        self.started = False
        self.stopped = False

    def add_job(self, function, **kwargs) -> None:
        self.function = function
        self.job = SimpleNamespace(next_run_time=datetime(2026, 8, 23, tzinfo=UTC))

    def start(self) -> None:
        self.started = True

    def get_job(self, job_id):
        return self.job

    def shutdown(self, *, wait: bool) -> None:
        self.stopped = True


class DailySchedulerTests(TestCase):
    def test_start_registers_future_job_without_immediate_backfill_and_shutdowns(self) -> None:
        calls: list[datetime] = []
        coordinator = SimpleNamespace(run_due=lambda *, scheduled_for: calls.append(scheduled_for))
        fake = _FakeScheduler()
        scheduler = DailyScheduler(
            SchedulerConfig(),
            coordinator,
            clock=lambda: NOW,
            scheduler_factory=lambda **kwargs: fake,
        )

        scheduler.start()

        self.assertTrue(fake.started)
        self.assertEqual(calls, [])
        self.assertEqual(scheduler.status().next_run_at, datetime(2026, 8, 23, tzinfo=UTC))
        fake.function()
        self.assertEqual(calls, [datetime(2026, 8, 22, 0, tzinfo=UTC)])
        scheduler.shutdown()
        self.assertTrue(fake.stopped)

    def test_disabled_scheduler_registers_no_job(self) -> None:
        scheduler = DailyScheduler(
            SchedulerConfig(enabled=False),
            SimpleNamespace(run_due=lambda **kwargs: None),
            scheduler_factory=lambda **kwargs: self.fail("factory should not be called"),
        )

        scheduler.start()

        self.assertFalse(scheduler.status().enabled)
        self.assertIsNone(scheduler.status().next_run_at)

    def test_fastapi_lifespan_starts_and_releases_scheduler_resources(self) -> None:
        events: list[str] = []

        class Scheduler:
            def start(self):
                events.append("start")

            def shutdown(self):
                events.append("scheduler_shutdown")

        class Executor:
            def shutdown(self, **kwargs):
                events.append("executor_shutdown")

        app = FastAPI(lifespan=build_scheduler_lifespan(Scheduler(), Executor()))
        app.get("/health")(lambda: {"status": "ok"})

        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(events, ["start"])

        self.assertEqual(events, ["start", "scheduler_shutdown", "executor_shutdown"])
