from __future__ import annotations

from hashlib import sha256
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import json
import os
import tempfile

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    KnowledgeAsset,
    KnowledgeBundle,
    KnowledgeClaim,
    ReadingSectionEvidence,
    ReadingVisualEvidence,
    split_front_matter,
)
from research_pulse.production.adapters import (
    ArxivCandidateFinder,
    DeepSeekEntailmentJudge,
    DoclingSourceParser,
    FilesystemKnowledgePublisher,
    MAX_JUDGE_OUTPUT_TOKENS,
    NON_THINKING_MODE,
    ProviderTimeout,
    _post_json,
    _response_json,
    source_blocks_to_material,
)
from research_pulse.production.pipeline import ExtractedDraft, PaperCandidate, SourceMaterial
from research_pulse.production.evidence import EvidenceCandidate, classify_candidate
from research_pulse.rag.chunking import chunk_bundle
from research_pulse.rag.contracts import IndexReceipt
from worker.discover import PaperCandidate as WorkerCandidate
from worker.fulltext import ParsedDocumentBlock, ParsedFullText


ROOT = Path(__file__).resolve().parents[1]


def _candidate() -> PaperCandidate:
    return PaperCandidate(
        "2606.10677v1",
        "A Test Paper",
        "https://arxiv.org/abs/2606.10677v1",
        "llm_agent_memory",
        datetime(2026, 6, 1, tzinfo=UTC),
    )


class _Rag:
    def __init__(self) -> None:
        self.published: list[KnowledgeBundle] = []

    def publish(self, bundle: KnowledgeBundle) -> IndexReceipt:
        self.published.append(bundle)
        return IndexReceipt(
            bundle.asset.knowledge_id,
            bundle.asset.knowledge_version,
            tuple(chunk.chunk_id for chunk in chunk_bundle(bundle)),
        )

    def search(self, request):
        return []


class _Registry:
    def __init__(self) -> None:
        self.marked: list[tuple[str, str]] = []

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        if (source_id, knowledge_id) not in self.marked:
            self.marked.append((source_id, knowledge_id))


class _FakePath:
    suffix = ".md"

    def __init__(self) -> None:
        self.parent = self
        self.markdown = ""
        self.replaced = False

    def mkdir(self, **kwargs) -> None:
        return None

    def with_suffix(self, suffix: str):
        return self

    def write_text(self, value: str, **kwargs) -> None:
        self.markdown = value

    def replace(self, target) -> None:
        self.replaced = True


class ProductionAdapterTests(TestCase):
    def test_arxiv_finder_adapts_existing_worker_candidates(self) -> None:
        captured = []

        def fetcher(subscription):
            captured.append(subscription)
            return [
                WorkerCandidate("arxiv", "one", "First", [], "2026-08-22T10:00:00Z", "2026-08-22T10:00:00Z", "", "https://example.com/one", []),
                WorkerCandidate("arxiv", "two", "Second", [], "2026-08-22T09:00:00Z", "2026-08-22T09:00:00Z", "", "https://example.com/two", []),
            ]

        candidates = ArxivCandidateFinder(fetcher=fetcher).discover(
            topic="agent memory", domain="llm_agent_memory", limit=1
        )

        self.assertEqual(captured[0].query, "agent memory")
        self.assertEqual([candidate.source_id for candidate in candidates], ["one"])
        self.assertEqual(candidates[0].domain, "llm_agent_memory")
        self.assertEqual(candidates[0].published_at, datetime(2026, 8, 22, 10, tzinfo=UTC))

    def test_arxiv_finder_filters_open_closed_window_and_rejects_bad_time(self) -> None:
        def fetcher(subscription):
            return [
                WorkerCandidate("arxiv", "new", "New", [], "2026-08-22T11:00:00Z", "", "", "https://example.com/new", []),
                WorkerCandidate("arxiv", "edge", "Edge", [], "2026-08-22T10:00:00Z", "", "", "https://example.com/edge", []),
            ]

        finder = ArxivCandidateFinder(fetcher=fetcher)
        candidates = finder.discover(
            topic="agent memory",
            domain="memory",
            limit=3,
            window_start=datetime(2026, 8, 22, 10, tzinfo=UTC),
            window_end=datetime(2026, 8, 22, 11, tzinfo=UTC),
        )
        self.assertEqual([candidate.source_id for candidate in candidates], ["new"])

        with self.assertRaisesRegex(ValueError, "published_at"):
            ArxivCandidateFinder(fetcher=lambda subscription: [
                WorkerCandidate("arxiv", "bad", "Bad", [], "", "", "", "https://example.com/bad", [])
            ]).discover(topic="memory", domain="memory", limit=1)

    def test_docling_parser_turns_temporary_markdown_into_anchors(self) -> None:
        def parser(*args, **kwargs):
            return ParsedFullText("2606.10677v1", "https://arxiv.org/pdf/2606.10677v1", "# Method\n\nThe method reaches 90% accuracy.", False)

        material = DoclingSourceParser(parser=parser).parse(_candidate())

        self.assertEqual(material.evidence_level, "full_text_text")
        self.assertEqual(len(material.anchors), 1)
        self.assertIn("90%", next(iter(material.source_fragments.values())))

    def test_docling_parser_prefers_native_blocks_over_markdown_prefixes(self) -> None:
        def parser(*args, **kwargs):
            return ParsedFullText(
                "2606.10677v1",
                "https://arxiv.org/pdf/2606.10677v1",
                "# Abstract\n\nAuthors: A. Researcher\n\nThe method reaches 90% accuracy.",
                False,
                blocks=(
                    ParsedDocumentBlock(
                        kind="text",
                        text="The method reaches 90% accuracy.",
                        section_path=("Method",),
                        page_start=2,
                    ),
                ),
            )

        material = DoclingSourceParser(parser=parser).parse(_candidate())

        self.assertEqual(list(material.evidence_candidates), list(material.anchors))
        candidate = next(iter(material.evidence_candidates.values()))
        self.assertEqual(candidate.section_path, ("Method",))
        self.assertEqual(candidate.page_start, 2)
        self.assertNotIn("Authors:", next(iter(material.source_fragments.values())))

    def test_native_unusable_blocks_remain_transient_but_are_not_fact_fragments(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(
                ParsedDocumentBlock("text", "A valid method statement.", section_path=("Method",), page_start=2),
                ParsedDocumentBlock("formula", "<!-- formula-not-decoded -->", section_path=("Method",), page_start=2),
                ParsedDocumentBlock("table", "| Evaluation | Parent vs continuation |", section_path=("Results",), page_start=4),
            ),
        )

        self.assertEqual(len(material.evidence_candidates), 3)
        self.assertEqual(len(material.evidence_blocks), 3)
        self.assertEqual(len(material.source_fragments), 1)
        self.assertEqual(
            sorted(block.rejection_reason for block in material.evidence_blocks.values() if not block.eligible_for_fact),
            ["parse_unparsed", "table_result_incomplete"],
        )
    def test_entailment_judge_disables_thinking_for_short_json_verdict(self) -> None:
        def post_json(url, payload, headers):
            self.assertEqual(payload["thinking"], NON_THINKING_MODE)
            self.assertEqual(payload["max_tokens"], MAX_JUDGE_OUTPUT_TOKENS)
            self.assertIn("continuous verbatim excerpt", payload["messages"][0]["content"])
            return {"choices": [{"message": {"content": '{"verdict":"supported"}'}}]}

        verdict = DeepSeekEntailmentJudge("deepseek-v4-flash", "test-key", post_json=post_json).assess(
            claim="The method reaches 90% accuracy.", evidence="The method reaches 90% accuracy."
        )

        self.assertEqual(verdict, "supported")
    def test_post_json_maps_provider_timeout_without_raw_error(self) -> None:
        with patch("research_pulse.production.adapters.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(ProviderTimeout):
                _post_json("https://example.invalid", {}, {}, timeout_seconds=0.1)

    def test_truncated_json_is_rejected_before_any_projection(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exceeded its output limit"):
            _response_json(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"summary":"incomplete"'},
                        }
                    ]
                }
            )
    def test_filesystem_publisher_writes_reparseable_markdown_then_indexes(self) -> None:
        bundle = _publishable_bundle()
        asset = bundle.asset
        rag = _Rag()
        registry = _Registry()
        vault = ROOT / "data" / "test-publisher-vault"
        try:
            publisher = FilesystemKnowledgePublisher(vault, rag, registry)
            manifest = publisher.publish(bundle, "test")
            markdown_path = next(vault.glob("papers/**/*.md"))
            metadata, reparsed_body = split_front_matter(markdown_path.read_text(encoding="utf-8"))
            reparsed = KnowledgeBundle.from_markdown(markdown_path)
        finally:
            _remove_test_vault_files(vault)

        self.assertEqual(metadata["content_sha256"], asset.content_sha256)
        self.assertEqual(sha256(reparsed_body.encode("utf-8")).hexdigest(), asset.content_sha256)
        self.assertEqual(reparsed.claims[0].claim_type, "source_fact")
        self.assertEqual(reparsed.asset.schema_version, 3)
        self.assertEqual(reparsed.reading_sections[0].anchor_ids, ("source:test:overview:1",))
        self.assertEqual(rag.published[0].anchors[0].section, "Overview")
        self.assertEqual(registry.marked, [("test", asset.knowledge_id)])
        self.assertEqual(manifest.index_status, "indexed")

    def test_filesystem_publisher_copies_only_selected_visual_asset(self) -> None:
        source = ROOT / "data" / "test-selected-visual.png"
        vault = ROOT / "data" / "test-selected-visual-vault"
        source.write_bytes(b"selected-image")
        try:
            base = _publishable_bundle()
            visual = ReadingVisualEvidence("source:figure:1", "figure", "mechanism", "展示方法结构。", "assets/visual.png")
            body = base.asset.body + "\n![图](assets/visual.png)\n"
            asset = replace(base.asset, body=body, content_sha256=sha256(body.encode("utf-8")).hexdigest())
            bundle = replace(
                base,
                asset=asset,
                reading_sections=(ReadingSectionEvidence("summary", (base.anchors[0].anchor_id,), (visual,)),),
                visual_assets={"assets/visual.png": source},
            )
            publisher = FilesystemKnowledgePublisher(vault, _Rag(), _Registry())
            publisher.publish(bundle, "test")
            copied = next(vault.glob("papers/**/*.md")).parent / "assets" / "visual.png"
            self.assertEqual(copied.read_bytes(), b"selected-image")
        finally:
            source.unlink(missing_ok=True)
            _remove_test_vault_files(vault)

    def test_sidecar_write_failure_does_not_commit_markdown_or_call_rag(self) -> None:
        bundle = _publishable_bundle()
        rag = _Rag()
        registry = _Registry()
        vault = ROOT / "data" / "test-publisher-failure-vault"
        real_write_bytes = Path.write_bytes

        def fail_sidecar(path: Path, value: bytes):
            if path.name.endswith(".provenance.json.tmp"):
                raise OSError("injected sidecar failure")
            return real_write_bytes(path, value)

        try:
            with patch.object(Path, "write_bytes", autospec=True, side_effect=fail_sidecar):
                with self.assertRaisesRegex(OSError, "injected"):
                    FilesystemKnowledgePublisher(vault, rag, registry).publish(bundle, "test")
            self.assertEqual(list(vault.glob("papers/**/*.md")), [])
            self.assertEqual(rag.published, [])
        finally:
            _remove_test_vault_files(vault)


def _publishable_bundle() -> KnowledgeBundle:
    body = "# Test\n\nA source-backed note.\n"
    asset = KnowledgeAsset(
        knowledge_id="kp:arxiv:test",
        knowledge_version="2026-08-22T00:00:00+00:00",
        publication_status="published",
        evidence_level="full_text_text",
        source_urls=("https://example.com/test",),
        domain="test",
        title="Test",
        body=body,
        content_sha256=sha256(body.encode("utf-8")).hexdigest(),
    )
    excerpt = "A source-backed note."
    anchor = DurableEvidenceAnchor(
        "source:test:overview:1",
        asset.source_urls[0],
        excerpt,
        sha256(excerpt.encode()).hexdigest(),
        section="Overview",
    )
    return KnowledgeBundle(
        asset=asset,
        claims=(KnowledgeClaim("claim:test", "source_fact", excerpt, (anchor.anchor_id,)),),
        anchors=(anchor,),
        reading_sections=(ReadingSectionEvidence("summary", (anchor.anchor_id,)),),
    )


def _remove_test_vault_files(vault: Path) -> None:
    if not vault.exists():
        return
    for path in vault.glob("papers/**/*"):
        if path.is_file():
            path.unlink(missing_ok=True)