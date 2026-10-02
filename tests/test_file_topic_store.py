"""File-backed topic store + note-only daily runner tests (no database)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.topics.contracts import ActiveRunConflict, ScheduledRunExists
from research_pulse.topics.daily_runner import ReadingBatchRunner
from research_pulse.topics.file_store import FileTopicStore
from research_pulse.topics.models import RunStatus, RunTrigger
from research_pulse.topics.service import TopicRunService


def _now():
    return datetime.now(UTC)


class FileTopicStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = FileTopicStore(self.root / ".topics")
        self.store.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _topic_run(self):
        svc = TopicRunService(self.store, runner=None)
        return svc.create_topic(name="Agent", query="LLM agent memory")

    def test_persists_and_reloads_across_store_instances(self) -> None:
        topic, run = self._topic_run()
        now = _now()
        done = self.store.save_run(run.start(started_at=now).finish(
            status=RunStatus.COMPLETED,
            candidate_count=1, published_count=1, failed_count=0,
            finished_at=now,
        ))

        reloaded = FileTopicStore(self.root / ".topics")
        reloaded.initialize()
        self.assertEqual([t.name for t, _ in reloaded.list_topics_with_latest_run()], ["Agent"])
        self.assertEqual(reloaded.get_run(run.run_id).status, RunStatus.COMPLETED)

    def test_atomic_write_leaves_no_temp_file(self) -> None:
        self._topic_run()
        self.assertTrue((self.root / ".topics" / "state.json").exists())
        self.assertFalse((self.root / ".topics" / "state.json.tmp").exists())

    def test_active_run_conflict_on_second_queued_run(self) -> None:
        topic, run = self._topic_run()  # first run is queued (active)
        svc = TopicRunService(self.store, runner=None)
        with self.assertRaises(ActiveRunConflict):
            svc.retry(topic.topic_id)

    def test_scheduled_run_for_same_slot_is_rejected(self) -> None:
        topic, run = self._topic_run()
        svc = TopicRunService(self.store, runner=None)
        # terminate the initial active run so scheduling is allowed
        now = _now()
        self.store.finish_run(
            run.start(started_at=now).finish(
                status=RunStatus.COMPLETED, candidate_count=0, published_count=0, failed_count=0, finished_at=now,
            ),
            discovery_succeeded=False, discovery_watermark=None,
        )
        slot = now + timedelta(seconds=1)
        svc.schedule(topic.topic_id, scheduled_for=slot, window_end=slot + timedelta(seconds=60))
        with self.assertRaises(ScheduledRunExists):
            svc.schedule(topic.topic_id, scheduled_for=slot, window_end=slot + timedelta(seconds=60))

    def test_terminal_run_finish_updates_watermark(self) -> None:
        topic, run = self._topic_run()
        running = run.start(started_at=_now())
        self.store.save_run(running)
        marked = running.finish(
            status=RunStatus.COMPLETED,
            candidate_count=0, published_count=0, failed_count=0,
            finished_at=_now(),
        )
        self.store.finish_run(marked, discovery_succeeded=True, discovery_watermark=_now())
        row = self.store.get_topic(topic.topic_id)
        self.assertIsNotNone(row.last_successful_discovery_at)

    def test_reconcile_marks_leftover_active_runs_as_failed(self) -> None:
        topic, run = self._topic_run()
        self.assertEqual(self.store.reconcile_active_runs(), 1)
        self.assertEqual(self.store.get_run(run.run_id).status, RunStatus.FAILED)
        self.assertEqual(self.store.get_run(run.run_id).error_code, "process_restarted")


class _FakeFinder:
    def __init__(self, candidates):
        self._candidates = candidates

    def discover(self, *, topic, domain, limit, window_start=None, window_end=None):
        return self._candidates


def _candidate(source_id, title):
    return PaperCandidate(source_id=source_id, title=title, source_url=f"https://arxiv.org/abs/{source_id}", domain="d")


class ReadingBatchRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_skips_already_published_source(self) -> None:
        pub_dir = self.root / "papers" / "p1"
        pub_dir.mkdir(parents=True)
        (pub_dir / "n.md").write_text("---\npublication_status: \"published\"\n---\n", encoding="utf-8")
        seen = []
        runner = ReadingBatchRunner(
            candidate_finder=_FakeFinder([_candidate("p1", "A"), _candidate("p2", "B")]),
            reader_service=type("R", (), {"process": lambda self, c: seen.append(c.source_id) or {"source_id": c.source_id, "status": "published"}})(),
            vault_root=self.root,
        )
        result = runner.run(run_id="r", topic="t", domain="d", limit=3, window_start=None, window_end=_now())
        statuses = [r["status"] for r in result["receipts"]]
        self.assertEqual(statuses, ["skipped_duplicate", "published"])
        self.assertEqual(seen, ["p2"])

    def test_reread_source_bypasses_dedupe(self) -> None:
        pub_dir = self.root / "papers" / "p1"
        pub_dir.mkdir(parents=True)
        (pub_dir / "n.md").write_text("---\n", encoding="utf-8")
        seen = []
        runner = ReadingBatchRunner(
            candidate_finder=_FakeFinder([_candidate("p1", "A")]),
            reader_service=type("R", (), {"process": lambda self, c: seen.append(c.source_id) or {"source_id": c.source_id, "status": "completed"}})(),
            vault_root=self.root,
            reread_sources={"p1"},
        )
        result = runner.run(run_id="r", topic="t", domain="d", limit=3, window_start=None, window_end=_now())
        self.assertEqual(result["receipts"][0]["status"], "completed")
        self.assertEqual(seen, ["p1"])

    def test_discovery_failure_marks_run_not_succeeded(self) -> None:
        class BoomFinder:
            def discover(self, **kwargs):
                raise RuntimeError("offline")

        runner = ReadingBatchRunner(candidate_finder=BoomFinder(), reader_service=None, vault_root=self.root)
        result = runner.run(run_id="r", topic="t", domain="d", limit=3, window_start=None, window_end=_now())
        self.assertFalse(result["discovery_succeeded"])


if __name__ == "__main__":
    unittest.main()
