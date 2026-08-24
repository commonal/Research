from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import TestCase

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    KnowledgeAsset,
    KnowledgeBundle,
    KnowledgeClaim,
)
from research_pulse.rag.chunking import chunk_bundle


ROOT = Path(__file__).resolve().parents[1]


def _bundle() -> KnowledgeBundle:
    asset = KnowledgeAsset.from_markdown(ROOT / "knowledge/fixtures/2026-08-22-validated-agent-memory.md")
    excerpt = "The method reaches 90% accuracy."
    anchor = DurableEvidenceAnchor(
        "source:test:results:1",
        asset.source_urls[0],
        excerpt,
        sha256(excerpt.encode()).hexdigest(),
        section="Results",
    )
    return KnowledgeBundle(
        asset=asset,
        claims=(
            KnowledgeClaim("claim:fact", "source_fact", excerpt, (anchor.anchor_id,)),
            KnowledgeClaim("claim:inference", "agent_inference", "This may generalize.", ()),
            KnowledgeClaim("claim:question", "reading_question", "Does it replicate?", ()),
        ),
        anchors=(anchor,),
    )


class ChunkingTests(TestCase):
    def test_claim_chunking_indexes_only_approved_source_facts(self) -> None:
        chunks = chunk_bundle(_bundle())

        self.assertEqual([chunk.claim_id for chunk in chunks], ["claim:fact"])
        self.assertEqual([chunk.claim_type for chunk in chunks], ["source_fact"])
        self.assertEqual(chunks[0].source_anchors[0].section, "Results")
        self.assertTrue(all(chunk.anchor_id.startswith("anchor:") for chunk in chunks))

    def test_legacy_bundle_is_not_answer_eligible(self) -> None:
        asset = _bundle().asset
        legacy = KnowledgeBundle(
            asset=replace(asset, schema_version=1),
            claims=(),
            anchors=(),
            provenance_status="legacy_missing_provenance",
        )

        with self.assertRaisesRegex(ValueError, "Legacy"):
            chunk_bundle(legacy)

    def test_degraded_source_fact_is_not_projected_to_retrieval(self) -> None:
        bundle = _bundle()
        degraded = replace(bundle.anchors[0], parse_status="degraded")
        unsafe = replace(bundle, anchors=(degraded,))

        with self.assertRaisesRegex(ValueError, "degraded or unparsed"):
            chunk_bundle(unsafe)
