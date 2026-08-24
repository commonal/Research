from __future__ import annotations

"""Production-facing reading route (Reading freeze milestone).

This is the Reading module's single seam into the three-layer architecture
(Acquisition / Reading / Knowledge Consumption):

    MaterialResolver(normalized_root, source_id) -> CanonicalPaperIR
        -> PaperReader.read(candidate, ir, intent) -> ReadingResult(draft, receipt)
        -> note (draft.markdown) -> minimal schema-v1 asset -> publish;
           receipt.status -> published / needs_review / failed (superseded later).

B1 scope (option A): the reading route consumes the server-produced normalized
blocks from a single ``normalized_root`` directory (``<root>/<source_id>/normalized/blocks.jsonl``);
it does NOT parse PDFs.  The Acquisition -> server material production -> sync
leg is a separate operational seam (documented in docs/architecture-consolidation.md).
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
import re

from research_pulse.knowledge.models import KnowledgeAsset
from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import ExtractedDraft, PaperCandidate
from research_pulse.production.reading import (
    CanonicalPaperIR,
    DeepSeekPaperReadingModel,
    PaperReader,
    ReadingIntent,
    ReadingResult,
)


@dataclass(frozen=True)
class ReaderConfig:
    normalized_root: Path
    language: str = "zh-CN"
    depth: str = "deep"
    image_subpath: str = "mineru/source/auto"


class ReaderMaterialResolver:
    """Resolve a candidate's server-normalized blocks into a CanonicalPaperIR."""

    def __init__(self, normalized_root: Path, image_subpath: str = "mineru/source/auto") -> None:
        self.normalized_root = normalized_root
        self.image_subpath = image_subpath

    def resolve(self, candidate: PaperCandidate) -> CanonicalPaperIR:
        blocks_path = self.normalized_root / candidate.source_id / "normalized" / "blocks.jsonl"
        blocks = load_normalized_jsonl(blocks_path)
        paper = CanonicalPaperIR.from_normalized(
            source_id=candidate.source_id,
            title=candidate.title,
            source_url=candidate.source_url,
            blocks=blocks,
        )
        paper = replace(paper, blocks=tuple(
            replace(block, image_path=str(resolved))
            if block.image_path and (resolved := self._resolve_image(candidate.source_id, block.image_path)) else block
            for block in paper.blocks
        ))
        return paper

    def _resolve_image(self, source_id: str, value: str) -> Path | None:
        for candidate in (
            self.normalized_root / source_id / value,
            self.normalized_root / source_id / self.image_subpath / value,
        ):
            if candidate.is_file():
                return candidate.resolve()
        return None


class ReaderProduction:
    """Read one paper through PaperReader and expose the note + status."""

    def __init__(self, config: ReaderConfig, model: Any | None = None) -> None:
        self.config = config
        self.model = model or DeepSeekPaperReadingModel.from_environment()
        self.resolver = ReaderMaterialResolver(config.normalized_root, config.image_subpath)

    def read(self, candidate: PaperCandidate) -> ReadingResult:
        paper = self.resolver.resolve(candidate)
        # PaperReader now defaults its writer/planner to the model's own, so no
        # lambdas are required.
        reader = PaperReader(self.model)
        return reader.read(
            candidate,
            paper,
            ReadingIntent(language=self.config.language, depth=self.config.depth),
        )

    @staticmethod
    def note_asset_markdown(note: str, candidate: PaperCandidate, receipt: Any) -> str:
        """Minimal schema-v1 asset: the reader's note is the knowledge body."""
        version = getattr(receipt, "completed_at", None) or getattr(receipt, "started_at", None) or "manual"
        status = getattr(receipt, "status", "failed")
        pub = "published" if status not in {"failed", "needs_review"} else "needs_review"
        return f"""---
knowledge_id: "kp:arxiv:{candidate.source_id}"
knowledge_version: "{version}"
publication_status: "{pub}"
evidence_level: "full_text_text"
source_urls: ["{candidate.source_url}"]
domain: "{candidate.domain}"
title: "{candidate.title}"
schema_version: 1
---
{note.strip()}
"""

    def produce(self, candidate: PaperCandidate) -> dict[str, Any]:
        """Run the reading route and return the note asset + status (no persistence)."""
        result = self.read(candidate)
        markdown = self.note_asset_markdown(result.draft.markdown, candidate, result.receipt)
        return {
            "receipt_status": result.receipt.status,
            "stop_reason": result.receipt.stop_reason,
            "note_chars": len(result.draft.markdown.strip()),
            "asset_markdown": markdown,
        }


class ReaderExtractor:
    """DraftExtractor adapter that routes a paper through PaperReader.

    Implements the production extractor seam (``extract(candidate, material) ->
    ExtractedDraft``) so ``ProductionService`` can be built with the reading
    route instead of the DeepSeek deep-reader.  ``material`` is ignored: the
    reading route resolves the server-normalized blocks from ``normalized_root``.
    The note is carried as a schema-v1 asset with empty claims (option A: the
    note is the knowledge; claims/provenance are a later boundary change).
    """

    def __init__(self, config: ReaderConfig, model: Any | None = None) -> None:
        self.reader = ReaderProduction(config, model)

    def extract(self, candidate: PaperCandidate, material: Any) -> ExtractedDraft:
        result = self.reader.read(candidate)
        note = result.draft.markdown
        receipt = result.receipt
        pub = "published" if receipt.status not in {"failed", "needs_review"} else "needs_review"
        version = getattr(receipt, "completed_at", None) or getattr(receipt, "started_at", None) or "manual"
        asset = KnowledgeAsset(
            knowledge_id=f"kp:arxiv:{candidate.source_id}",
            knowledge_version=version,
            publication_status=pub,
            evidence_level="full_text_text",
            source_urls=(candidate.source_url,),
            domain=candidate.domain,
            title=candidate.title,
            body=note,
            content_sha256=sha256(note.encode("utf-8")).hexdigest(),
            schema_version=1,
        )
        return ExtractedDraft(asset=asset, claims=())


@dataclass(frozen=True)
class ReaderVaultConfig:
    vault_root: Path


def _safe_version(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)


class ReaderNotePublisher:
    """Note-only publish path (option A1): write the schema-v1 markdown directly.

    Does NOT build a KnowledgeBundle/claims/anchors provenance.  The note is the
    knowledge; the retrieval/claims layer adapts later.  Mirrors the existing
    ``knowledge/papers/<source_id>/<version>.md`` location so downstream readers
    can find it the same way.
    """

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root

    def publish(self, candidate: PaperCandidate, note: str, receipt: Any) -> Path:
        markdown = ReaderProduction.note_asset_markdown(note, candidate, receipt)
        version = getattr(receipt, "completed_at", None) or getattr(receipt, "started_at", None) or "manual"
        directory = self.vault_root / "papers" / candidate.source_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_version(version)}.md"
        path.write_text(markdown, encoding="utf-8")
        return path


class ReaderProductionService:
    """Reading route as a drop-in production entry: resolve -> read -> publish."""

    def __init__(self, config: ReaderConfig, vault_root: Path, model: Any | None = None) -> None:
        self.reader = ReaderProduction(config, model)
        self.publisher = ReaderNotePublisher(vault_root)

    def process(self, candidate: PaperCandidate) -> dict[str, Any]:
        result = self.reader.read(candidate)
        path = self.publisher.publish(candidate, result.draft.markdown, result.receipt)
        return {
            "source_id": candidate.source_id,
            "receipt_status": result.receipt.status,
            "stop_reason": result.receipt.stop_reason,
            "note_chars": len(result.draft.markdown.strip()),
            "published_path": str(path),
        }
