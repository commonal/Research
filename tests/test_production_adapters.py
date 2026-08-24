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
    DEFAULT_DEEPSEEK_TEXT_MODEL,
    DEFAULT_DEEPSEEK_VISION_MODEL,
    DeepSeekEvidenceMapper,
    DeepSeekDeepReader,
    DeepSeekEntailmentJudge,
    DeepSeekStructuredExtractor,
    DoclingSourceParser,
    FilesystemKnowledgePublisher,
    MAX_EXTRACTION_OUTPUT_TOKENS,
    MAX_JUDGE_OUTPUT_TOKENS,
    MAX_MAP_FALLBACK_OUTPUT_TOKENS,
    MAX_MAP_OUTPUT_TOKENS,
    MAX_READING_FALLBACK_OUTPUT_TOKENS,
    MAX_READING_OUTPUT_TOKENS,
    NON_THINKING_MODE,
    ProviderTimeout,
    THINKING_MODE,
    _post_json,
    _response_json,
    _parse_deep_reading,
    _render_body,
    _build_ordered_reading_units,
    _deepseek_reading_unit_payload,
    _table_html_to_markdown,
    _select_extraction_fragments,
    source_blocks_to_material,
)
from research_pulse.production.pipeline import EvidenceMap, ExtractedDraft, PaperCandidate, ReadingVisual, SourceMaterial
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


def _reading_section(text: str, *block_ids: str, **extra: object) -> dict[str, object]:
    return {"text": text, "evidence_block_ids": list(block_ids), **extra}


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
    def test_environment_defaults_to_current_text_and_provider_vision_models(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_READING_VISION": "true",
                "DEEPSEEK_READING_VISION_MODEL": "",
            },
            clear=False,
        ):
            extractor = DeepSeekStructuredExtractor.from_environment()

        self.assertEqual(extractor.model, DEFAULT_DEEPSEEK_TEXT_MODEL)
        self.assertEqual(extractor.deep_reader.model, DEFAULT_DEEPSEEK_TEXT_MODEL)
        self.assertEqual(extractor.deep_reader.vision_model, DEFAULT_DEEPSEEK_VISION_MODEL)

    def test_complete_table_html_is_rendered_instead_of_false_degraded(self) -> None:
        rendered = _table_html_to_markdown(
            "<table><tr><td>Metric</td><td>Base</td><td>Seed 1</td></tr>"
            "<tr><td>Safe success</td><td>64.36%</td><td>98.48%</td></tr></table>"
        )
        self.assertEqual(
            rendered,
            "| Metric | Base | Seed 1 |\n| --- | --- | --- |\n| Safe success | 64.36% | 98.48% |",
        )

    def test_incomplete_table_html_is_degraded_instead_of_padded(self) -> None:
        self.assertIsNone(
            _table_html_to_markdown(
                "<table><tr><td>Metric</td><td>Base</td><td>Seed 1</td></tr>"
                "<tr><td>Safe success 64.36% 98.48%</td></tr></table>"
            )
        )

    def test_ordered_unit_vision_payload_attaches_only_safe_local_images(self) -> None:
        image_path = Path(tempfile.gettempdir()) / "research-pulse-unit-visual-test.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nunit-test")
        try:
            figure_id = "source:figure:vision"
            figure_candidate = EvidenceCandidate(
                figure_id,
                "figure",
                "Fig. 1 broker workflow.",
                "https://example.com/paper",
                "normalized",
                "available",
                "exact",
                ("Method",),
                page_start=3,
                caption="Fig. 1 broker workflow.",
                image_source_path=image_path,
            )
            block = classify_candidate(figure_candidate)
            material = SourceMaterial(
                evidence_level="full_text_multimodal",
                anchors={},
                source_fragments={figure_id: figure_candidate.text},
                evidence_candidates={figure_id: figure_candidate},
                evidence_blocks={figure_id: block},
            )
            unit = _build_ordered_reading_units(material, max_chars=2_000)[0]
            payload = _deepseek_reading_unit_payload(
                _candidate(),
                unit,
                {},
                "",
                material.evidence_blocks,
                evidence_map=None,
                model="vision-test",
                max_tokens=512,
                vision_enabled=True,
            )
            content = payload["messages"][1]["content"]
            self.assertIsInstance(content, list)
            image_parts = [item for item in content if item.get("type") == "image_url"]
            self.assertEqual(len(image_parts), 1)
            self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        finally:
            image_path.unlink(missing_ok=True)

    def test_ordered_reading_strategy_uses_contiguous_units_then_synthesis(self) -> None:
        material = source_blocks_to_material(
            source_id="ordered-paper",
            source_url="https://example.com/ordered-paper",
            evidence_level="full_text_text",
            blocks=(
                ParsedDocumentBlock("text", "问题先在引言中出现。" * 200, ("Introduction",), 1),
                ParsedDocumentBlock("text", "方法随后定义核心机制。" * 200, ("Method",), 2),
                ParsedDocumentBlock("text", "实验最后验证主张。" * 200, ("Results",), 3),
            ),
        )
        units = _build_ordered_reading_units(material, max_chars=2_000)
        self.assertEqual([unit.start_order for unit in units], [1, 2, 3])
        self.assertEqual([unit.end_order for unit in units], [1, 2, 3])
        anchor_id = next(iter(material.source_fragments))
        calls: list[dict[str, object]] = []

        def post_json(url, payload, headers):
            calls.append(payload)
            system = payload["messages"][0]["content"]
            if "orientation pass" in system:
                response = {"paper_problem": "问题", "claimed_gap": "缺口", "claimed_solution": "方案"}
            elif "reading one contiguous unit" in system:
                response = {"unit_purpose": "理解本单元", "source_block_ids": []}
            else:
                section = _reading_section("综合笔记。", anchor_id)
                response = {
                    "summary": section,
                    "problem": section,
                    "research_question": section,
                    "core_idea": section,
                    "method": section,
                    "workflow": section,
                    "experiments": section,
                    "experiment_design": section,
                    "result_interpretation": section,
                    "limitations": section,
                    "reproduction": section,
                    "reading_boundary": "无。",
                }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        reader = DeepSeekDeepReader(
            "test-model",
            "test-key",
            post_json=post_json,
            reading_strategy="units",
            reading_unit_chars=2_000,
            reading_unit_max_tokens=512,
        )
        analysis = reader.read(_candidate(), material)

        self.assertEqual(len(calls), 1 + len(units) + 1)
        unit_payloads = calls[1:-1]
        self.assertIn("DOC_ORDER 0001", unit_payloads[0]["messages"][1]["content"])
        self.assertIn("DOC_ORDER 0002", unit_payloads[1]["messages"][1]["content"])
        self.assertEqual(analysis.summary.text, "综合笔记。")

    def test_deep_reading_selects_visuals_without_promoting_formula(self) -> None:
        text_id = "source:text:1"
        formula_id = "source:formula:1"
        text_candidate = EvidenceCandidate(
            text_id, "text", "The method separates action verification from execution.",
            "https://example.com/paper", "normalized", "available", "exact", ("Method",), page_start=2,
        )
        formula_candidate = EvidenceCandidate(
            formula_id, "formula", "z(a_t)=f(a_t)", "https://example.com/paper", "normalized", "available", "exact",
            ("Method",), page_start=2, latex=r"z(a_t)=f(a_t)",
        )
        blocks = {
            text_id: classify_candidate(text_candidate),
            formula_id: classify_candidate(formula_candidate),
        }
        section = _reading_section(
            "方法先验证动作，再决定是否执行。公式展示动作如何映射到验证表示。",
            text_id,
            visuals=[{
                "block_id": formula_id,
                "role": "mechanism",
                "explanation": "它把动作变成可供验证器检查的表示。",
                "title": "动作表示公式",
            }],
        )
        payload = {field: section for field in ("summary", "problem", "method", "experiments", "limitations", "reproduction")}
        payload.update({"research_question": section, "core_idea": section, "workflow": section, "experiment_design": section, "result_interpretation": section, "reading_boundary": "公式仅作为理解对象展示。"})
        parsed = _parse_deep_reading(payload, {text_id: text_candidate.text, formula_id: formula_candidate.text}, blocks)
        self.assertEqual(parsed.method.visuals[0].block_id, formula_id)
        self.assertEqual(parsed.method.visuals[0].role, "mechanism")
        self.assertFalse(blocks[formula_id].eligible_for_fact)

    def test_deep_reading_drops_unknown_visual_and_records_boundary(self) -> None:
        text_id = "source:text:2"
        text_candidate = EvidenceCandidate(
            text_id, "text", "The method is evaluated.", "https://example.com/paper", "normalized", "available", "exact",
            ("Experiments",), page_start=4,
        )
        blocks = {text_id: classify_candidate(text_candidate)}
        section = _reading_section(
            "实验验证方法。",
            text_id,
            visuals=[{"block_id": "missing", "role": "comparison", "explanation": "不存在的图。"}],
        )
        payload = {field: section for field in ("summary", "problem", "method", "experiments", "limitations", "reproduction")}
        payload.update({"reading_boundary": "当前未恢复图表。"})
        parsed = _parse_deep_reading(payload, {text_id: text_candidate.text}, blocks)
        self.assertEqual(parsed.experiments.visuals, ())
        self.assertIn("experiments", parsed.reading_boundary)

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

    def test_deepseek_extractor_rejects_unknown_anchor_and_builds_hashed_asset(self) -> None:
        anchor_id = "source:2606.10677v1:method:1"
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method reaches 90% accuracy.", ("Method",), 3),),
        )
        anchor_id = next(iter(material.source_fragments))
        response_payload = {
            "summary": "摘要。",
            "problem": "问题。",
            "method": "方法。",
            "experiments": "90% accuracy。",
            "limitations": "需要更多验证。",
            "reproduction": "尚未验证。",
            "claims": [
                {"claim_type": "source_fact", "text": "The method reaches 90% accuracy.", "anchor_ids": [anchor_id], "facet": "experiment", "page_start": 999, "parse_status": "available"},
                {"claim_type": "agent_inference", "text": "The method may generalize.", "anchor_ids": []},
                {"claim_type": "reading_question", "text": "Can the result be reproduced?", "anchor_ids": []},
            ],
        }

        def post_json(url, payload, headers):
            self.assertIn(f"EVIDENCE_BLOCK {anchor_id}", payload["messages"][1]["content"])
            self.assertIn("allowed_facets=method", payload["messages"][1]["content"])
            self.assertEqual(payload["thinking"], NON_THINKING_MODE)
            return {"choices": [{"message": {"content": json.dumps(response_payload)}}]}

        draft = DeepSeekStructuredExtractor("deepseek-chat", "test-key", post_json=post_json).extract(
            _candidate(), material
        )

        self.assertEqual(draft.claims[0].anchor_ids, (anchor_id,))
        self.assertEqual(draft.claims[0].source_facet, "method")
        self.assertNotIn("page_start", draft.claims[0].__dict__)
        self.assertEqual(
            [claim.claim_type for claim in draft.claims],
            ["source_fact", "agent_inference", "reading_question"],
        )
        self.assertEqual(draft.asset.content_sha256, sha256(draft.asset.body.encode("utf-8")).hexdigest())
        self.assertIn("证据主张", draft.asset.body)

    def test_deepseek_extractor_accepts_fenced_json_without_weakening_anchor_checks(self) -> None:
        anchor_id = "source:2606.10677v1:method:1"
        material = SourceMaterial(
            evidence_level="full_text_text",
            anchors={},
            source_fragments={anchor_id: "The method reaches 90% accuracy."},
        )
        response_payload = {
            "summary": "摘要。",
            "problem": "问题。",
            "method": "方法。",
            "experiments": "实验。",
            "limitations": "局限。",
            "reproduction": "复现。",
            "claims": [{"claim_type": "source_fact", "text": "中文释义不应作为来源事实保存。", "anchor_ids": [anchor_id], "facet": "method"}],
        }

        def post_json(url, payload, headers):
            self.assertEqual(payload["max_tokens"], MAX_EXTRACTION_OUTPUT_TOKENS)
            self.assertEqual(payload["thinking"], NON_THINKING_MODE)
            return {"choices": [{"message": {"content": "模型输出：\n```json\n" + json.dumps(response_payload) + "\n```"}}]}

        draft = DeepSeekStructuredExtractor("deepseek-chat", "test-key", post_json=post_json).extract(
            _candidate(), material
        )

        self.assertEqual(draft.claims[0].anchor_ids, (anchor_id,))
        self.assertEqual(draft.claims[0].text, "The method reaches 90% accuracy.")

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

    def test_deep_reader_keeps_thinking_enabled_and_returns_bounded_analysis(self) -> None:
        anchor_id = "source:test:method"
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )

        def post_json(url, payload, headers):
            self.assertEqual(payload["thinking"], THINKING_MODE)
            self.assertEqual(payload["max_tokens"], MAX_READING_OUTPUT_TOKENS)
            selected_id = next(iter(material.source_fragments))
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": _reading_section("论文总结。", selected_id),
                                    "problem": _reading_section("研究问题。", selected_id),
                                    "core_idea": _reading_section("核心直觉。", selected_id),
                                    "method": _reading_section("方法解释。", selected_id),
                                    "workflow": _reading_section("工作流程。", selected_id),
                                    "experiments": _reading_section("实验解释。", selected_id),
                                    "experiment_design": _reading_section("实验设计。", selected_id),
                                    "result_interpretation": _reading_section("结果解读。", selected_id),
                                    "limitations": _reading_section("局限解释。", selected_id),
                                    "reproduction": _reading_section("复现线索。", selected_id),
                                    "reading_boundary": "公式未结构化解析。",
                                }
                            )
                        }
                    }
                ]
            }

        analysis = DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material
        )

        self.assertEqual(analysis.method.text, "方法解释。")
        self.assertEqual(analysis.core_idea.text, "核心直觉。")
        self.assertEqual(analysis.workflow.text, "工作流程。")
        self.assertEqual(analysis.method.evidence_block_ids, (next(iter(material.source_fragments)),))

    def test_deep_reader_does_not_advertise_map_ids_outside_its_bounded_context(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )
        selected_id = next(iter(material.source_fragments))

        def post_json(url, payload, headers):
            user_content = payload["messages"][1]["content"]
            self.assertIn(selected_id, user_content)
            self.assertNotIn("source:outside:context", user_content)
            response = {
                "summary": _reading_section("论文总结。", selected_id),
                "problem": _reading_section("研究问题。", selected_id),
                "method": _reading_section("方法解释。", selected_id),
                "experiments": _reading_section("实验解释。", selected_id),
                "limitations": _reading_section("局限解释。", selected_id),
                "reproduction": _reading_section("复现线索。", selected_id),
                "reading_boundary": "仅使用有界上下文。",
            }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(),
            material,
            evidence_map=EvidenceMap(((selected_id, "方法块。"), ("source:outside:context", "越界块。"))),
        )

    def test_deep_reader_uses_shared_paper_context_for_each_section(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(
                ParsedDocumentBlock("text", "The paper addresses unnecessary authority.", ("Introduction",), 1),
                ParsedDocumentBlock("text", "The method learns a task-conditioned policy.", ("Method",), 2),
                ParsedDocumentBlock("text", "The evaluation reports 90.50% success.", ("Results",), 3),
                ParsedDocumentBlock("text", "The study does not evaluate deployment drift.", ("Limitations",), 4),
            ),
        )
        ids_by_facet = {
            facet: next(
                block_id
                for block_id, block in material.evidence_blocks.items()
                if facet in block.supported_facets
            )
            for facet in ("problem", "method", "experiment", "limitation")
        }

        def post_json(url, payload, headers):
            user_content = payload["messages"][1]["content"]
            experiments = user_content.split("### experiments", 1)[1].split("###", 1)[0]
            method = user_content.split("### method", 1)[1].split("###", 1)[0]
            self.assertIn(ids_by_facet["experiment"], experiments)
            self.assertIn(ids_by_facet["problem"], experiments)
            self.assertIn(ids_by_facet["method"], experiments)
            self.assertIn(ids_by_facet["method"], method)
            self.assertIn(ids_by_facet["experiment"], method)
            response = {
                "summary": _reading_section("论文总结。", ids_by_facet["problem"]),
                "problem": _reading_section("研究问题。", ids_by_facet["problem"]),
                "core_idea": _reading_section("核心直觉。", ids_by_facet["method"]),
                "method": _reading_section("方法解释。", ids_by_facet["method"]),
                "workflow": _reading_section("工作流程。", ids_by_facet["method"]),
                "experiments": _reading_section("实验解释。", ids_by_facet["experiment"]),
                "experiment_design": _reading_section("实验设计。", ids_by_facet["experiment"]),
                "result_interpretation": _reading_section("结果解读。", ids_by_facet["experiment"]),
                "limitations": _reading_section("局限解释。", ids_by_facet["limitation"]),
                "reproduction": _reading_section("复现线索。", ids_by_facet["method"]),
            "reading_boundary": "使用共享论文级证据上下文。",
            }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material
        )

    def test_deep_reader_keeps_uncovered_required_sections_empty_for_quality_review(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )
        method_id = next(iter(material.source_fragments))

        def post_json(url, payload, headers):
            empty = _reading_section("")
            response = {
                "summary": empty,
                "problem": empty,
                "core_idea": _reading_section("核心直觉。", method_id),
                "method": _reading_section("方法解释。", method_id),
                "workflow": _reading_section("工作流程。", method_id),
                "experiments": empty,
                "experiment_design": empty,
                "result_interpretation": empty,
                "limitations": empty,
                "reproduction": _reading_section("复现线索。", method_id),
                "reading_boundary": "问题、实验和局限没有合格证据块。",
            }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        analysis = DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material
        )

        self.assertEqual(analysis.problem.text, "")
        self.assertEqual(analysis.experiments.text, "")
        self.assertEqual(analysis.method.evidence_block_ids, (method_id,))

    def test_deep_reader_can_be_restricted_to_projection_candidates(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(
                ParsedDocumentBlock("text", "The first method detail.", ("Method",), 1),
                ParsedDocumentBlock("text", "The selected method detail.", ("Method",), 2),
            ),
        )
        first_id, selected_id = material.source_fragments

        def post_json(url, payload, headers):
            user_content = payload["messages"][1]["content"]
            method = user_content.split("### method", 1)[1].split("###", 1)[0]
            self.assertIn(selected_id, method)
            self.assertNotIn(first_id, method)
            empty = _reading_section("")
            response = {
                "summary": empty,
                "problem": empty,
                "core_idea": _reading_section("核心直觉。", selected_id),
                "method": _reading_section("方法解释。", selected_id),
                "workflow": _reading_section("工作流程。", selected_id),
                "experiments": empty,
                "experiment_design": empty,
                "result_interpretation": empty,
                "limitations": empty,
                "reproduction": _reading_section("复现线索。", selected_id),
                "reading_boundary": "仅使用投影候选。",
            }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material, allowed_block_ids=(selected_id,)
        )

    def test_deep_reader_context_matches_durable_excerpt_boundary(self) -> None:
        prefix = "The method is bounded and source grounded. "
        long_text = (prefix * 30) + "The appendix mentions a 4B model."
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", long_text, ("Method",), 3),),
        )
        selected_id = next(iter(material.source_fragments))

        def post_json(url, payload, headers):
            method = payload["messages"][1]["content"].split("### method", 1)[1].split("###", 1)[0]
            self.assertIn(selected_id, method)
            self.assertNotIn("4B model", method)
            self.assertNotIn("allowed_numeric_tokens=", method)
            empty = _reading_section("")
            response = {
                "summary": empty,
                "problem": empty,
                "core_idea": _reading_section("核心直觉。", selected_id),
                "method": _reading_section("方法解释。", selected_id),
                "workflow": _reading_section("工作流程。", selected_id),
                "experiments": empty,
                "experiment_design": empty,
                "result_interpretation": empty,
                "limitations": empty,
                "reproduction": _reading_section("复现线索。", selected_id),
                "reading_boundary": "只使用 durable excerpt。",
            }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material, allowed_block_ids=(selected_id,)
        )

    def test_deep_reader_retries_truncated_thinking_json_without_publishing_partial_output(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )
        calls: list[dict[str, object]] = []
        selected_id = next(iter(material.source_fragments))

        def post_json(url, payload, headers):
            calls.append(payload)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"summary":"incomplete"'},
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": _reading_section("论文总结。", selected_id),
                                    "problem": _reading_section("研究问题。", selected_id),
                                    "method": _reading_section("方法解释。", selected_id),
                                    "experiments": _reading_section("实验解释。", selected_id),
                                    "limitations": _reading_section("局限解释。", selected_id),
                                    "reproduction": _reading_section("复现线索。", selected_id),
                                    "reading_boundary": "仅使用文本块。",
                                }
                            )
                        }
                    }
                ]
            }

        analysis = DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material
        )

        self.assertEqual(analysis.summary.text, "论文总结。")
        self.assertEqual([call["thinking"] for call in calls], [THINKING_MODE, NON_THINKING_MODE])
        self.assertEqual(calls[1]["max_tokens"], MAX_READING_FALLBACK_OUTPUT_TOKENS)

    def test_deep_reader_keeps_only_eligible_section_specific_block_ids(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(
                ParsedDocumentBlock("text", "The paper addresses broad execution privileges.", ("Introduction",), 1),
                ParsedDocumentBlock("text", "The method learns a task-conditioned policy.", ("Method",), 3),
                ParsedDocumentBlock("formula", "formula-not-decoded", ("Method",), 3),
            ),
        )
        problem_id, method_id = material.source_fragments
        formula_id = next(block_id for block_id in material.evidence_blocks if block_id not in material.source_fragments)

        def post_json(url, payload, headers):
            response = {
                "summary": _reading_section("论文总结。", problem_id, method_id),
                "problem": _reading_section("研究问题。", problem_id, quote="模型伪造摘录", page=999),
                "method": _reading_section(
                    "方法解释。",
                    problem_id,
                    formula_id,
                    "unknown:block",
                    parse_status="available",
                    bbox=[0, 0, 1, 1],
                ),
                "experiments": _reading_section("实验解释。", method_id),
                "limitations": _reading_section("局限解释。", method_id),
                "reproduction": _reading_section("复现线索。", method_id),
                "reading_boundary": "公式未结构化解析。",
                "evidence_block_ids": [problem_id, method_id],
            }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        analysis = DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material
        )

        self.assertEqual(analysis.problem.evidence_block_ids, (problem_id,))
        self.assertEqual(analysis.method.evidence_block_ids, ("source:2606.10677v1:introduction:1",))
        self.assertEqual(analysis.experiments.evidence_block_ids, (method_id,))
        self.assertEqual(analysis.reproduction.evidence_block_ids, (method_id,))
        self.assertFalse(hasattr(analysis.problem, "quote"))
        self.assertFalse(hasattr(analysis.problem, "page"))
        self.assertIn("服务端丢弃", analysis.reading_boundary)

    def test_deep_reader_accepts_boundary_text_object_without_trusting_model_metadata(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )
        selected_id = next(iter(material.source_fragments))

        def post_json(url, payload, headers):
            response = {
                "summary": _reading_section("论文总结。", selected_id),
                "problem": _reading_section("研究问题。", selected_id),
                "method": _reading_section("方法解释。", selected_id),
                "experiments": _reading_section("实验解释。", selected_id),
                "limitations": _reading_section("局限解释。", selected_id),
                "reproduction": _reading_section("复现线索。", selected_id),
                "reading_boundary": {
                    "text": "公式未结构化解析。",
                    "parse_status": "available",
                    "quote": "不应信任的模型摘录",
                },
            }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        analysis = DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json).read(
            _candidate(), material
        )

        self.assertTrue(analysis.reading_boundary.startswith("公式未结构化解析。"))
        self.assertNotIn("不应信任", analysis.reading_boundary)

    def test_evidence_mapper_preserves_only_supplied_block_ids(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )
        selected_id = next(iter(material.source_fragments))

        def post_json(url, payload, headers):
            self.assertEqual(payload["thinking"], THINKING_MODE)
            self.assertEqual(payload["max_tokens"], MAX_MAP_OUTPUT_TOKENS)
            return {"choices": [{"message": {"content": json.dumps({"items": [{"block_id": selected_id, "summary": "方法块。"}]})}}]}

        mapped = DeepSeekEvidenceMapper("deepseek-v4-flash", "test-key", post_json=post_json).map(
            _candidate(), material
        )

        self.assertEqual(mapped.summaries, ((selected_id, "方法块。"),))

    def test_evidence_mapper_retries_truncated_thinking_json(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )
        selected_id = next(iter(material.source_fragments))
        calls: list[dict[str, object]] = []

        def post_json(url, payload, headers):
            calls.append(payload)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"items":['},
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"items": [{"block_id": selected_id, "summary": "方法块。"}]}
                            )
                        }
                    }
                ]
            }

        mapper = DeepSeekEvidenceMapper("deepseek-v4-flash", "test-key", post_json=post_json)
        result = mapper.map(_candidate(), material)

        self.assertEqual(result.summaries, ((selected_id, "方法块。"),))
        self.assertEqual([call["thinking"] for call in calls], [THINKING_MODE, NON_THINKING_MODE])
        self.assertEqual(calls[1]["max_tokens"], MAX_MAP_FALLBACK_OUTPUT_TOKENS)

    def test_configured_extractor_runs_map_deep_read_and_projection_separately(self) -> None:
        material = source_blocks_to_material(
            source_id="2606.10677v1",
            source_url="https://arxiv.org/abs/2606.10677v1",
            evidence_level="full_text_text",
            blocks=(ParsedDocumentBlock("text", "The method builds a verified index.", ("Method",), 3),),
        )
        anchor_id = next(iter(material.source_fragments))
        calls: list[dict[str, Any]] = []

        def post_json(url, payload, headers):
            calls.append(payload)
            if payload["thinking"] == THINKING_MODE and "evidence-map pass" in payload["messages"][0]["content"]:
                response = {"items": [{"block_id": anchor_id, "summary": "方法块。"}]}
            elif payload["thinking"] == THINKING_MODE:
                response = {
                    "summary": _reading_section("深度总结。", anchor_id),
                    "problem": _reading_section("问题解读。", anchor_id),
                    "core_idea": _reading_section("核心直觉解读。", anchor_id),
                    "method": _reading_section("方法解读。", anchor_id),
                    "workflow": _reading_section("工作流程解读。", anchor_id),
                    "experiments": _reading_section("实验解读。", anchor_id),
                    "experiment_design": _reading_section("实验设计解读。", anchor_id),
                    "result_interpretation": _reading_section("结果解读。", anchor_id),
                    "limitations": _reading_section("局限解读。", anchor_id),
                    "reproduction": _reading_section("复现线索。", anchor_id),
                    "reading_boundary": "仅分析文本块。",
                }
            else:
                response = {
                    "summary": "投影摘要。",
                    "problem": "问题。",
                    "method": "方法。",
                    "experiments": "实验。",
                    "limitations": "局限。",
                    "reproduction": "复现。",
                    "claims": [{"claim_type": "source_fact", "text": "原文", "anchor_ids": [anchor_id], "facet": "method"}],
                }
            return {"choices": [{"message": {"content": json.dumps(response)}}]}

        mapper = DeepSeekEvidenceMapper("deepseek-v4-flash", "test-key", post_json=post_json)
        reader = DeepSeekDeepReader("deepseek-v4-flash", "test-key", post_json=post_json)
        extractor = DeepSeekStructuredExtractor(
            "deepseek-v4-flash", "test-key", post_json=post_json, evidence_mapper=mapper, deep_reader=reader
        )

        draft = extractor.extract(_candidate(), material)

        self.assertEqual(len(calls), 3)
        self.assertEqual([call["thinking"] for call in calls], [THINKING_MODE, THINKING_MODE, NON_THINKING_MODE])
        self.assertIn("deep-reading pass", calls[1]["messages"][0]["content"])
        self.assertIn("source-bounded paper reading draft", calls[2]["messages"][0]["content"])
        self.assertIn("深度总结。", draft.asset.body)
        self.assertIn("## 核心想法：把问题变成可计算目标", draft.asset.body)
        self.assertIn("工作流程解读。", draft.asset.body)
        self.assertIn("## 应该怎样解读，而不是过度宣传", draft.asset.body)
        self.assertIn("精读证据边界", draft.asset.body)

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

    def test_extractor_selects_a_small_facet_balanced_context(self) -> None:
        blocks = {}
        fragments = {}
        for facet, section, text in (
            ("problem", "Introduction", "The paper addresses stale memory."),
            ("method", "Method", "The method builds a verified index."),
            ("experiment", "Results", "The evaluation reaches 90% accuracy."),
            ("limitation", "Limitations", "A limitation is that scale is not evaluated."),
        ):
            anchor_id = f"source:test:{facet}"
            candidate = EvidenceCandidate(
                block_id=anchor_id,
                kind="text",
                text=text,
                source_url="https://arxiv.org/abs/2606.10677v1",
                parser="fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=(section,),
            )
            blocks[anchor_id] = classify_candidate(candidate)
            fragments[anchor_id] = text
        for ordinal in range(20):
            anchor_id = f"source:test:noise:{ordinal}"
            candidate = EvidenceCandidate(
                block_id=anchor_id,
                kind="text",
                text=f"Unprioritized body evidence {ordinal}.",
                source_url="https://arxiv.org/abs/2606.10677v1",
                parser="fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=("Appendix",),
            )
            blocks[anchor_id] = classify_candidate(candidate)
            fragments[anchor_id] = candidate.text
        material = SourceMaterial("full_text_text", {}, fragments, evidence_blocks=blocks)

        selected = _select_extraction_fragments(material, 60_000)

        self.assertEqual(set(selected), {f"source:test:{facet}" for facet in ("problem", "method", "experiment", "limitation")})
        self.assertEqual(MAX_EXTRACTION_OUTPUT_TOKENS, 4_096)

    def test_extractor_keeps_paper_band_candidates_and_does_not_backfill_with_table_title(self) -> None:
        candidates = (
            EvidenceCandidate(
                block_id="source:test:method:1",
                kind="text",
                text="The method learns a task-conditioned permission boundary from execution traces.",
                source_url="https://arxiv.org/abs/2606.10677v1",
                parser="fixture",
                parse_status="available",
                locator_completeness="exact",
                section_path=("Method",),
                page_start=3,
            ),
            EvidenceCandidate(
                block_id="source:test:method:2",
                kind="text",
                text="The policy applies the learned boundary before each tool execution.",
                source_url="https://arxiv.org/abs/2606.10677v1",
                parser="fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=("Method",),
            ),
            EvidenceCandidate(
                block_id="source:test:method:3",
                kind="text",
                text="A third method description is outside the per-facet block budget.",
                source_url="https://arxiv.org/abs/2606.10677v1",
                parser="fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=("Method",),
            ),
            EvidenceCandidate(
                block_id="source:test:table-title",
                kind="caption",
                text="TABLE V. CONTINUATION RESULTS",
                source_url="https://arxiv.org/abs/2606.10677v1",
                parser="fixture",
                parse_status="available",
                locator_completeness="section_only",
                section_path=("Results",),
            ),
        )
        material = SourceMaterial(
            "full_text_text",
            {},
            {candidate.block_id: candidate.text for candidate in candidates},
            evidence_blocks={candidate.block_id: classify_candidate(candidate) for candidate in candidates},
        )

        selected = _select_extraction_fragments(material, 60_000)

        self.assertEqual(tuple(selected), ("source:test:method:1", "source:test:method:2", "source:test:method:3"))
        self.assertNotIn("source:test:table-title", selected)

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
