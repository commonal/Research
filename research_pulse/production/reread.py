"""Explicit historical-paper re-reading entrypoint.

Re-reading is intentionally separate from daily discovery.  It resolves one
currently published arXiv asset, bypasses deduplication only for that source,
and then reuses the normal parser, deep reader, quality gate, and versioned
publisher.  A successful run creates a new knowledge version; it never edits
the existing Markdown in place.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from research_pulse.knowledge.reader import FilesystemKnowledgeReader, KnowledgeReader
from research_pulse.production.adapters import (
    DEFAULT_DEEPSEEK_TEXT_MODEL,
    DeepSeekEntailmentJudge,
    DeepSeekStructuredExtractor,
    DoclingSourceParser,
    FilesystemKnowledgePublisher,
    PostgresProcessedPaperRegistry,
)
from research_pulse.production.pipeline import PaperCandidate, ProcessedPaperRegistry, ProductionService
from research_pulse.rag.postgres import PostgresResearchRAG
from research_pulse.review_drafts import PostgresReviewDraftStore


class RereadError(ValueError):
    """Raised when an existing knowledge asset cannot be re-read safely."""


class RereadProcessedPaperRegistry:
    """Bypass deduplication only for the explicitly selected source."""

    def __init__(self, delegate: ProcessedPaperRegistry, *, target_source_id: str) -> None:
        self.delegate = delegate
        self.target_source_id = target_source_id

    def was_processed(self, source_id: str) -> bool:
        if source_id == self.target_source_id:
            return False
        return self.delegate.was_processed(source_id)

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        self.delegate.mark_processed(source_id, knowledge_id)


def candidate_from_knowledge_id(knowledge_id: str, reader: KnowledgeReader) -> PaperCandidate:
    """Resolve one current arXiv asset into the normal production candidate."""

    prefix = "kp:arxiv:"
    if not knowledge_id.startswith(prefix) or not knowledge_id.removeprefix(prefix).strip():
        raise RereadError("Re-reading currently supports only arXiv knowledge IDs (kp:arxiv:...).")
    detail = reader.get_current(knowledge_id)
    if detail is None:
        raise RereadError(f"Knowledge asset {knowledge_id} was not found in the canonical vault.")
    if not detail.source_urls:
        raise RereadError(f"Knowledge asset {knowledge_id} has no source URL.")
    try:
        published_at = datetime.fromisoformat(detail.knowledge_version.replace("Z", "+00:00"))
    except ValueError:
        published_at = None
    return PaperCandidate(
        source_id=knowledge_id.removeprefix(prefix),
        title=detail.title,
        source_url=detail.source_urls[0],
        domain=detail.domain,
        published_at=published_at,
    )


def run_reread(
    *,
    knowledge_id: str,
    database_url: str,
    vault_root: Path,
    model: str,
    with_formulas: bool = False,
) -> object:
    """Run one historical re-read through the standard production service."""

    reader = FilesystemKnowledgeReader(vault_root)
    candidate = candidate_from_knowledge_id(knowledge_id, reader)
    rag = PostgresResearchRAG(database_url)
    delegate = PostgresProcessedPaperRegistry(database_url)
    rag.initialize()
    delegate.initialize()
    reread_registry = RereadProcessedPaperRegistry(delegate, target_source_id=candidate.source_id)
    review_sink = PostgresReviewDraftStore(database_url=database_url, vault_root=vault_root)
    review_sink.initialize()
    service = ProductionService(
        parser=DoclingSourceParser(with_formulas=with_formulas),
        extractor=DeepSeekStructuredExtractor.from_environment(model=model),
        publisher=FilesystemKnowledgePublisher(vault_root=vault_root, rag=rag, processed_registry=delegate),
        processed_registry=reread_registry,
        entailment_judge=DeepSeekEntailmentJudge.from_environment(model=model),
        run_deadline_seconds=float(os.getenv("DEEPSEEK_RUN_DEADLINE_SECONDS", "420")),
        review_sink=review_sink,
    )
    return service.process(candidate)


def main(argv: Sequence[str] | None = None) -> int:
    project_root = _project_root()
    load_dotenv(project_root / ".env", override=False)
    parser = argparse.ArgumentParser(description="Re-read one existing Research Pulse arXiv knowledge asset.")
    parser.add_argument("--knowledge-id", required=True, help="Existing ID, for example kp:arxiv:2608.18351v1")
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_TEXT_MODEL))
    parser.add_argument("--with-formulas", action="store_true", help="Enable Docling formula enrichment.")
    args = parser.parse_args(argv)

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required; copy the local development value from .env.example.")
    try:
        receipt = run_reread(
            knowledge_id=args.knowledge_id,
            database_url=database_url,
            vault_root=_vault_root(project_root),
            model=args.model,
            with_formulas=args.with_formulas,
        )
    except RereadError as error:
        parser.error(str(error))
    print(json.dumps(asdict(receipt), ensure_ascii=False))
    return 0 if getattr(receipt, "status", None) == "published" else 1


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _vault_root(project_root: Path) -> Path:
    configured = os.getenv("RESEARCH_PULSE_VAULT_ROOT", "knowledge").strip()
    path = Path(configured)
    return path if path.is_absolute() else project_root / path


if __name__ == "__main__":
    raise SystemExit(main())
