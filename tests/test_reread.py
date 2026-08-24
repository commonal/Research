from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from unittest import TestCase
import shutil
import uuid

from research_pulse.knowledge.models import KnowledgeAsset
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.production.adapters import render_asset_markdown
from research_pulse.production.reread import (
    RereadError,
    RereadProcessedPaperRegistry,
    candidate_from_knowledge_id,
)


class _Registry:
    def __init__(self) -> None:
        self.marked: list[tuple[str, str]] = []

    def was_processed(self, source_id: str) -> bool:
        return True

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        self.marked.append((source_id, knowledge_id))


class RereadTests(TestCase):
    def test_candidate_is_resolved_from_current_arxiv_asset(self) -> None:
        vault = Path(__file__).resolve().parents[1] / "data" / f"test-reread-{uuid.uuid4().hex}"
        vault.mkdir(parents=True)
        try:
            body = "# Existing note\n\n## 方法\n\nOld short note.\n"
            asset = KnowledgeAsset(
                knowledge_id="kp:arxiv:2608.18351v1",
                knowledge_version="2026-08-22T10:37:31.084511+00:00",
                publication_status="published",
                evidence_level="full_text_text",
                source_urls=("https://arxiv.org/abs/2608.18351v1",),
                domain="llm_agent_memory",
                title="Task-Conditioned Least-Privilege Learning",
                body=body,
                content_sha256=sha256(body.encode("utf-8")).hexdigest(),
            )
            path = vault / "papers" / "2608.18351v1" / "asset.md"
            path.parent.mkdir(parents=True)
            path.write_text(render_asset_markdown(asset), encoding="utf-8")

            candidate = candidate_from_knowledge_id(
                "kp:arxiv:2608.18351v1", FilesystemKnowledgeReader(vault)
            )

            self.assertEqual(candidate.source_id, "2608.18351v1")
            self.assertEqual(candidate.source_url, "https://arxiv.org/abs/2608.18351v1")
            self.assertEqual(candidate.domain, "llm_agent_memory")
            self.assertEqual(candidate.title, "Task-Conditioned Least-Privilege Learning")
        finally:
            shutil.rmtree(vault, ignore_errors=False)

    def test_reread_rejects_missing_or_non_arxiv_assets(self) -> None:
        vault = Path(__file__).resolve().parents[1] / "data" / f"test-reread-{uuid.uuid4().hex}"
        vault.mkdir(parents=True)
        try:
            reader = FilesystemKnowledgeReader(vault)
            with self.assertRaisesRegex(RereadError, "supports only arXiv knowledge IDs"):
                candidate_from_knowledge_id("kp:website:paper", reader)
            with self.assertRaisesRegex(RereadError, "was not found"):
                candidate_from_knowledge_id("kp:arxiv:missing", reader)
        finally:
            shutil.rmtree(vault, ignore_errors=False)

    def test_reread_registry_bypasses_only_the_explicit_target(self) -> None:
        delegate = _Registry()
        registry = RereadProcessedPaperRegistry(delegate, target_source_id="2608.18351v1")

        self.assertFalse(registry.was_processed("2608.18351v1"))
        self.assertTrue(registry.was_processed("another-paper"))
        registry.mark_processed("2608.18351v1", "kp:arxiv:2608.18351v1")

        self.assertEqual(delegate.marked, [("2608.18351v1", "kp:arxiv:2608.18351v1")])
