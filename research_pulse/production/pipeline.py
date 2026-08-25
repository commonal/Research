"""Framework-free production service used by the background LangGraph nodes.

Every adapter has a narrow responsibility.  In particular, parsing material is
kept in this call stack and never returned to the graph checkpoint.

LEGACY / NOT ON THE NOTE-ONLY MAIN CHAIN
----------------------------------------
``ProductionService`` drove the retired ``DeepSeekStructuredExtractor`` bundle
pipeline (~DraftExtractor -> validate_draft -> KnowledgeBundle).  The reader
route does NOT use it: ``reader_production.ReaderProductionService`` is the
source of truth for note-only production.  This module is retained for tests
and any future bundle/claims re-introduction; do not wire it into new entry
points.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence

from research_pulse.knowledge.models import EvidenceAnchor, KnowledgeAsset, KnowledgeBundle, KnowledgeClaim
from research_pulse.production.evidence import EvidenceBlock, EvidenceCandidate, EvidenceFacet
from research_pulse.production.deadline import ProductionDeadlineExceeded, production_deadline
from research_pulse.production.quality import EntailmentJudge, QualityGateResult, validate_draft


@dataclass(frozen=True)
class PaperCandidate:
    """Minimal paper identity needed by the durable production path."""

    source_id: str
    title: str
    source_url: str
    domain: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class SourceMaterial:
    """Transient parser output, keyed by stable evidence anchors."""

    evidence_level: Literal["abstract_only", "full_text_text", "full_text_multimodal"]
    anchors: Mapping[str, EvidenceAnchor]
    source_fragments: Mapping[str, str]
    evidence_candidates: Mapping[str, EvidenceCandidate] = field(default_factory=dict)
    evidence_blocks: Mapping[str, EvidenceBlock] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedDraft:
    """A model-proposed note before publication status is granted."""

    asset: KnowledgeAsset
    claims: tuple[KnowledgeClaim, ...]
    claim_facets: Mapping[str, EvidenceFacet] = field(default_factory=dict)
    visual_assets: Mapping[str, Path] = field(default_factory=dict)


class SourceParser(Protocol):
    def parse(self, candidate: PaperCandidate) -> SourceMaterial: ...


class DraftExtractor(Protocol):
    def extract(self, candidate: PaperCandidate, material: SourceMaterial) -> ExtractedDraft: ...


class KnowledgePublisher(Protocol):
    def publish(self, bundle: KnowledgeBundle, source_id: str) -> object: ...


class ReviewDraftSink(Protocol):
    def save_review_draft(
        self,
        *,
        candidate: PaperCandidate,
        draft: ExtractedDraft,
        quality: QualityGateResult,
    ) -> object: ...


class ProcessedPaperRegistry(Protocol):
    def was_processed(self, source_id: str) -> bool: ...

    def mark_processed(self, source_id: str, knowledge_id: str) -> None: ...


@dataclass(frozen=True)
class CandidateReceipt:
    source_id: str
    status: Literal["published", "skipped_duplicate", "needs_review", "failed"]
    knowledge_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ProductionService:
    """LEGACY — NOT on the note-only main chain.

    Part of the retired DeepSeekStructuredExtractor bundle pipeline.  New
    production entry points use ``reader_production.ReaderProductionService``.
    Retained for tests and future claims/bundle work.
    """

    parser: SourceParser
    extractor: DraftExtractor
    publisher: KnowledgePublisher
    processed_registry: ProcessedPaperRegistry
    entailment_judge: EntailmentJudge | None = None
    run_deadline_seconds: float | None = 420.0
    review_sink: ReviewDraftSink | None = None

    def process(self, candidate: PaperCandidate) -> CandidateReceipt:
        """Process exactly one candidate without leaking its raw source outward."""

        if self.processed_registry.was_processed(candidate.source_id):
            return CandidateReceipt(source_id=candidate.source_id, status="skipped_duplicate")
        try:
            with production_deadline(self.run_deadline_seconds) as deadline:
                if deadline:
                    deadline.ensure_remaining()
                material = self.parser.parse(candidate)
                if deadline:
                    deadline.ensure_remaining()
                draft = self.extractor.extract(candidate, material)
                if deadline:
                    deadline.ensure_remaining()
                quality = validate_draft(
                    asset=draft.asset,
                    claims=draft.claims,
                    anchors=material.anchors,
                    source_fragments=material.source_fragments,
                    evidence_blocks=material.evidence_blocks or None,
                    visual_assets=draft.visual_assets,
                    entailment_judge=self.entailment_judge,
                )
                if deadline:
                    deadline.ensure_remaining()
                return self._publish_if_allowed(candidate, draft, quality)
        except ProductionDeadlineExceeded:
            return CandidateReceipt(source_id=candidate.source_id, status="failed", reason="reading_timeout")
        except Exception as error:  # One bad PDF or provider response must not stop a batch.
            return CandidateReceipt(source_id=candidate.source_id, status="failed", reason=_safe_reason(error))

    def _publish_if_allowed(
        self,
        candidate: PaperCandidate,
        draft: ExtractedDraft,
        quality: QualityGateResult,
    ) -> CandidateReceipt:
        if not quality.approved:
            self._save_review_draft(candidate, draft, quality)
            return CandidateReceipt(
                source_id=candidate.source_id,
                status="needs_review",
                knowledge_id=draft.asset.knowledge_id,
                reason=_issue_codes(quality),
            )
        if quality.requires_human_review:
            self._save_review_draft(candidate, draft, quality)
            return CandidateReceipt(
                source_id=candidate.source_id,
                status="needs_review",
                knowledge_id=draft.asset.knowledge_id,
                reason=_issue_codes(quality),
            )
        if quality.bundle is None:
            return CandidateReceipt(candidate.source_id, "failed", draft.asset.knowledge_id, "missing_knowledge_bundle")
        published = replace(
            quality.bundle,
            asset=replace(quality.bundle.asset, publication_status="published"),
        )
        self.publisher.publish(published, candidate.source_id)
        return CandidateReceipt(
            source_id=candidate.source_id,
            status="published",
            knowledge_id=published.asset.knowledge_id,
        )

    def _save_review_draft(
        self,
        candidate: PaperCandidate,
        draft: ExtractedDraft,
        quality: QualityGateResult,
    ) -> None:
        if self.review_sink is None:
            return
        try:
            self.review_sink.save_review_draft(candidate=candidate, draft=draft, quality=quality)
        except Exception:
            # A review persistence failure must never turn a safe non-publish
            # result into a published result or expose provider details.
            return


def _issue_codes(quality: QualityGateResult) -> str:
    return ",".join(issue.code for issue in quality.issues)


def _safe_reason(error: Exception) -> str:
    """Keep batch receipts concise; provider secrets/body text must not escape."""

    message = str(error).replace("\n", " ").strip()
    return message[:300] or type(error).__name__
