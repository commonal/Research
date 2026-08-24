from __future__ import annotations

from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.topics.contracts import ActiveRunConflict, ScheduledRunExists
from research_pulse.topics.models import ProductionRun, RunStatus, RunTrigger, ResearchTopic, TopicValidationError
from research_pulse.topics.service import TopicRunService


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


class ResearchTopicModelTests(TestCase):
    def test_valid_topic_has_stable_server_domain_and_trimmed_input(self) -> None:
        topic = ResearchTopic.create(
            name="  Agent 记忆  ",
            query="  LLM agent memory  ",
            topic_id="topic-123",
            created_at=NOW,
        )

        self.assertEqual(topic.topic_id, "topic-123")
        self.assertEqual(topic.domain, "topic:topic-123")
        self.assertEqual(topic.name, "Agent 记忆")
        self.assertEqual(topic.query, "LLM agent memory")
        self.assertTrue(topic.enabled)
        self.assertEqual(topic.daily_limit, 3)
        self.assertIsNone(topic.last_successful_discovery_at)

    def test_blank_or_oversized_topic_input_is_rejected(self) -> None:
        invalid_inputs = [
            {"name": " ", "query": "memory"},
            {"name": "memory", "query": "\t"},
            {"name": "x" * 121, "query": "memory"},
            {"name": "memory", "query": "x" * 501},
        ]

        for values in invalid_inputs:
            with self.subTest(values=values), self.assertRaises(TopicValidationError):
                ResearchTopic.create(**values, topic_id="topic-123", created_at=NOW)

    def test_run_accepts_only_valid_state_transitions_and_limit(self) -> None:
        run = ProductionRun.queued(run_id="run-1", topic_id="topic-123", created_at=NOW, limit=3)
        running = run.start(started_at=NOW)
        completed = running.finish(
            status=RunStatus.COMPLETED,
            candidate_count=2,
            published_count=1,
            failed_count=0,
            finished_at=NOW,
        )

        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(completed.published_count, 1)
        with self.assertRaises(TopicValidationError):
            run.finish(
                status=RunStatus.COMPLETED,
                candidate_count=0,
                published_count=0,
                failed_count=0,
                finished_at=NOW,
            )
        with self.assertRaises(TopicValidationError):
            ProductionRun.queued(run_id="run-2", topic_id="topic-123", created_at=NOW, limit=4)

    def test_run_failure_keeps_only_whitelisted_error_copy(self) -> None:
        failed = ProductionRun.queued(
            run_id="run-1",
            topic_id="topic-123",
            created_at=NOW,
        ).start(started_at=NOW).finish(
            status=RunStatus.FAILED,
            candidate_count=0,
            published_count=0,
            failed_count=0,
            finished_at=NOW,
            error_code="production_run_failed",
            error_summary="provider body contains sk-secret and C:\\private\\paper.pdf",
        )

        self.assertEqual(failed.error_code, "production_run_failed")
        self.assertEqual(failed.error_summary, "论文生产未完成，请稍后重试。")
        self.assertNotIn("secret", failed.error_summary)
        self.assertNotIn("private", failed.error_summary)

    def test_topic_settings_and_run_window_combinations_are_validated(self) -> None:
        topic = ResearchTopic.create(name="Memory", query="agent memory", topic_id="topic-123", created_at=NOW)
        updated = topic.update_settings(enabled=False, daily_limit=2).advance_discovery_watermark(NOW)

        self.assertFalse(updated.enabled)
        self.assertEqual(updated.daily_limit, 2)
        self.assertEqual(updated.last_successful_discovery_at, NOW)
        scheduled = ProductionRun.queued(
            run_id="scheduled-1",
            topic_id=topic.topic_id,
            created_at=NOW,
            trigger=RunTrigger.SCHEDULED,
            scheduled_for=NOW,
            window_end=NOW,
        )
        self.assertEqual(scheduled.trigger, RunTrigger.SCHEDULED)
        with self.assertRaises(TopicValidationError):
            topic.update_settings(daily_limit=4)
        with self.assertRaises(TopicValidationError):
            topic.update_settings(enabled="false")
        with self.assertRaises(TopicValidationError):
            ProductionRun.queued(
                run_id="scheduled-2",
                topic_id=topic.topic_id,
                created_at=NOW,
                trigger=RunTrigger.SCHEDULED,
            )


class _MemoryRepository:
    def __init__(self) -> None:
        self.topics: dict[str, ResearchTopic] = {}
        self.runs: dict[str, ProductionRun] = {}

    def initialize(self) -> None:
        return None

    def create_topic_with_run(self, topic: ResearchTopic, run: ProductionRun) -> None:
        self.topics[topic.topic_id] = topic
        self.runs[run.run_id] = run

    def list_topics_with_latest_run(self):
        result = []
        for topic in self.topics.values():
            runs = sorted(
                (run for run in self.runs.values() if run.topic_id == topic.topic_id),
                key=lambda run: run.created_at,
                reverse=True,
            )
            result.append((topic, runs[0] if runs else None))
        return result

    def list_enabled_topics(self):
        return [topic for topic in self.topics.values() if topic.enabled]

    def get_topic(self, topic_id: str):
        return self.topics.get(topic_id)

    def get_run(self, run_id: str):
        return self.runs.get(run_id)

    def create_run(self, run: ProductionRun) -> None:
        duplicate = next(
            (
                value
                for value in self.runs.values()
                if run.trigger is RunTrigger.SCHEDULED
                and value.trigger is RunTrigger.SCHEDULED
                and value.topic_id == run.topic_id
                and value.scheduled_for == run.scheduled_for
            ),
            None,
        )
        if duplicate:
            raise ScheduledRunExists(duplicate)
        active = next(
            (
                value
                for value in self.runs.values()
                if value.topic_id == run.topic_id and value.status in {RunStatus.QUEUED, RunStatus.RUNNING}
            ),
            None,
        )
        if active:
            raise ActiveRunConflict(active)
        self.runs[run.run_id] = run

    def update_topic(self, topic: ResearchTopic) -> None:
        self.topics[topic.topic_id] = topic

    def save_run(self, run: ProductionRun) -> None:
        self.runs[run.run_id] = run

    def finish_run(self, run, *, discovery_succeeded: bool, discovery_watermark) -> None:
        self.runs[run.run_id] = run
        if discovery_succeeded and discovery_watermark is not None:
            self.topics[run.topic_id] = self.topics[run.topic_id].advance_discovery_watermark(discovery_watermark)

    def reconcile_active_runs(self) -> int:
        return 0


class _Runner:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or {"candidate_ids": [], "receipts": [], "discovery_succeeded": True}
        self.error = error
        self.inputs: list[dict[str, object]] = []

    def run(self, *, run_id: str, topic: str, domain: str, limit: int, window_start, window_end):
        self.inputs.append({
            "run_id": run_id,
            "topic": topic,
            "domain": domain,
            "limit": limit,
            "window_start": window_start,
            "window_end": window_end,
        })
        if self.error:
            raise self.error
        return self.result


class TopicRunServiceTests(TestCase):
    def _service(self, runner: _Runner | None):
        repository = _MemoryRepository()
        service = TopicRunService(
            repository,
            runner=runner,
            topic_id_factory=lambda: "topic-123",
            run_id_factory=lambda: f"run-{len(repository.runs) + 1}",
            clock=lambda: NOW,
        )
        return service, repository

    def test_created_topic_and_queued_run_are_persisted_with_limit_three(self) -> None:
        service, repository = self._service(_Runner())

        topic, run = service.create_topic(name="Agent 记忆", query="LLM agent memory")

        self.assertEqual(topic.domain, "topic:topic-123")
        self.assertEqual(run.limit, 3)
        self.assertEqual(run.trigger, RunTrigger.INITIAL)
        self.assertEqual(repository.get_topic(topic.topic_id), topic)
        self.assertEqual(repository.get_run(run.run_id), run)

    def test_receipts_determine_completed_partial_and_failed_terminal_states(self) -> None:
        cases = [
            (["published", "skipped_duplicate"], RunStatus.COMPLETED, 1, 0),
            (["published", "failed"], RunStatus.PARTIAL_FAILED, 1, 1),
            (["needs_review"], RunStatus.FAILED, 0, 1),
            ([], RunStatus.COMPLETED, 0, 0),
        ]
        for statuses, expected_status, published, failed in cases:
            with self.subTest(statuses=statuses):
                receipts = [
                    {"source_id": f"paper-{index}", "status": status}
                    for index, status in enumerate(statuses)
                ]
                runner = _Runner({
                    "candidate_ids": [r["source_id"] for r in receipts],
                    "receipts": receipts,
                    "discovery_succeeded": True,
                })
                service, _ = self._service(runner)
                _, run = service.create_topic(name="Agent 记忆", query="LLM agent memory")

                terminal = service.execute(run.run_id)

                self.assertEqual(terminal.status, expected_status)
                self.assertEqual(terminal.candidate_count, len(statuses))
                self.assertEqual(terminal.published_count, published)
                self.assertEqual(terminal.failed_count, failed)
                self.assertEqual(runner.inputs[0]["limit"], 3)

    def test_settings_scheduled_idempotency_and_watermark_updates(self) -> None:
        service, repository = self._service(_Runner())
        topic, initial = service.create_topic(name="Agent 记忆", query="LLM agent memory")
        service.execute(initial.run_id)
        paused = service.update_topic_settings(topic.topic_id, enabled=False, daily_limit=2)
        self.assertFalse(paused.enabled)
        self.assertEqual(paused.daily_limit, 2)
        self.assertEqual(paused.last_successful_discovery_at, NOW)

        scheduled = service.schedule(topic.topic_id, scheduled_for=NOW, window_end=NOW)
        self.assertEqual(scheduled.trigger, RunTrigger.SCHEDULED)
        self.assertEqual(scheduled.limit, 2)
        with self.assertRaises(ScheduledRunExists):
            service.schedule(topic.topic_id, scheduled_for=NOW, window_end=NOW)

    def test_missing_deepseek_and_provider_exception_become_safe_failures(self) -> None:
        cases = [
            (None, "deepseek_not_configured"),
            (_Runner(error=RuntimeError("sk-secret C:\\private\\paper.pdf provider body")), "production_run_failed"),
        ]
        for runner, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                service, _ = self._service(runner)
                topic, run = service.create_topic(name="Agent 记忆", query="LLM agent memory")

                terminal = service.execute(run.run_id)

                self.assertEqual(topic.name, "Agent 记忆")
                self.assertEqual(terminal.status, RunStatus.FAILED)
                self.assertEqual(terminal.error_code, expected_code)
                self.assertNotIn("secret", terminal.error_summary or "")
                self.assertNotIn("private", terminal.error_summary or "")

    def test_discovery_failure_does_not_advance_existing_watermark(self) -> None:
        service, repository = self._service(_Runner())
        topic, initial = service.create_topic(name="Agent 记忆", query="LLM agent memory")
        service.execute(initial.run_id)
        before = repository.get_topic(topic.topic_id).last_successful_discovery_at
        service.runner = _Runner(error=RuntimeError("arxiv failed"))
        retry = service.retry(topic.topic_id)

        terminal = service.execute(retry.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(repository.get_topic(topic.topic_id).last_successful_discovery_at, before)
