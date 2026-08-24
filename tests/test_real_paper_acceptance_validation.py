from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import TestCase, skipUnless
import os
import uuid

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from research_pulse.acceptance.orchestration import CostBoundary, RejectingSupplementer
from research_pulse.acceptance.validation import (
    AcceptanceValidationError,
    build_acceptance_app,
    snapshot_knowledge,
    snapshot_raw_material,
    verify_new_bundle,
    verify_no_new_raw_material,
    verify_postgres_retrieval,
    verify_reading_api,
    verify_scoped_chat,
    verify_source_facts,
)
from research_pulse.knowledge.models import DurableEvidenceAnchor, KnowledgeAsset, KnowledgeBundle, KnowledgeClaim
from research_pulse.production.adapters import FilesystemKnowledgePublisher
from research_pulse.rag.chunking import chunk_bundle
from research_pulse.rag.contracts import EvidenceHit, IndexReceipt, SearchRequest
from research_pulse.rag.postgres import PostgresResearchRAG
from research_pulse.workflows.interactive import (
    CitationOnlyAnswerGenerator,
    InteractiveGraphDependencies,
    build_interactive_graph,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "2608.00001v1"
DATABASE_URL = os.getenv("RESEARCH_PULSE_TEST_DATABASE_URL")


class _Registry:
    def __init__(self) -> None:
        self.marked = []

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        self.marked.append((source_id, knowledge_id))


class _MemoryRag:
    def __init__(self) -> None:
        self.hits: list[EvidenceHit] = []

    def publish(self, bundle: KnowledgeBundle) -> IndexReceipt:
        chunks = chunk_bundle(bundle)
        self.hits = [
            EvidenceHit(
                chunk_id=chunk.chunk_id,
                knowledge_id=chunk.knowledge_id,
                knowledge_version=chunk.knowledge_version,
                title=bundle.asset.title,
                text=chunk.text,
                source_url=bundle.asset.source_urls[0],
                anchor_id=chunk.anchor_id,
                dense_score=None,
                keyword_score=0.2,
                fused_score=0.2,
                claim_id=chunk.claim_id,
                claim_type=chunk.claim_type,
                source_anchors=chunk.source_anchors,
            )
            for chunk in chunks
            if chunk.claim_type == "source_fact"
        ]
        return IndexReceipt(bundle.asset.knowledge_id, bundle.asset.knowledge_version, tuple(c.chunk_id for c in chunks))

    def search(self, request: SearchRequest):
        return [
            hit for hit in self.hits
            if (not request.knowledge_ids or hit.knowledge_id in request.knowledge_ids)
            and (request.domain is None or request.domain == "llm_agent_memory")
        ][: request.limit]


def _bundle(*, source_fact_count: int = 2, source_id: str = SOURCE_ID) -> KnowledgeBundle:
    source_url = f"https://arxiv.org/abs/{source_id}"
    body = "# A Real Paper\n\n## 方法\n\nAgent memory method.\n\n## 实验\n\nAccuracy improves by 10%.\n"
    asset = KnowledgeAsset(
        knowledge_id=f"kp:arxiv:{source_id}",
        knowledge_version="2026-08-22T08:00:00Z",
        publication_status="published",
        evidence_level="full_text_text",
        source_urls=(source_url,),
        domain="llm_agent_memory",
        title="A Real Paper",
        body=body,
        content_sha256=sha256(body.encode("utf-8")).hexdigest(),
    )
    excerpts = ("Agent memory method.", "Accuracy improves by 10%.")
    anchors = tuple(
        DurableEvidenceAnchor(
            anchor_id=f"source:{source_id}:{index}",
            source_url=source_url,
            evidence_excerpt=excerpt,
            excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
            section="Method" if index == 1 else "Results",
        )
        for index, excerpt in enumerate(excerpts, start=1)
    )
    claims = tuple(
        KnowledgeClaim(
            f"claim:{source_id}:{index}",
            "source_fact",
            excerpts[index - 1],
            (anchors[index - 1].anchor_id,),
        )
        for index in range(1, source_fact_count + 1)
    )
    used_anchor_count = max(source_fact_count, 1)
    return KnowledgeBundle(asset, claims, anchors[:used_anchor_count])


class AcceptanceBundleValidationTests(TestCase):
    def setUp(self) -> None:
        self.vault = ROOT / "data" / f"test-acceptance-vault-{uuid.uuid4().hex}"

    def tearDown(self) -> None:
        if self.vault.exists():
            for path in sorted(self.vault.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.vault.rmdir()

    def test_reloads_exactly_one_indexed_bundle_and_two_source_facts(self) -> None:
        rag = _MemoryRag()
        before = snapshot_knowledge(self.vault)
        FilesystemKnowledgePublisher(self.vault, rag, _Registry()).publish(_bundle(), SOURCE_ID)

        verification = verify_new_bundle(vault_root=self.vault, before=before, source_id=SOURCE_ID)
        retrieval = verify_postgres_retrieval(rag, verification)

        self.assertEqual(verification.manifest.index_status, "indexed")
        self.assertEqual(len(verification.source_fact_ids), 2)
        self.assertEqual(len(verification.source_anchor_ids), 2)
        self.assertGreaterEqual(len(retrieval.chunk_ids), 2)

        class _EmptyRag:
            def search(self, request):
                return []

        with self.assertRaisesRegex(AcceptanceValidationError, "did not return two"):
            verify_postgres_retrieval(_EmptyRag(), verification)

        class _MutatingRag:
            def __init__(self, *, version=None, chunk_id=None) -> None:
                self.version = version
                self.chunk_id = chunk_id

            def search(self, request):
                return [
                    replace(
                        hit,
                        knowledge_version=self.version or hit.knowledge_version,
                        chunk_id=self.chunk_id or hit.chunk_id,
                    )
                    for hit in rag.search(request)
                ]

        with self.assertRaisesRegex(AcceptanceValidationError, "identity or version"):
            verify_postgres_retrieval(_MutatingRag(version="old-version"), verification)
        with self.assertRaisesRegex(AcceptanceValidationError, "absent from the indexed manifest"):
            verify_postgres_retrieval(_MutatingRag(chunk_id="orphan-chunk"), verification)

    def test_rejects_no_new_wrong_source_partial_manifest_and_too_few_facts(self) -> None:
        with self.assertRaisesRegex(AcceptanceValidationError, "exactly one"):
            verify_new_bundle(vault_root=self.vault, before=frozenset(), source_id=SOURCE_ID)

        with self.assertRaisesRegex(AcceptanceValidationError, "at least 2"):
            verify_source_facts(_bundle(source_fact_count=1))

        rag = _MemoryRag()
        FilesystemKnowledgePublisher(self.vault, rag, _Registry()).publish(_bundle(), SOURCE_ID)
        with self.assertRaisesRegex(AcceptanceValidationError, "does not match"):
            verify_new_bundle(vault_root=self.vault, before=frozenset(), source_id="other")

        manifest = next(self.vault.rglob("*.manifest.json"))
        text = manifest.read_text(encoding="utf-8").replace('"index_status":"indexed"', '"index_status":"index_pending"')
        text = text.replace('"indexed_at":"', '"indexed_at":"')
        manifest.write_text(text, encoding="utf-8")
        with self.assertRaises(AcceptanceValidationError):
            verify_new_bundle(vault_root=self.vault, before=frozenset(), source_id=SOURCE_ID)

    def test_reading_api_and_scoped_chat_use_the_same_current_bundle(self) -> None:
        rag = _MemoryRag()
        FilesystemKnowledgePublisher(self.vault, rag, _Registry()).publish(_bundle(), SOURCE_ID)
        bundle = next(
            verify_new_bundle(vault_root=self.vault, before=frozenset(), source_id=SOURCE_ID).bundle
            for _ in [None]
        )
        supplementer = RejectingSupplementer()
        graph = build_interactive_graph(
            InteractiveGraphDependencies(rag, supplementer, CitationOnlyAnswerGenerator(), minimum_hits=2),
            checkpointer=MemorySaver(),
        )
        client = TestClient(build_acceptance_app(interactive_graph=graph, vault_root=self.vault))

        api = verify_reading_api(client, bundle)
        chat = verify_scoped_chat(
            client,
            query="论文的方法和实验结果是什么？",
            bundle=bundle,
            budget=CostBoundary(),
        )

        self.assertEqual((api.list_status, api.detail_status), (200, 200))
        self.assertEqual(len(chat.citation_ids), 2)
        self.assertEqual(set(chat.source_anchor_ids), {anchor.anchor_id for anchor in bundle.anchors})
        self.assertEqual(supplementer.calls, 0)

    def test_scoped_chat_rejects_interrupt_and_out_of_scope_citations(self) -> None:
        bundle = _bundle()
        empty_graph = build_interactive_graph(
            InteractiveGraphDependencies(_MemoryRag(), RejectingSupplementer(), CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        empty_client = TestClient(build_acceptance_app(interactive_graph=empty_graph, vault_root=self.vault))
        with self.assertRaisesRegex(AcceptanceValidationError, "insufficient"):
            verify_scoped_chat(
                empty_client,
                query="论文方法是什么？",
                bundle=bundle,
                budget=CostBoundary(),
            )

    def test_raw_material_inventory_reports_but_does_not_delete(self) -> None:
        root = self.vault / "managed"
        root.mkdir(parents=True)
        before = snapshot_raw_material([root])
        raw = root / "source.pdf"
        raw.write_bytes(b"%PDF-test")
        with self.assertRaisesRegex(AcceptanceValidationError, "root-0/source.pdf"):
            verify_no_new_raw_material(before=before, roots=[root])
        self.assertTrue(raw.exists())


@skipUnless(DATABASE_URL, "RESEARCH_PULSE_TEST_DATABASE_URL is not configured")
class AcceptancePostgresValidationTests(TestCase):
    source_id = "acceptance-postgres-test"
    knowledge_id = f"kp:arxiv:{source_id}"

    def setUp(self) -> None:
        assert DATABASE_URL is not None
        self.vault = ROOT / "data" / f"test-acceptance-postgres-vault-{uuid.uuid4().hex}"
        self.rag = PostgresResearchRAG(DATABASE_URL)
        self.rag.initialize()
        self._delete_rows()

    def tearDown(self) -> None:
        self._delete_rows()
        if self.vault.exists():
            for path in sorted(self.vault.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.vault.rmdir()

    def _delete_rows(self) -> None:
        with self.rag._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM knowledge_assets WHERE knowledge_id = %s", (self.knowledge_id,))

    def test_real_postgres_fts_returns_current_anchored_manifest_chunks(self) -> None:
        before = snapshot_knowledge(self.vault)
        FilesystemKnowledgePublisher(self.vault, self.rag, _Registry()).publish(
            _bundle(source_id=self.source_id), self.source_id
        )
        verification = verify_new_bundle(
            vault_root=self.vault,
            before=before,
            source_id=self.source_id,
        )

        retrieval = verify_postgres_retrieval(self.rag, verification)

        self.assertGreaterEqual(len(retrieval.hits), 2)
        self.assertTrue(all(hit.knowledge_version == verification.bundle.asset.knowledge_version for hit in retrieval.hits))
        self.assertTrue(all(hit.source_anchors for hit in retrieval.hits))
