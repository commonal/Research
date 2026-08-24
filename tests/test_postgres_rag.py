from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
from unittest import TestCase, skipUnless

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    KnowledgeAsset,
    KnowledgeAssetError,
    KnowledgeBundle,
    KnowledgeClaim,
    render_provenance,
)
from research_pulse.rag.contracts import SearchRequest
from research_pulse.rag.postgres import PostgresResearchRAG, expand_search_query


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("RESEARCH_PULSE_TEST_DATABASE_URL")


class SearchQueryExpansionTests(TestCase):
    def test_expand_search_query_bridges_common_chinese_research_terms(self) -> None:
        expanded = expand_search_query("这篇论文的方法和实验结果有什么局限？")

        self.assertIn("method", expanded)
        self.assertIn("experiment", expanded)
        self.assertIn("result", expanded)
        self.assertIn("limitation", expanded)
        self.assertNotIn("这篇论文", expanded)
        self.assertEqual(expand_search_query("LLM Agent"), "LLM Agent")


@skipUnless(DATABASE_URL, "RESEARCH_PULSE_TEST_DATABASE_URL is not configured")
class PostgresResearchRAGTests(TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        self.rag = PostgresResearchRAG(DATABASE_URL)
        self.rag.initialize()
        source = KnowledgeAsset.from_markdown(
            ROOT / "knowledge" / "fixtures" / "2026-08-22-validated-agent-memory.md"
        )
        self.asset_v1 = replace(
            source,
            knowledge_id="kp:test:postgres-fts",
            knowledge_version="2026-08-22T00:00:00Z",
            domain="test_domain",
        )
        self.bundle_v1 = _persisted_bundle(self.asset_v1)

    def tearDown(self) -> None:
        with self.rag._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM knowledge_assets WHERE knowledge_id = %(knowledge_id)s",
                    {"knowledge_id": self.asset_v1.knowledge_id},
                )

    def test_publish_and_search_returns_only_the_current_version(self) -> None:
        asset_v2 = replace(self.asset_v1, knowledge_version="2026-08-23T00:00:00Z")
        bundle_v2 = _persisted_bundle(asset_v2)

        first_receipt = self.rag.publish(self.bundle_v1)
        duplicate_receipt = self.rag.publish(self.bundle_v1)
        self.rag.publish(bundle_v2)
        hits = self.rag.search(SearchRequest(query="LLM Agent", domain="test_domain"))
        scoped_hits = self.rag.search(
            SearchRequest(
                query="LLM Agent",
                domain="test_domain",
                knowledge_ids=(self.asset_v1.knowledge_id,),
            )
        )
        excluded_hits = self.rag.search(
            SearchRequest(query="LLM Agent", knowledge_ids=("kp:test:other",))
        )

        self.assertTrue(hits)
        self.assertEqual([hit.chunk_id for hit in scoped_hits], [hit.chunk_id for hit in hits])
        self.assertEqual(excluded_hits, [])
        self.assertTrue(all(hit.knowledge_version == asset_v2.knowledge_version for hit in hits))
        self.assertTrue(all(hit.anchor_id and hit.source_url for hit in hits))
        self.assertTrue(all(hit.claim_id == "claim:test:fact" for hit in hits))
        self.assertTrue(all(hit.claim_type == "source_fact" for hit in hits))
        self.assertTrue(all(hit.source_anchors[0].anchor_id == "source:test:overview:1" for hit in hits))
        self.assertTrue(all(hit.source_anchors[0].parse_status == "available" for hit in hits))
        self.assertEqual(first_receipt, duplicate_receipt)
        with self.rag._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM knowledge_chunks WHERE knowledge_id = %s AND knowledge_version = %s",
                    (self.asset_v1.knowledge_id, self.asset_v1.knowledge_version),
                )
                self.assertEqual(cursor.fetchone()[0], len(first_receipt.chunk_ids))

    def test_domain_filter_prevents_cross_domain_retrieval(self) -> None:
        self.rag.publish(self.bundle_v1)

        hits = self.rag.search(SearchRequest(query="LLM Agent", domain="other_domain"))

        self.assertEqual(hits, [])

    def test_scoped_fallback_serves_selected_paper_when_lexical_search_misses(self) -> None:
        bundle = _two_fact_bundle(self.asset_v1)
        self.rag.publish(bundle)

        scoped_hits = self.rag.search(
            SearchRequest(
                query="这篇论文的方法和实验结果是什么",
                domain="test_domain",
                knowledge_ids=(self.asset_v1.knowledge_id,),
            )
        )
        unscoped_hits = self.rag.search(
            SearchRequest(query="这篇论文的方法和实验结果是什么", domain="test_domain")
        )

        self.assertEqual(len(scoped_hits), 2)
        self.assertTrue(all(hit.knowledge_id == self.asset_v1.knowledge_id for hit in scoped_hits))
        self.assertTrue(all(hit.source_anchors for hit in scoped_hits))
        self.assertEqual(len(unscoped_hits), 2)
        self.assertTrue(all(hit.knowledge_id == self.asset_v1.knowledge_id for hit in unscoped_hits))

    def test_projection_does_not_index_agent_inferences(self) -> None:
        self.rag.publish(_bundle_with_inference(self.asset_v1))

        hits = self.rag.search(
            SearchRequest(
                query="LLM Agent",
                domain="test_domain",
            )
        )

        self.assertEqual([hit.claim_type for hit in hits], ["source_fact"])

    def test_initialize_is_idempotent_and_legacy_publish_is_rejected(self) -> None:
        self.rag.initialize()
        legacy = KnowledgeBundle(
            asset=self.asset_v1,
            claims=(),
            anchors=(),
            provenance_status="legacy_missing_provenance",
        )

        with self.assertRaisesRegex(KnowledgeAssetError, "Legacy"):
            self.rag.publish(legacy)
        damaged = replace(
            self.bundle_v1,
            asset=replace(self.bundle_v1.asset, provenance_sha256="f" * 64),
        )
        with self.assertRaisesRegex(KnowledgeAssetError, "mismatched provenance hash"):
            self.rag.publish(damaged)


def _persisted_bundle(asset: KnowledgeAsset) -> KnowledgeBundle:
    excerpt = "LLM Agent memory reaches 90% accuracy."
    anchor = DurableEvidenceAnchor(
        anchor_id="source:test:overview:1",
        source_url=asset.source_urls[0],
        evidence_excerpt=excerpt,
        excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
        section="Overview",
        block_kind="text",
        parse_status="available",
        locator_completeness="section_only",
    )
    persisted_asset = replace(
        asset,
        schema_version=2,
        provenance_file="test.provenance.json",
        provenance_sha256="0" * 64,
    )
    provisional = KnowledgeBundle(
        persisted_asset,
        (KnowledgeClaim("claim:test:fact", "source_fact", excerpt, (anchor.anchor_id,)),),
        (anchor,),
    )
    provenance_hash = sha256(render_provenance(provisional).encode("utf-8")).hexdigest()
    return replace(provisional, asset=replace(persisted_asset, provenance_sha256=provenance_hash))


def _two_fact_bundle(asset: KnowledgeAsset) -> KnowledgeBundle:
    excerpts = (
        ("source:test:overview:1", "The method reaches 90% accuracy.", "Overview"),
        ("source:test:results:1", "The experiment compares two baselines.", "Results"),
    )
    anchors = tuple(
        DurableEvidenceAnchor(
            anchor_id=anchor_id,
            source_url=asset.source_urls[0],
            evidence_excerpt=excerpt,
            excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
            section=section,
            block_kind="text",
            parse_status="available",
            locator_completeness="section_only",
        )
        for anchor_id, excerpt, section in excerpts
    )
    persisted_asset = replace(
        asset,
        schema_version=2,
        provenance_file="test.provenance.json",
        provenance_sha256="0" * 64,
    )
    claims = tuple(
        KnowledgeClaim(f"claim:test:fact:{index}", "source_fact", anchor.evidence_excerpt, (anchor.anchor_id,))
        for index, anchor in enumerate(anchors, start=1)
    )
    provisional = KnowledgeBundle(persisted_asset, claims, anchors)
    provenance_hash = sha256(render_provenance(provisional).encode("utf-8")).hexdigest()
    return replace(provisional, asset=replace(persisted_asset, provenance_sha256=provenance_hash))


def _bundle_with_inference(asset: KnowledgeAsset) -> KnowledgeBundle:
    bundle = _persisted_bundle(asset)
    provisional = replace(
        bundle,
        claims=(*bundle.claims, KnowledgeClaim("claim:test:inference", "agent_inference", "The method may generalize.", ())),
    )
    provenance_hash = sha256(render_provenance(provisional).encode("utf-8")).hexdigest()
    return replace(provisional, asset=replace(provisional.asset, provenance_sha256=provenance_hash))
