from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from research_pulse.knowledge.models import KnowledgeBundle
from research_pulse.rag.chunking import chunk_bundle
from research_pulse.rag.contracts import EvidenceHit
from research_pulse.workflows.interactive import CitationOnlyAnswerGenerator


ROOT = Path(__file__).resolve().parents[1]


class EvidenceFixtureAcceptanceTests(TestCase):
    def test_v2_fixture_reaches_a_source_fact_only_answer(self) -> None:
        bundle = KnowledgeBundle.from_markdown(
            ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.md"
        )
        chunks = chunk_bundle(bundle)
        hits = [
            EvidenceHit(
                chunk_id=chunk.chunk_id,
                knowledge_id=chunk.knowledge_id,
                knowledge_version=chunk.knowledge_version,
                title=bundle.asset.title,
                text=chunk.text,
                source_url=bundle.asset.source_urls[0],
                anchor_id=chunk.anchor_id,
                dense_score=None,
                keyword_score=1.0,
                fused_score=1.0,
                claim_id=chunk.claim_id,
                claim_type=chunk.claim_type,
                source_anchors=chunk.source_anchors,
            )
            for chunk in chunks
        ]

        answer = CitationOnlyAnswerGenerator().generate(query="What is known?", evidence=hits)

        self.assertEqual([hit.claim_type for hit in hits], ["source_fact"])
        self.assertEqual(hits[0].source_anchors[0].anchor_id, "source:fixture:results:1")
        self.assertIn("【论文事实】", answer)
        self.assertNotIn("【系统推断】", answer)
        self.assertNotIn("Does it replicate?", answer)
        self.assertIn(hits[0].anchor_id, answer)
        self.assertIn("source:fixture:results:1", answer)
