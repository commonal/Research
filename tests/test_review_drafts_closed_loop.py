from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import shutil
from unittest import TestCase, skipUnless
from uuid import uuid4

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    EvidenceAnchor,
    KnowledgeAsset,
    KnowledgeBundle,
    KnowledgeClaim,
)
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.production.adapters import (
    FilesystemKnowledgePublisher,
    PostgresProcessedPaperRegistry,
)
from research_pulse.production.evidence import EvidenceCandidate, classify_candidate
from research_pulse.production.pipeline import ExtractedDraft, PaperCandidate, ProductionService, SourceMaterial
from research_pulse.rag.contracts import SearchRequest
from research_pulse.rag.postgres import PostgresResearchRAG
from research_pulse.production.reread import RereadProcessedPaperRegistry
from research_pulse.review_drafts import PostgresReviewDraftStore, ReviewDraft


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("RESEARCH_PULSE_TEST_DATABASE_URL")


class _Judge:
    def assess(self, *, claim: str, evidence: str) -> str:
        return "supported"


class _FixtureParser:
    def __init__(self, *, complete: bool) -> None:
        self.complete = complete

    def parse(self, candidate: PaperCandidate) -> SourceMaterial:
        facets = ("problem", "method", "experiment", "limitation") if self.complete else ("method",)
        anchors: dict[str, EvidenceAnchor] = {}
        fragments: dict[str, str] = {}
        blocks = {}
        for facet in facets:
            anchor_id = f"source:{candidate.source_id}:{facet}"
            text = {
                "problem": "The paper addresses a constrained research problem.",
                "method": "The method uses a verified pipeline.",
                "experiment": "The experiment reports 90% accuracy.",
                "limitation": "The limitation is synthetic evaluation.",
            }[facet]
            anchors[anchor_id] = EvidenceAnchor(anchor_id, candidate.source_url, section=facet.title())
            fragments[anchor_id] = text
            block = classify_candidate(
                EvidenceCandidate(
                    block_id=anchor_id,
                    kind="text",
                    text=text,
                    source_url=candidate.source_url,
                    parser="controlled-fixture",
                    parse_status="available",
                    locator_completeness="section_only",
                    section_path=(facet.title(),),
                )
            )
            blocks[anchor_id] = block
        return SourceMaterial(
            evidence_level="full_text_text",
            anchors=anchors,
            source_fragments=fragments,
            evidence_blocks=blocks,
        )


class _FixtureExtractor:
    def __init__(self, *, version: str, complete: bool) -> None:
        self.version = version
        self.complete = complete

    def extract(self, candidate: PaperCandidate, material: SourceMaterial) -> ExtractedDraft:
        facets = ("problem", "method", "experiment", "limitation") if self.complete else ("method",)
        body = (
            "# Controlled review paper\n\n"
            + ("A complete, source-backed reading note.\n" if self.complete else "A draft missing evidence facets.\n")
        )
        claims = tuple(
            KnowledgeClaim(
                claim_id=f"claim:{facet}",
                claim_type="source_fact",
                text={
                    "problem": "The paper addresses a constrained research problem.",
                    "method": "The method uses a verified pipeline.",
                    "experiment": "The experiment reports 90% accuracy.",
                    "limitation": "The limitation is synthetic evaluation.",
                }[facet],
                anchor_ids=(f"source:{candidate.source_id}:{facet}",),
                source_facet=facet,
            )
            for facet in facets
        )
        asset = KnowledgeAsset(
            knowledge_id=f"kp:arxiv:{candidate.source_id}",
            knowledge_version=self.version,
            publication_status="needs_review",
            evidence_level=material.evidence_level,
            source_urls=(candidate.source_url,),
            domain=candidate.domain,
            title=candidate.title,
            body=body,
            content_sha256=sha256(body.encode("utf-8")).hexdigest(),
        )
        return ExtractedDraft(asset=asset, claims=claims)


class _ApprovalService:
    def __init__(self, service: ProductionService, candidate: PaperCandidate) -> None:
        self.service = service
        self.candidate = candidate

    def approve(self, draft: ReviewDraft):
        return self.service.process(self.candidate)


def _candidate(source_id: str) -> PaperCandidate:
    return PaperCandidate(
        source_id=source_id,
        title="Controlled review paper",
        source_url=f"https://arxiv.org/abs/{source_id}",
        domain="controlled_acceptance",
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
    )


def _old_bundle(candidate: PaperCandidate) -> KnowledgeBundle:
    body = "# Controlled review paper\n\nOld published version.\n"
    asset = KnowledgeAsset(
        knowledge_id=f"kp:arxiv:{candidate.source_id}",
        knowledge_version="2026-08-21T00:00:00+00:00",
        publication_status="published",
        evidence_level="full_text_text",
        source_urls=(candidate.source_url,),
        domain=candidate.domain,
        title=candidate.title,
        body=body,
        content_sha256=sha256(body.encode("utf-8")).hexdigest(),
    )
    anchor_id = f"source:{candidate.source_id}:old"
    excerpt = "Old published version."
    return KnowledgeBundle(
        asset=asset,
        claims=(KnowledgeClaim("claim:old", "source_fact", excerpt, (anchor_id,)),),
        anchors=(
            # The publisher materializes durable anchors from transient source
            # data, so the seed bundle only needs a valid durable anchor here.
            DurableEvidenceAnchor(
                anchor_id,
                candidate.source_url,
                excerpt,
                sha256(excerpt.encode("utf-8")).hexdigest(),
                section="Overview",
            ),
        ),
    )


@skipUnless(DATABASE_URL, "RESEARCH_PULSE_TEST_DATABASE_URL is not configured")
class ReviewDraftClosedLoopAcceptanceTests(TestCase):
    def test_needs_review_preview_approve_publishes_new_current_and_only_new_rag_version(self) -> None:
        assert DATABASE_URL is not None
        source_id = f"closed-loop-{uuid4().hex}"
        candidate = _candidate(source_id)
        knowledge_id = f"kp:arxiv:{source_id}"
        vault = ROOT / "data" / f"test-review-closed-loop-{uuid4().hex}"
        rag = PostgresResearchRAG(DATABASE_URL)
        registry = PostgresProcessedPaperRegistry(DATABASE_URL)
        review_store = PostgresReviewDraftStore(database_url=DATABASE_URL, vault_root=vault)
        rag.initialize()
        registry.initialize()
        review_store.initialize()
        try:
            publisher = FilesystemKnowledgePublisher(vault, rag, registry)
            publisher.publish(_old_bundle(candidate), source_id)
            old_reader = FilesystemKnowledgeReader(vault)
            self.assertEqual(old_reader.get_current(knowledge_id).knowledge_version, "2026-08-21T00:00:00+00:00")

            reread_registry = RereadProcessedPaperRegistry(registry, target_source_id=source_id)
            review_receipt = ProductionService(
                parser=_FixtureParser(complete=False),
                extractor=_FixtureExtractor(version="2026-08-22T00:00:00+00:00", complete=False),
                publisher=publisher,
                processed_registry=reread_registry,
                entailment_judge=_Judge(),
                review_sink=review_store,
            ).process(candidate)
            self.assertEqual(review_receipt.status, "needs_review")
            drafts = review_store.list_for_knowledge(knowledge_id)
            self.assertEqual(len(drafts), 1)
            draft = drafts[0]
            self.assertEqual(draft.status, "needs_review")
            self.assertNotIn("missing evidence", old_reader.get_current(knowledge_id).markdown)
            self.assertEqual(
                [hit.knowledge_version for hit in rag.search(SearchRequest(query="old published", knowledge_ids=(knowledge_id,)))],
                ["2026-08-21T00:00:00+00:00"],
            )

            publish_service = ProductionService(
                parser=_FixtureParser(complete=True),
                extractor=_FixtureExtractor(version="2026-08-22T00:00:00+00:00", complete=True),
                publisher=publisher,
                processed_registry=reread_registry,
                entailment_judge=_Judge(),
            )
            client = TestClient(
                create_app(
                    interactive_graph=object(),
                    knowledge_reader=old_reader,
                    review_store=review_store,
                    review_approver=_ApprovalService(publish_service, candidate),
                )
            )
            preview = client.get(f"/api/knowledge/{knowledge_id}/review-drafts/{draft.draft_id}")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()["status"], "needs_review")
            approved = client.post(f"/api/knowledge/{knowledge_id}/review-drafts/{draft.draft_id}/approve")
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["draft"]["status"], "published")

            current = old_reader.get_current(knowledge_id)
            self.assertEqual(current.knowledge_version, "2026-08-22T00:00:00+00:00")
            versions = sorted(
                KnowledgeBundle.from_markdown(path).asset.knowledge_version
                for path in vault.glob("papers/**/*.md")
            )
            self.assertEqual(versions, ["2026-08-21T00:00:00+00:00", "2026-08-22T00:00:00+00:00"])
            hits = rag.search(SearchRequest(query="verified pipeline", knowledge_ids=(knowledge_id,)))
            self.assertTrue(hits)
            self.assertTrue(all(hit.knowledge_version == "2026-08-22T00:00:00+00:00" for hit in hits))
            self.assertEqual(review_store.list_for_knowledge(knowledge_id), ())

            rejected_receipt = ProductionService(
                parser=_FixtureParser(complete=False),
                extractor=_FixtureExtractor(version="2026-08-23T00:00:00+00:00", complete=False),
                publisher=publisher,
                processed_registry=reread_registry,
                entailment_judge=_Judge(),
                review_sink=review_store,
            ).process(candidate)
            self.assertEqual(rejected_receipt.status, "needs_review")
            rejected_draft = review_store.list_for_knowledge(knowledge_id)[0]
            rejected = client.post(f"/api/knowledge/{knowledge_id}/review-drafts/{rejected_draft.draft_id}/reject")
            self.assertEqual(rejected.status_code, 200)
            self.assertEqual(rejected.json()["status"], "rejected")
            self.assertEqual(old_reader.get_current(knowledge_id).knowledge_version, "2026-08-22T00:00:00+00:00")
            rejected_hits = rag.search(SearchRequest(query="verified pipeline", knowledge_ids=(knowledge_id,)))
            self.assertTrue(all(hit.knowledge_version == "2026-08-22T00:00:00+00:00" for hit in rejected_hits))
        finally:
            with rag._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM knowledge_assets WHERE knowledge_id = %s", (knowledge_id,))
                    cursor.execute("DELETE FROM processed_papers WHERE source_id = %s", (source_id,))
                    cursor.execute("DELETE FROM review_drafts WHERE knowledge_id = %s", (knowledge_id,))
            shutil.rmtree(vault, ignore_errors=True)
