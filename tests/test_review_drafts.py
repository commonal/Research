from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import unittest
from unittest import TestCase
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from research_pulse.api.app import create_app
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.production.pipeline import CandidateReceipt
from research_pulse.review_drafts import (
    FilesystemReviewDraftStore,
    PostgresReviewDraftStore,
    ReviewDraft,
    ReviewDraftError,
)
from research_pulse.scheduling import SchedulerStatus
from research_pulse.workflows.interactive import (
    CitationOnlyAnswerGenerator,
    InteractiveGraphDependencies,
    SupplementationResult,
    build_interactive_graph,
)


ROOT = Path(__file__).resolve().parents[1]


def _draft(status: str = "needs_review") -> ReviewDraft:
    markdown = "# Review draft\n\n## 核心直觉\n\n仅供人工审核。\n"
    return ReviewDraft(
        draft_id="review:kp-arxiv-test:one",
        source_id="test-paper",
        knowledge_id="kp:arxiv:test-paper",
        knowledge_version="2026-08-22T00:00:00+00:00",
        title="Review Paper",
        domain="test",
        source_urls=("https://arxiv.org/abs/test-paper",),
        status=status,  # type: ignore[arg-type]
        markdown=markdown,
        content_sha256=sha256(markdown.encode("utf-8")).hexdigest(),
        quality_issues=(),
        evidence_boundary="尚未通过自动质量门禁。",
        created_at="2026-08-22T00:00:00+00:00",
        updated_at="2026-08-22T00:00:00+00:00",
    )


class _MemoryReviewStore:
    def __init__(self, draft: ReviewDraft) -> None:
        self.draft = draft

    def save(self, draft: ReviewDraft) -> ReviewDraft:
        self.draft = draft
        return draft

    def save_review_draft(self, **kwargs):
        return self.draft

    def get(self, draft_id: str) -> ReviewDraft | None:
        return self.draft if self.draft.draft_id == draft_id else None

    def list_for_knowledge(self, knowledge_id: str, *, include_closed: bool = False):
        if self.draft.knowledge_id != knowledge_id or (not include_closed and self.draft.status != "needs_review"):
            return ()
        return (self.draft,)

    def transition(self, draft_id: str, status: str) -> ReviewDraft:
        if self.get(draft_id) is None:
            raise ReviewDraftError("review draft not found")
        self.draft = self.draft.transition(status)  # type: ignore[arg-type]
        return self.draft


class _Approver:
    def __init__(self, receipt: CandidateReceipt) -> None:
        self.receipt = receipt

    def approve(self, draft: ReviewDraft) -> CandidateReceipt:
        return self.receipt


class _Rag:
    def search(self, request):
        return []


class _Supplementer:
    def supplement(self, *, query: str, domain: str | None, reason: str) -> SupplementationResult:
        return SupplementationResult("review-test", "failed", ())


class ReviewDraftTests(TestCase):
    def test_review_statuses_are_one_way_from_needs_review(self) -> None:
        for status in ("rejected", "published", "expired"):
            transitioned = _draft().transition(status)  # type: ignore[arg-type]
            self.assertEqual(transitioned.status, status)
            with self.assertRaisesRegex(ReviewDraftError, "needs_review"):
                transitioned.transition("published")

    def test_filesystem_store_is_separate_from_published_reader_and_transitions_once(self) -> None:
        vault = ROOT / "data" / f"test-review-drafts-{uuid4().hex}"
        try:
            store = FilesystemReviewDraftStore(vault)
            draft = _draft()
            store.save(draft)

            metadata_files = list((vault / "review-drafts").rglob("*.json"))
            self.assertEqual(len(metadata_files), 1)
            metadata_text = metadata_files[0].read_text(encoding="utf-8")
            for forbidden in ("pdf", "fulltext", "prompt", "provider_response"):
                self.assertNotIn(forbidden, metadata_text.lower())

            loaded = store.get(draft.draft_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.markdown, draft.markdown)
            self.assertEqual(store.list_for_knowledge(draft.knowledge_id)[0].status, "needs_review")
            self.assertEqual(FilesystemKnowledgeReader(vault).recent(limit=10), [])

            rejected = store.transition(draft.draft_id, "rejected")
            self.assertEqual(rejected.status, "rejected")
            self.assertEqual(store.list_for_knowledge(draft.knowledge_id), ())
            with self.assertRaisesRegex(ReviewDraftError, "needs_review"):
                store.transition(draft.draft_id, "published")
        finally:
            import shutil

            shutil.rmtree(vault, ignore_errors=True)

    def test_api_lists_previews_and_approval_publishes_only_after_approver(self) -> None:
        draft = _draft()
        store = _MemoryReviewStore(draft)
        graph = build_interactive_graph(
            InteractiveGraphDependencies(_Rag(), _Supplementer(), CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        client = TestClient(
            create_app(
                interactive_graph=graph,
                knowledge_reader=FilesystemKnowledgeReader(ROOT / "knowledge"),
                review_store=store,
                review_approver=_Approver(CandidateReceipt("test-paper", "published", draft.knowledge_id)),
            )
        )

        listing = client.get(f"/api/knowledge/{draft.knowledge_id}/review-drafts")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["items"][0]["status"], "needs_review")
        detail = client.get(f"/api/knowledge/{draft.knowledge_id}/review-drafts/{draft.draft_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("仅供人工审核", detail.json()["markdown"])

        approved = client.post(f"/api/knowledge/{draft.knowledge_id}/review-drafts/{draft.draft_id}/approve")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["draft"]["status"], "published")

        rejected = client.post(f"/api/knowledge/{draft.knowledge_id}/review-drafts/{draft.draft_id}/reject")
        self.assertEqual(rejected.status_code, 409)

    def test_api_rejects_approval_when_quality_still_blocks(self) -> None:
        draft = _draft()
        store = _MemoryReviewStore(draft)
        graph = build_interactive_graph(
            InteractiveGraphDependencies(_Rag(), _Supplementer(), CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        client = TestClient(
            create_app(
                interactive_graph=graph,
                knowledge_reader=FilesystemKnowledgeReader(ROOT / "knowledge"),
                review_store=store,
                review_approver=_Approver(CandidateReceipt("test-paper", "needs_review", draft.knowledge_id, "missing_facet")),
            )
        )

        response = client.post(f"/api/knowledge/{draft.knowledge_id}/review-drafts/{draft.draft_id}/approve")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "quality_gate_blocked")
        self.assertEqual(store.draft.status, "needs_review")

    @unittest.skipUnless(os.getenv("RESEARCH_PULSE_TEST_DATABASE_URL"), "Postgres integration URL is not configured")
    def test_postgres_store_indexes_isolated_markdown_and_transitions(self) -> None:
        vault = ROOT / "data" / f"test-review-drafts-pg-{uuid4().hex}"
        draft = _draft()
        store = PostgresReviewDraftStore(
            database_url=os.environ["RESEARCH_PULSE_TEST_DATABASE_URL"],
            vault_root=vault,
        )
        try:
            store.initialize()
            store.save(draft)
            loaded = store.get(draft.draft_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.markdown, draft.markdown)
            self.assertEqual(store.transition(draft.draft_id, "published").status, "published")
        finally:
            import shutil

            try:
                import psycopg

                with psycopg.connect(os.environ["RESEARCH_PULSE_TEST_DATABASE_URL"]) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("DELETE FROM review_drafts WHERE draft_id = %s", (draft.draft_id,))
            except Exception:
                pass
            shutil.rmtree(vault, ignore_errors=True)
