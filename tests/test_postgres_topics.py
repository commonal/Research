from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest import TestCase, skipUnless
from uuid import uuid4

from research_pulse.topics.contracts import ActiveRunConflict, ScheduledRunExists
from research_pulse.topics.models import ProductionRun, ResearchTopic, RunStatus, RunTrigger
from research_pulse.topics.postgres import PostgresTopicRepository


DATABASE_URL = os.getenv("RESEARCH_PULSE_TEST_DATABASE_URL")


@skipUnless(DATABASE_URL, "RESEARCH_PULSE_TEST_DATABASE_URL is not configured")
class PostgresTopicRepositoryTests(TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        self.repository = PostgresTopicRepository(DATABASE_URL)
        self.repository.initialize()
        self.prefix = f"test-{uuid4()}"

    def tearDown(self) -> None:
        import psycopg

        assert DATABASE_URL is not None
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM research_topics WHERE topic_id LIKE %s", (f"{self.prefix}%",))

    def _topic_and_run(self) -> tuple[ResearchTopic, ProductionRun]:
        now = datetime.now(UTC)
        topic = ResearchTopic.create(
            topic_id=f"{self.prefix}-topic",
            name="Agent memory",
            query="LLM agent memory",
            created_at=now,
        )
        run = ProductionRun.queued(
            run_id=f"{self.prefix}-run-1",
            topic_id=topic.topic_id,
            created_at=now,
        )
        return topic, run

    def test_initialize_is_idempotent_and_created_topic_is_retrievable(self) -> None:
        self.repository.initialize()
        topic, run = self._topic_and_run()

        self.repository.create_topic_with_run(topic, run)

        self.assertEqual(self.repository.get_topic(topic.topic_id), topic)
        self.assertEqual(self.repository.get_run(run.run_id), run)
        listed = dict(self.repository.list_topics_with_latest_run())
        self.assertEqual(listed[topic], run)

    def test_active_run_is_unique_but_terminal_run_can_be_retried(self) -> None:
        topic, first = self._topic_and_run()
        self.repository.create_topic_with_run(topic, first)
        second = ProductionRun.queued(run_id=f"{self.prefix}-run-2", topic_id=topic.topic_id)

        with self.assertRaises(ActiveRunConflict) as conflict:
            self.repository.create_run(second)

        self.assertEqual(conflict.exception.active_run.run_id, first.run_id)
        terminal = first.start().finish(
            status=RunStatus.COMPLETED,
            candidate_count=0,
            published_count=0,
            failed_count=0,
        )
        self.repository.save_run(terminal)
        self.repository.create_run(second)
        self.assertEqual(self.repository.get_run(second.run_id), second)

    def test_reconcile_marks_stale_run_failed_with_safe_error(self) -> None:
        topic, run = self._topic_and_run()
        self.repository.create_topic_with_run(topic, run)
        self.repository.save_run(run.start())

        reconciled = self.repository.reconcile_active_runs()

        stored = self.repository.get_run(run.run_id)
        assert stored is not None
        self.assertEqual(reconciled, 1)
        self.assertEqual(stored.status, RunStatus.FAILED)
        self.assertEqual(stored.error_code, "process_restarted")
        self.assertNotIn("postgres", (stored.error_summary or "").lower())
        self.assertNotIn("api", (stored.error_summary or "").lower())

    def test_settings_enabled_listing_and_atomic_watermark_are_persisted(self) -> None:
        topic, run = self._topic_and_run()
        self.repository.create_topic_with_run(topic, run)
        paused = topic.update_settings(enabled=False, daily_limit=2)
        self.repository.update_topic(paused)

        self.assertEqual(self.repository.get_topic(topic.topic_id), paused)
        self.assertNotIn(paused, self.repository.list_enabled_topics())

        running = run.start()
        self.repository.save_run(running)
        terminal = running.finish(
            status=RunStatus.COMPLETED,
            candidate_count=0,
            published_count=0,
            failed_count=0,
        )
        watermark = datetime.now(UTC)
        self.repository.finish_run(terminal, discovery_succeeded=True, discovery_watermark=watermark)

        stored = self.repository.get_topic(topic.topic_id)
        assert stored is not None
        self.assertEqual(stored.last_successful_discovery_at, watermark)

    def test_scheduled_slot_is_unique_after_first_run_finishes(self) -> None:
        topic, initial = self._topic_and_run()
        self.repository.create_topic_with_run(topic, initial)
        initial_terminal = initial.start().finish(
            status=RunStatus.COMPLETED,
            candidate_count=0,
            published_count=0,
            failed_count=0,
        )
        self.repository.finish_run(initial_terminal, discovery_succeeded=False, discovery_watermark=None)
        slot = datetime.now(UTC)
        first = ProductionRun.queued(
            run_id=f"{self.prefix}-scheduled-1",
            topic_id=topic.topic_id,
            created_at=slot,
            trigger=RunTrigger.SCHEDULED,
            scheduled_for=slot,
            window_end=slot,
        )
        self.repository.create_run(first)
        first_terminal = first.start().finish(
            status=RunStatus.COMPLETED,
            candidate_count=0,
            published_count=0,
            failed_count=0,
        )
        self.repository.finish_run(first_terminal, discovery_succeeded=True, discovery_watermark=slot)
        duplicate = ProductionRun.queued(
            run_id=f"{self.prefix}-scheduled-2",
            topic_id=topic.topic_id,
            created_at=slot,
            trigger=RunTrigger.SCHEDULED,
            scheduled_for=slot,
            window_end=slot,
        )

        with self.assertRaises(ScheduledRunExists):
            self.repository.create_run(duplicate)
