"""Note-only daily reading runner with per-source de-duplication.

Sequence per topic: discover candidates -> for each, skip any ``source_id`` that
already has a published note under ``knowledge/papers/<source_id>/`` (unless an
explicit reread is requested) -> run the untouched ones through
``ReaderProductionService.process``.  Skipped duplicates surface as
``status: "skipped_duplicate"`` so ``TopicRunService`` counts them as neither
published nor failed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from research_pulse.production.adapters import ArxivCandidateFinder
from research_pulse.topics.models import MAX_INITIAL_RUN_LIMIT
from research_pulse.topics.service import ProductionBatchRunner


class ReadingBatchRunner:
    """Drive one topic batch through the note-only reader route (dedupe on).

    ``vault_root`` is used for de-duplication: a candidate whose
    ``knowledge/papers/<source_id>/`` directory already holds a published note
    is reported as ``skipped_duplicate`` instead of being read again.  Pass an
    explicit ``reread_sources`` (a set of ``source_id``) to force re-reading.

    When a ``server_parser`` is supplied, each not-yet-published candidate is
    first pushed through the server parse loop (PDF -> remote MinerU+Docling ->
    normalized pulled back locally) via ``server_parser.ensure_normalized``
    before the reader consumes it.  A parse failure surfaces as a ``failed``
    receipt rather than aborting the whole batch.
    """

    def __init__(
        self,
        candidate_finder: Any = None,
        reader_service: Any = None,
        vault_root: Path | None = None,
        reread_sources: set[str] | None = None,
        server_parser: Any = None,
    ) -> None:
        self.candidate_finder = candidate_finder or ArxivCandidateFinder()
        self.reader_service = reader_service
        self.vault_root = vault_root
        self.reread_sources = reread_sources or set()
        self.server_parser = server_parser

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
        try:
            candidates = self.candidate_finder.discover(
                topic=topic,
                domain=domain,
                limit=min(limit, MAX_INITIAL_RUN_LIMIT),
                window_start=window_start,
                window_end=window_end,
            )
        except Exception:
            return {"candidate_ids": [], "receipts": [], "discovery_succeeded": False}

        receipts: list[dict[str, Any]] = []
        for candidate in candidates:
            if self._is_published(candidate.source_id):
                receipts.append(
                    {
                        "source_id": candidate.source_id,
                        "status": "skipped_duplicate",
                        "receipt_status": "skipped_duplicate",
                    }
                )
                continue
            try:
                if self.server_parser is not None:
                    self.server_parser.ensure_normalized(candidate)
                receipts.append(self.reader_service.process(candidate))
            except Exception:
                receipts.append(
                    {
                        "source_id": candidate.source_id,
                        "status": "failed",
                        "receipt_status": "failed",
                    }
                )
        return {
            "candidate_ids": [candidate.source_id for candidate in candidates],
            "receipts": receipts,
            "discovery_succeeded": True,
        }

    def _is_published(self, source_id: str) -> bool:
        if self.vault_root is None:
            return False
        if source_id in self.reread_sources:
            return False
        directory = self.vault_root / "papers" / source_id
        return directory.is_dir() and any(directory.glob("*.md"))
