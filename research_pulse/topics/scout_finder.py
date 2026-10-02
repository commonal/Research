"""Candidate source backed by the scout agent's daily submissions.

The scout flow
(``research_pulse.scout``: RSS cs.LG/cs.IR/cs.AI -> keyword recall + already-read
+ dislikes -> DeepSeek per-paper judgment -> deterministic Top-K) writes its
ranked candidates to ``scout/submissions/<yyyy-mm-dd>.jsonl``.  This module is
the bridge into the note-only reading chain: it reads that file and exposes the
rows as ``production.PaperCandidate`` through the same ``discover`` seam the
rest of ``ReadingBatchRunner`` already consumes.

Run order when a new day has no submissions yet:
  1. invoke ``scout_runner`` (if supplied) to produce today's ``<date>.jsonl``,
  2. read the resulting file and map rows to candidates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence
import json

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.topics.models import MAX_INITIAL_RUN_LIMIT


class ScoutSubmissionFinder:
    """Yield ranked DOI/arXiv candidates from the scout daily submissions feed."""

    def __init__(
        self,
        submissions_dir: Path,
        *,
        date: str | None = None,
        scout_runner: Callable[[], int] | None = None,
    ) -> None:
        self.submissions_dir = Path(submissions_dir)
        # Keep an explicit date stable for tests/replays; production resolves
        # today's date on every tick so a long-running API does not stay pinned
        # to the day on which it started.
        self.date = date
        self.scout_runner = scout_runner

    def discover(
        self,
        *,
        topic: str,
        domain: str,
        limit: int,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[PaperCandidate]:
        rows = self._load_rows()
        ranked = sorted(rows, key=lambda row: (row.get("priority", 1_000_000), row.get("scout_score", 0.0)))
        capped = ranked[: min(limit, MAX_INITIAL_RUN_LIMIT) if limit else 3]
        candidates = []
        for row in capped:
            source_id = str(row.get("source_id", "")).strip()
            if not source_id:
                continue
            if window_start is not None:
                published = _parse_published(row.get("published_at"))
                if published is None or published <= window_start:
                    continue
            if window_end is not None:
                published = _parse_published(row.get("published_at"))
                if published is not None and published > window_end:
                    continue
            candidates.append(
                PaperCandidate(
                    source_id=source_id,
                    title=str(row.get("title", "")).strip() or source_id,
                    source_url=_abs_url(source_id),
                    domain=domain,
                    published_at=_parse_published(row.get("published_at")),
                )
            )
        return candidates

    def _load_rows(self) -> list[dict[str, Any]]:
        date = self.date or datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.submissions_dir / f"{date}.jsonl"
        if not path.exists() and self.scout_runner is not None:
            self.scout_runner()
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


def _parse_published(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _abs_url(source_id: str) -> str:
    return f"https://arxiv.org/abs/{source_id}"
