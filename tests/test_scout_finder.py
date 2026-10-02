"""ScoutSubmissionFinder tests: scout submissions feed -> PaperCandidate."""

from __future__ import annotations

import tempfile
import unittest
import json
from datetime import UTC, datetime
from pathlib import Path

from research_pulse.topics.scout_finder import ScoutSubmissionFinder


def _row(source_id, priority, score=2.0, published="2026-08-25T00:00:00Z"):
    return {
        "source": "arxiv",
        "source_id": source_id,
        "title": f"Paper {source_id}",
        "authors": ["A"],
        "abstract": "abs",
        "published_at": published,
        "priority": priority,
        "scout_score": score,
    }


class ScoutSubmissionFinderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, date: str, rows) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / f"{date}.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_reads_and_maps_rows_in_priority_order(self) -> None:
        self._write("2026-08-25", [
            _row("2608.qqq", priority=2, score=2.4),
            _row("2608.aaa", priority=1, score=2.8),
            _row("2608.zzz", priority=3, score=2.0),
        ])
        cands = ScoutSubmissionFinder(self.dir, date="2026-08-25").discover(
            topic="t", domain="d", limit=3
        )
        self.assertEqual([c.source_id for c in cands], ["2608.aaa", "2608.qqq", "2608.zzz"])
        self.assertEqual(cands[0].domain, "d")
        self.assertEqual(cands[0].source_url, "https://arxiv.org/abs/2608.aaa")
        self.assertEqual(cands[0].published_at, datetime(2026, 8, 25, tzinfo=UTC))

    def test_limit_caps_emitted_candidates(self) -> None:
        self._write("2026-08-25", [_row(f"2608.{i}", priority=i) for i in (1, 2, 3, 4)])
        cands = ScoutSubmissionFinder(self.dir, date="2026-08-25").discover(topic="t", domain="d", limit=2)
        self.assertEqual(len(cands), 2)

    def test_missing_file_without_runner_returns_empty(self) -> None:
        cands = ScoutSubmissionFinder(self.dir, date="2099-01-01").discover(topic="t", domain="d", limit=3)
        self.assertEqual(cands, [])

    def test_scout_runner_invoked_when_submissions_missing(self) -> None:
        called = {"n": 0}

        def runner():
            called["n"] += 1
            self._write("2026-08-25", [_row("2608.aaa", priority=1)])
            return 0

        cands = ScoutSubmissionFinder(self.dir, date="2026-08-25", scout_runner=runner).discover(
            topic="t", domain="d", limit=3
        )
        self.assertEqual(called["n"], 1)
        self.assertEqual([c.source_id for c in cands], ["2608.aaa"])

    def test_window_filter_skips_old_papers(self) -> None:
        self._write("2026-08-25", [
            _row("2608.old", priority=1, published="2026-08-23T00:00:00Z"),
            _row("2608.new", priority=2, published="2026-08-25T00:00:00Z"),
        ])
        start = datetime(2026, 8, 24, tzinfo=UTC)
        cands = ScoutSubmissionFinder(self.dir, date="2026-08-25").discover(
            topic="t", domain="d", limit=3, window_start=start
        )
        self.assertEqual([c.source_id for c in cands], ["2608.new"])


if __name__ == "__main__":
    unittest.main()
