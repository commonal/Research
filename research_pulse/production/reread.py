"""Explicit historical-paper re-reading entrypoint.

Re-reading is intentionally separate from daily discovery.  It resolves one
currently published arXiv asset, bypasses deduplication only for that source,
and then runs the PaperReader reading route (note-only publish) to create a
new knowledge version.  A successful run creates a new version; it never edits
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
from research_pulse.production.pipeline import PaperCandidate, ProcessedPaperRegistry
from research_pulse.reader_production import ReaderConfig, ReaderProductionService


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
    vault_root: Path,
    normalized_root: Path,
    model: str | None = None,
) -> dict[str, str | int | None]:
    """Run one historical re-read through the PaperReader reading route."""

    reader = FilesystemKnowledgeReader(vault_root)
    candidate = candidate_from_knowledge_id(knowledge_id, reader)
    service = ReaderProductionService(ReaderConfig(normalized_root=normalized_root), vault_root, model=model)
    return service.process(candidate)


def main(argv: Sequence[str] | None = None) -> int:
    project_root = _project_root()
    load_dotenv(project_root / ".env", override=False)
    parser = argparse.ArgumentParser(description="Re-read one existing Research Pulse arXiv knowledge asset.")
    parser.add_argument("--knowledge-id", required=True, help="Existing ID, for example kp:arxiv:2608.18351v1")
    parser.add_argument(
        "--normalized-root",
        type=Path,
        required=True,
        help="Root containing <source_id>/normalized/blocks.jsonl for server-produced blocks.",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root for note-only publish (defaults to ./knowledge under the project root).",
    )
    args = parser.parse_args(argv)

    try:
        receipt = run_reread(
            knowledge_id=args.knowledge_id,
            vault_root=args.vault_root or _vault_root(project_root),
            normalized_root=args.normalized_root,
        )
    except RereadError as error:
        parser.error(str(error))
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt.get("receipt_status", "failed") == "published" else 1


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _vault_root(project_root: Path) -> Path:
    configured = os.getenv("RESEARCH_PULSE_VAULT_ROOT", "knowledge").strip()
    path = Path(configured)
    return path if path.is_absolute() else project_root / path


if __name__ == "__main__":
    raise SystemExit(main())
