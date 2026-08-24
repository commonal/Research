from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import sleep
from unittest import TestCase

from langgraph.checkpoint.memory import MemorySaver

from research_pulse.knowledge.models import EvidenceAnchor, KnowledgeAsset, KnowledgeBundle, KnowledgeClaim
from research_pulse.production.pipeline import (
    ExtractedDraft,
    PaperCandidate,
    ProductionService,
    SourceMaterial,
)
from research_pulse.production.evidence import EvidenceCandidate, classify_candidate
from research_pulse.workflows.production import ProductionGraphDependencies, build_production_graph


ROOT = Path(__file__).resolve().parents[1]


class _Finder:
    def __init__(self, candidates: list[PaperCandidate], error: Exception | None = None) -> None:
        self.candidates = candidates
        self.error = error

    def discover(self, *, topic: str, domain: str, limit: int, window_start=None, window_end=None) -> list[PaperCandidate]:
        if self.error:
            raise self.error
        return self.candidates[:limit]


class _Registry:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.marked: list[tuple[str, str]] = []

    def was_processed(self, source_id: str) -> bool:
        return source_id in self.existing

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        self.marked.append((source_id, knowledge_id))


class _Parser:
    def parse(self, candidate: PaperCandidate) -> SourceMaterial:
        anchor = EvidenceAnchor("anchor:test", candidate.source_url, section="Results")
        return SourceMaterial(
            evidence_level="full_text_text",
            anchors={anchor.anchor_id: anchor},
            source_fragments={anchor.anchor_id: "The method reaches 90% accuracy."},
        )


class _FailingParser(_Parser):
    def parse(self, candidate: PaperCandidate) -> SourceMaterial:
        if candidate.source_id == "bad":
            raise RuntimeError("broken PDF with secret")
        return super().parse(candidate)


class _Extractor:
    def extract(self, candidate: PaperCandidate, material: SourceMaterial) -> ExtractedDraft:
        sample = KnowledgeAsset.from_markdown(ROOT / "knowledge/fixtures/2026-08-22-validated-agent-memory.md")
        asset = replace(
            sample,
            knowledge_id=f"kp:arxiv:{candidate.source_id}",
            knowledge_version="2026-08-22T00:00:00Z",
            publication_status="needs_review",
            source_urls=(candidate.source_url,),
            domain=candidate.domain,
            title=candidate.title,
        )
        return ExtractedDraft(
            asset=asset,
            claims=(KnowledgeClaim("claim:test", "source_fact", "The method reaches 90% accuracy.", ("anchor:test",)),),
        )


class _Judge:
    def assess(self, *, claim: str, evidence: str) -> str:
        return "supported"


class _Publisher:
    def __init__(self) -> None:
        self.published: list[KnowledgeBundle] = []
        self.source_ids: list[str] = []

    def publish(self, bundle: KnowledgeBundle, source_id: str) -> None:
        self.published.append(bundle)
        self.source_ids.append(source_id)


def _candidate(source_id: str) -> PaperCandidate:
    return PaperCandidate(
        source_id,
        f"Paper {source_id}",
        f"https://arxiv.org/abs/{source_id}",
        "llm_agent_memory",
        datetime(2026, 8, 22, tzinfo=UTC),
    )


class ProductionPipelineTests(TestCase):
    def test_run_deadline_aborts_before_publish(self) -> None:
        class SlowParser(_Parser):
            def parse(self, candidate: PaperCandidate) -> SourceMaterial:
                sleep(0.03)
                return super().parse(candidate)

        publisher = _Publisher()
        registry = _Registry()
        service = ProductionService(
            SlowParser(), _Extractor(), publisher, registry, _Judge(), run_deadline_seconds=0.01
        )

        receipt = service.process(_candidate("deadline"))

        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.reason, "reading_timeout")
        self.assertEqual(publisher.published, [])
        self.assertEqual(registry.marked, [])

    def test_graph_publishes_new_candidates_and_skips_duplicates_without_source_in_state(self) -> None:
        publisher = _Publisher()
        registry = _Registry(existing={"duplicate"})
        service = ProductionService(_Parser(), _Extractor(), publisher, registry, _Judge())
        graph = build_production_graph(
            ProductionGraphDependencies(_Finder([_candidate("duplicate"), _candidate("new")]), service),
            checkpointer=MemorySaver(),
        )

        result = graph.invoke(
            {"run_id": "run:1", "topic": "agent memory", "domain": "llm_agent_memory", "limit": 3},
            {"configurable": {"thread_id": "production:1"}},
        )

        self.assertEqual([receipt["status"] for receipt in result["receipts"]], ["skipped_duplicate", "published"])
        self.assertEqual(len(publisher.published), 1)
        self.assertEqual(publisher.published[0].asset.publication_status, "published")
        self.assertEqual(publisher.source_ids, ["new"])
        self.assertNotIn("source_fragments", result)
        self.assertNotIn("_candidates", result)
        self.assertTrue(result["discovery_succeeded"])

    def test_missing_independent_judge_requires_review_not_auto_publish(self) -> None:
        publisher = _Publisher()
        registry = _Registry()
        service = ProductionService(_Parser(), _Extractor(), publisher, registry)

        receipt = service.process(_candidate("review"))

        self.assertEqual(receipt.status, "needs_review")
        self.assertIn("entailment_judge_missing", receipt.reason or "")
        self.assertEqual(publisher.published, [])

    def test_missing_enhanced_facets_needs_review_without_publishing_or_processing(self) -> None:
        candidate = _candidate("incomplete-evidence")
        anchor = EvidenceAnchor("anchor:method", candidate.source_url, section="Method")
        fragment = "The method uses a version-aware memory index. private parser text must not leave the receipt."
        method_block = classify_candidate(
            EvidenceCandidate(
                block_id=anchor.anchor_id,
                kind="text",
                text=fragment,
                source_url=candidate.source_url,
                parser="fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=("Method",),
            )
        )

        class IncompleteParser:
            def parse(self, ignored: PaperCandidate) -> SourceMaterial:
                return SourceMaterial(
                    evidence_level="full_text_text",
                    anchors={anchor.anchor_id: anchor},
                    source_fragments={anchor.anchor_id: fragment},
                    evidence_blocks={anchor.anchor_id: method_block},
                )

        class IncompleteExtractor(_Extractor):
            def extract(self, ignored: PaperCandidate, material: SourceMaterial) -> ExtractedDraft:
                draft = super().extract(ignored, material)
                return replace(
                    draft,
                    claims=(
                        KnowledgeClaim(
                            "claim:method",
                            "source_fact",
                            fragment,
                            (anchor.anchor_id,),
                            "method",
                        ),
                    ),
                )

        class ReviewSink:
            def __init__(self) -> None:
                self.calls = []

            def save_review_draft(self, **kwargs):
                self.calls.append(kwargs)

        publisher = _Publisher()
        review_sink = ReviewSink()
        registry = _Registry()
        receipt = ProductionService(
            IncompleteParser(), IncompleteExtractor(), publisher, registry, _Judge(), review_sink=review_sink
        ).process(candidate)

        self.assertEqual(receipt.status, "needs_review")
        self.assertIn("missing_evidence_facet", receipt.reason or "")
        self.assertNotIn("private parser text", receipt.reason or "")
        self.assertEqual(publisher.published, [])
        self.assertEqual(registry.marked, [])
        self.assertEqual(len(review_sink.calls), 1)
        self.assertEqual(review_sink.calls[0]["candidate"].source_id, candidate.source_id)

    def test_zero_candidates_and_single_failure_keep_graph_state_bounded(self) -> None:
        publisher = _Publisher()
        service = ProductionService(_FailingParser(), _Extractor(), publisher, _Registry(), _Judge())
        empty_graph = build_production_graph(
            ProductionGraphDependencies(_Finder([]), service),
            checkpointer=MemorySaver(),
        )
        empty = empty_graph.invoke(
            {"run_id": "run:empty", "topic": "memory", "domain": "memory", "limit": 3},
            {"configurable": {"thread_id": "production:empty"}},
        )
        self.assertTrue(empty["discovery_succeeded"])
        self.assertEqual(empty["candidate_ids"], [])
        self.assertEqual(empty["receipts"], [])

        graph = build_production_graph(
            ProductionGraphDependencies(_Finder([_candidate("bad"), _candidate("good")]), service),
            checkpointer=MemorySaver(),
        )
        result = graph.invoke(
            {"run_id": "run:mixed", "topic": "memory", "domain": "memory", "limit": 3},
            {"configurable": {"thread_id": "production:mixed"}},
        )
        self.assertEqual([receipt["status"] for receipt in result["receipts"]], ["failed", "published"])
        self.assertNotIn("source_fragments", result)
        self.assertNotIn("abstract", result)
        self.assertNotIn("pdf", result)

    def test_discovery_failure_does_not_report_success(self) -> None:
        service = ProductionService(_Parser(), _Extractor(), _Publisher(), _Registry(), _Judge())
        graph = build_production_graph(
            ProductionGraphDependencies(_Finder([], RuntimeError("arxiv unavailable")), service),
            checkpointer=MemorySaver(),
        )

        with self.assertRaisesRegex(RuntimeError, "arxiv unavailable"):
            graph.invoke(
                {"run_id": "run:failed", "topic": "memory", "domain": "memory", "limit": 3},
                {"configurable": {"thread_id": "production:failed"}},
            )