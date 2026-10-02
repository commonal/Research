from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import re
import tempfile
import unittest

from research_pulse.knowledge.models import KnowledgeBundle
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.traceable_reading.asset_quality import assess_representation
from research_pulse.traceable_reading.contracts import Finding, ReadingAnswer, RequirementResult
from research_pulse.traceable_reading.contracts import MaterialDocument
from research_pulse.traceable_reading.material import MaterialError, load_material_document
from research_pulse.traceable_reading.pipeline import (
    BasicEvidenceGate,
    TraceableReadingError,
    TraceableReadingPipeline,
    build_reading_packets,
    build_survey_view,
    resolve_source_spans,
    SurveyReader,
    build_coverage,
)


class ScriptedModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def call_json(self, operation: str, prompt: str):
        self.calls.append((operation, prompt))
        request = json.loads(prompt)
        if operation == "paper_survey":
            section_id = request["survey_view"].split("[")[1].split("]")[0]
            asset_ids = [re.search(r"\[(asset:[^\]]+)\]", line).group(1) for line in request["survey_view"].splitlines() if "[asset:" in line and "Figure 2" not in line]
            obligations = []
            for facet in ("problem", "method", "experiments", "limitations"):
                obligations.append({
                    "obligation_id": f"obligation:{facet}",
                    "question": f"论文的{facet}是什么？",
                    "purpose": f"理解{facet}",
                    "facet": facet,
                    "core": True,
                    "target_section_ids": [section_id],
                    "target_asset_ids": asset_ids if facet == "method" else [],
                    "evidence_requirements": [{"requirement_id": f"req:{facet}", "description": f"核查论文如何说明{facet}", "required": True}],
                })
            return {
                "paper_map": {
                    "paper_type": "method",
                    "problem": "治理长期记忆",
                    "approach": "持久化验证信号",
                    "experiments": "基准与消融",
                    "limitations": "依赖验证器",
                    "important_asset_ids": asset_ids,
                    "open_questions": [],
                },
                "obligations": obligations,
            }
        if operation == "finding_read":
            block_id = request["material"].split("[")[1].split(" |")[0]
            answers = []
            for obligation in request["obligations"]:
                facet = obligation["facet"]
                finding_id = f"finding:{facet}"
                answers.append({
                    "answer_id": f"answer:{facet}", "obligation_id": obligation["obligation_id"],
                    "status": "answered", "answer": f"论文对{facet}给出了完整说明，并保持上下文关系。",
                    "findings": [{"finding_id": finding_id, "statement": f"论文说明了{facet}。", "answer_quote": f"对{facet}给出了完整说明", "requirement_id": f"req:{facet}", "support_kind": "direct", "block_refs": [block_id]}],
                    "requirement_results": [{"requirement_id": f"req:{facet}", "status": "answered", "finding_ids": [finding_id]}],
                    "asset_uses": [{"asset_id": item["asset_id"], "explanation": "该素材帮助解释方法或证据。", "finding_refs": [finding_id]} for item in request.get("assets", [])],
                })
            return {"answers": answers}
        if operation.startswith("traceable_note_plan_"):
            self.assert_writer_is_bounded(request)
            answer_by_facet = {item["obligation_id"].split(":")[-1]: item for item in request["reading_answers"]}
            sections = []
            for facet in request["target_facets"]:
                answer = answer_by_facet[facet]
                finding_id = answer["findings"][0]["finding_id"]
                sections.append({
                    "section_id": f"note:{facet}", "facet": facet, "title": f"{facet}解读",
                    "reader_question": f"论文的{facet}应当怎样理解？", "direct_answer": answer["answer"],
                    "mechanism_sequence": ["先识别输入，再执行判断"] if facet == "method" else [],
                    "evidence_focus": ["读取对照结果及其差异"] if facet == "experiments" else [],
                    "interpretation_goal": f"理解{facet}对论文主张意味着什么",
                    "boundary": f"结论只限于论文明确报告的{facet}证据",
                    "finding_refs": [finding_id],
                    "asset_refs": [item["asset_id"] for item in request["asset_decisions"] if item["decision"] != "omit" and finding_id in item["finding_refs"]],
                    "worked_example": ({
                        "mode": "abstract_walkthrough", "setup": "跟踪一条候选记忆进入系统",
                        "steps": ["候选记忆先进入 verifier", "验证结果决定是否激活"],
                        "takeaway": "验证信号贯穿准入过程", "finding_refs": [finding_id],
                    } if facet == "method" else None),
                    "experiment_units": ([{
                        "unit_id": "experiment:main", "claim": "完整方法能够改善目标指标",
                        "comparison": "比较完整方法与基线", "result": "论文报告完整方法表现更好",
                        "meaning": "该比较支持核心方法有效", "boundary": "仅限论文报告的设置",
                        "finding_refs": [finding_id],
                    }] if facet == "experiments" else []),
                })
            return {"policy_version": request["writer_policy"]["version"], "sections": sections}
        if operation == "traceable_note_write":
            self.assert_writer_is_bounded(request)
            answers = {item["obligation_id"].split(":")[-1]: item for item in request["reading_answers"]}
            sections = []
            for plan in request["note_plan"]["sections"]:
                facet = plan["facet"]
                finding_id = answers[facet]["findings"][0]["finding_id"]
                primary_role = {"problem": "context", "method": "mechanism", "experiments": "evidence", "limitations": "limitation"}[facet]
                plan_item_refs = ["worked_example"] if facet == "method" else [item["unit_id"] for item in plan["experiment_units"]] if facet == "experiments" else []
                blocks = [{"block_id": f"note:{facet}:answer", "kind": "paragraph", "text": plan["direct_answer"], "finding_refs": [finding_id], "asset_ref": None, "caption": "", "explanation": "", "explanation_role": primary_role, "paragraph_purpose": f"直接回答读者对{facet}的核心疑问", "plan_item_refs": plan_item_refs}]
                if facet in {"method", "experiments"}:
                    blocks.append({"block_id": f"note:{facet}:meaning", "kind": "paragraph", "text": f"这说明{plan['interpretation_goal']}，同时{plan['boundary']}。", "finding_refs": [finding_id], "asset_ref": None, "caption": "", "explanation": "", "explanation_role": "interpretation", "paragraph_purpose": f"解释{facet}证据意味着什么以及结论边界"})
                sections.append({"section_id": plan["section_id"], "title": plan["title"], "reader_question": plan["reader_question"], "blocks": blocks})
            return {"title": "MemGuard 可追溯阅读笔记", "sections": sections}
        raise AssertionError(operation)

    def assert_writer_is_bounded(self, request):
        serialized = json.dumps(request, ensure_ascii=False)
        if "这是 MinerU 编排的全文独有标记" in serialized:
            raise AssertionError("Writer received full.md")


class TraceableReadingPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        self.material_root = self.root / "material"
        self.material_root.mkdir()
        (self.material_root / "full.md").write_text(
            "# MemGuard\n\n这是 MinerU 编排的全文独有标记。\n\n## Method\n\nVerifier controls admission.\n",
            encoding="utf-8",
        )
        content = [
            {"type": "text", "text_level": 1, "text": "MemGuard", "page_idx": 0, "bbox": [10, 10, 900, 50]},
            {"type": "text", "text": "Verifier controls admission.", "page_idx": 0, "bbox": [10, 60, 900, 140]},
            {"type": "text", "text_level": 2, "text": "Method", "page_idx": 1, "bbox": [10, 10, 900, 50]},
            {"type": "text", "text": "Signals persist for lifecycle governance.", "page_idx": 1, "bbox": [10, 60, 900, 140]},
            {"type": "image", "content": "Framework", "image_caption": ["Figure 1"], "img_path": "images/figure-1.png", "page_idx": 1, "bbox": [10, 150, 500, 500]},
            {"type": "image", "content": "Figure 2 chart", "image_caption": ["Figure 2"], "img_path": "images/figure-2-table.png", "page_idx": 1, "bbox": [510, 150, 990, 500]},
            {"type": "table", "table_body": "<table><tr><th>Metric</th><th>Score</th></tr><tr><td>Accuracy</td><td>0.91</td></tr></table>", "table_caption": ["Table 1"], "page_idx": 1, "bbox": [10, 510, 990, 700]},
            {"type": "equation", "text": "s=\\sigma(Wx+b)", "page_idx": 1, "bbox": [10, 710, 990, 780]},
            {"type": "table", "table_body": "| Metric ***** |", "table_caption": ["Table 2"], "img_path": "images/figure-2-table.png", "page_idx": 2, "bbox": [10, 100, 900, 200]},
            {"type": "image", "content": "Ablation", "image_caption": ["Figure 3"], "img_path": "images/figure-3.png", "page_idx": 2, "bbox": [10, 210, 900, 400]},
        ]
        (self.material_root / "content_list.json").write_text(json.dumps(content), encoding="utf-8")
        (self.material_root / "images").mkdir()
        (self.material_root / "images" / "figure-1.png").write_bytes(b"selected")
        (self.material_root / "images" / "figure-2-table.png").write_bytes(b"selected")
        self.document = load_material_document(self.material_root, source_id="2608.21867", source_url="https://arxiv.org/abs/2608.21867")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_material_document_preserves_dual_views_identity_and_sections(self) -> None:
        self.assertIn("全文独有标记", self.document.full_markdown())
        self.assertEqual([block.order for block in self.document.blocks], list(range(10)))
        self.assertEqual(self.document.blocks[3].page, 2)
        self.assertEqual(self.document.blocks[3].bbox, (10.0, 60.0, 900.0, 140.0))
        self.assertIn("content_list.json#/3", self.document.blocks[3].locator)
        self.assertTrue(any(section.title == "Method" and section.start_order == 2 for section in self.document.sections))
        payload = self.document.to_payload()
        self.assertEqual(payload["blocks"][3]["material_version"], self.document.material_version)
        self.assertEqual(MaterialDocument.from_payload(payload), self.document)

    def test_material_requires_both_full_markdown_and_content_list(self) -> None:
        (self.material_root / "full.md").unlink()
        with self.assertRaisesRegex(MaterialError, "full_markdown"):
            load_material_document(self.material_root, source_id="x", source_url="https://example.test/x")

    def test_survey_view_has_deterministic_bounded_fallback(self) -> None:
        view = build_survey_view(self.document, max_chars=80)
        self.assertIn("论文目录", view)
        self.assertLess(len(view), 700)

    def test_invalid_survey_plan_stops_before_finding_reader(self) -> None:
        class InvalidSurveyModel(ScriptedModel):
            def call_json(self, operation, prompt):
                self.calls.append((operation, prompt))
                return {"paper_map": {"paper_type": "method", "problem": "p", "approach": "a", "experiments": "e", "limitations": "l"}, "obligations": []}

        model = InvalidSurveyModel()
        with self.assertRaisesRegex(TraceableReadingError, "survey_invalid_output"):
            TraceableReadingPipeline(model, self.root / "knowledge").run(self.document, domain="agents")
        self.assertEqual([item[0] for item in model.calls], ["paper_survey"])

    def test_survey_rejects_unknown_section_and_missing_core_facets(self) -> None:
        valid = ScriptedModel().call_json("paper_survey", json.dumps({"survey_view": f"[{self.document.sections[0].section_id}]"}))
        valid["obligations"][0]["target_section_ids"] = ["section:missing"]
        class Fixed:
            def call_json(self, operation, prompt):
                return valid
        with self.assertRaisesRegex(TraceableReadingError, "unknown_section"):
            SurveyReader(Fixed()).read(self.document)

    def test_full_pipeline_publishes_but_remains_rag_ineligible(self) -> None:
        model = ScriptedModel()
        vault = self.root / "knowledge"
        receipt = TraceableReadingPipeline(model, vault).run(self.document, domain="agents")

        self.assertEqual(receipt.publication_status, "published")
        self.assertEqual(receipt.semantic_evidence_status, "not_evaluated")
        self.assertFalse(receipt.rag_eligible)
        markdown = Path(receipt.published_path)
        self.assertTrue(markdown.is_file())
        bundle = KnowledgeBundle.from_markdown(markdown)
        self.assertEqual(bundle.asset.evidence_level, "source_linked_unverified")
        self.assertFalse(bundle.asset.rag_eligible)
        self.assertFalse(bundle.answer_eligible)
        markdown_text = markdown.read_text(encoding="utf-8")
        self.assertIn("# MemGuard 可追溯阅读笔记\n\n> 原论文标题：MemGuard\n", markdown_text)
        self.assertNotIn("[^finding:", markdown_text)
        self.assertIn("研究背景与动机：", markdown_text)
        self.assertIn("### 贯穿示例", markdown_text)
        self.assertIn("1. 候选记忆先进入 verifier", markdown_text)
        self.assertEqual(len(FilesystemKnowledgeReader(vault).recent(limit=10)), 1)
        copied_assets = list(markdown.parent.joinpath("assets").glob("*"))
        self.assertEqual(len(copied_assets), 2)
        self.assertEqual({path.read_bytes() for path in copied_assets}, {b"selected"})
        self.assertFalse(any(path.name in {"full.md", "content_list.json", "figure-3.png"} for path in markdown.parent.rglob("*")))
        evidence = json.loads(Path(receipt.evidence_index_path).read_text(encoding="utf-8"))
        self.assertEqual(evidence["semantic_evidence_status"], "not_evaluated")
        self.assertEqual(len(evidence["reading_plan"]["obligations"]), 4)
        self.assertTrue(evidence["reading_coverage"]["core_complete"])
        self.assertEqual(evidence["paper_map"]["approach"], "持久化验证信号")
        self.assertEqual(evidence["writer_policy_version"], "pedagogical-note-v3")
        self.assertEqual(evidence["original_title"], "MemGuard")
        method_plan = next(item for item in evidence["note_plan"]["sections"] if item["facet"] == "method")
        self.assertEqual(method_plan["worked_example"]["mode"], "abstract_walkthrough")
        experiment_plan = next(item for item in evidence["note_plan"]["sections"] if item["facet"] == "experiments")
        self.assertEqual(len(experiment_plan["experiment_units"]), 1)
        self.assertEqual({item["facet"] for item in evidence["note_plan"]["sections"]}, {"problem", "method", "experiments", "limitations"})
        planner_request = json.loads(next(prompt for operation, prompt in model.calls if operation == "traceable_note_plan_method"))
        self.assertNotIn("survey_view", planner_request)
        self.assertNotIn("source_spans", planner_request)
        self.assertTrue(all(span["blocks"][0]["page"] >= 1 for span in evidence["source_spans"]))
        self.assertEqual([call[0] for call in model.calls], ["paper_survey", "finding_read", "finding_read", "traceable_note_plan_method", "traceable_note_plan_experiments", "traceable_note_write"])
        receipt_path = next((vault / "receipts" / "2608.21867").glob("*.json"))
        receipt_text = receipt_path.read_text(encoding="utf-8")
        self.assertNotIn("全文独有标记", receipt_text)

    def test_pipeline_can_write_review_draft_without_exposing_it_to_reader(self) -> None:
        """The workbench approval gate must be enforced by the canonical writer."""
        vault = self.root / "review-knowledge"
        receipt = TraceableReadingPipeline(ScriptedModel(), vault).run(
            self.document,
            domain="agents",
            publication_status="needs_review",
        )

        self.assertEqual(receipt.publication_status, "needs_review")
        markdown = Path(receipt.published_path)
        bundle = KnowledgeBundle.from_markdown(markdown)
        self.assertEqual(bundle.asset.publication_status, "needs_review")
        self.assertIsNone(FilesystemKnowledgeReader(vault).get_current(bundle.asset.knowledge_id))

    def test_required_visual_table_and_equation_assets_reach_published_note(self) -> None:
        source_kinds = {asset.kind for asset in self.document.assets}
        self.assertTrue({"image", "table", "equation"}.issubset(source_kinds))

        receipt = TraceableReadingPipeline(ScriptedModel(), self.root / "asset-knowledge").run(self.document, domain="agents")
        markdown = Path(receipt.published_path).read_text(encoding="utf-8")
        self.assertRegex(markdown, r"!\[[^\]]+\]\(assets/")
        # Simple structured tables are published as safe GFM rather than raw
        # MinerU HTML.  Complex/ragged tables still use the source-image
        # fallback (covered by the neighbouring degradation test).
        self.assertIn("| Metric | Score |", markdown)
        self.assertNotIn("<table", markdown)
        self.assertIn("$$", markdown)
        evidence = json.loads(Path(receipt.evidence_index_path).read_text(encoding="utf-8"))
        self.assertTrue(evidence["asset_decisions"])
        self.assertTrue(all(item["decision"] in {"inline", "reference", "omit"} for item in evidence["asset_decisions"]))

    def test_wrong_writer_asset_kind_is_normalized_before_publication(self) -> None:
        class WrongAssetKindModel(ScriptedModel):
            def call_json(self, operation, prompt):
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    request = json.loads(prompt)
                    decision = next(item for item in request["asset_decisions"] if item["decision"] == "inline")
                    payload["sections"][0]["blocks"].append({
                        "block_id": "wrong-kind", "kind": "paragraph", "text": "错误标成正文的素材",
                        "finding_refs": decision["finding_refs"], "asset_ref": decision["asset_id"],
                        "caption": "素材", "explanation": "该素材用于解释论文论证。",
                        "explanation_role": decision["explanation_role"], "paragraph_purpose": "解释素材的论证作用",
                    })
                return payload

        receipt = TraceableReadingPipeline(WrongAssetKindModel(), self.root / "wrong-kind-knowledge").run(self.document, domain="agents")
        note = json.loads(Path(receipt.published_path).with_suffix(".note.json").read_text(encoding="utf-8"))
        blocks = [block for section in note["sections"] for block in section["blocks"]]
        target = next(block for block in blocks if block["block_id"] == "wrong-kind")
        self.assertIn(target["kind"], {"figure", "table", "equation"})

    def test_degraded_structured_table_falls_back_to_source_image(self) -> None:
        degraded = {asset.asset_id: asset for asset in self.document.assets if asset.caption == "Table 2"}
        self.assertEqual(len(degraded), 1)
        table_asset = next(iter(degraded.values()))
        self.assertEqual([rep.representation_type for rep in table_asset.representations], ["image"])

        receipt = TraceableReadingPipeline(ScriptedModel(), self.root / "fallback-knowledge").run(self.document, domain="agents")
        markdown = Path(receipt.published_path).read_text(encoding="utf-8")
        self.assertNotIn("| Metric ***** |", markdown)
        self.assertRegex(markdown, r"!\[[^\]]+\]\(assets/[0-9a-f]+\.png\)\n\n> 结构化解析质量不足")
        evidence = json.loads(Path(receipt.evidence_index_path).read_text(encoding="utf-8"))
        decision = next(item for item in evidence["asset_decisions"] if item["asset_id"] == table_asset.asset_id)
        self.assertEqual(decision["decision"], "inline")
        self.assertEqual(decision["representation_type"], "image")

    def test_asset_without_any_file_is_still_visible_and_omitted_explicitly(self) -> None:
        missing = {asset.asset_id: asset for asset in self.document.assets if asset.caption == "Figure 3"}
        self.assertEqual(len(missing), 1)
        figure_asset = next(iter(missing.values()))
        self.assertEqual(figure_asset.representations, ())
        self.assertFalse(figure_asset.path)

        receipt = TraceableReadingPipeline(ScriptedModel(), self.root / "omission-knowledge").run(self.document, domain="agents")
        evidence = json.loads(Path(receipt.evidence_index_path).read_text(encoding="utf-8"))
        decision = next(item for item in evidence["asset_decisions"] if item["asset_id"] == figure_asset.asset_id)
        self.assertEqual(decision["decision"], "omit")
        self.assertEqual(decision["reason"], "no_publishable_representation")
        markdown = Path(receipt.published_path).read_text(encoding="utf-8")
        self.assertNotIn("Ablation", markdown)

    def test_asset_quality_assessment_rejects_degraded_structured_content(self) -> None:
        self.assertFalse(assess_representation("html", "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>").usable)
        self.assertFalse(assess_representation("markdown", "| Metric ***** |").usable)
        self.assertFalse(assess_representation("latex", "\\sigma(x").usable)
        self.assertFalse(assess_representation("latex", "\\begin{aligned}x=1").usable)
        self.assertTrue(assess_representation("html", "<table><tr><th>a</th></tr><tr><td>b</td></tr></table>").usable)
        self.assertTrue(assess_representation("markdown", "| a | b |\n| --- | --- |\n| c | d |").usable)
        self.assertTrue(assess_representation("latex", "s=\\sigma(Wx+b)").usable)

    def test_simple_html_table_is_projected_to_gfm_but_spans_are_not_flattened(self) -> None:
        from research_pulse.traceable_reading.asset_quality import table_html_to_markdown

        markdown = table_html_to_markdown(
            "<table><tr><th>Metric</th><th>Score</th></tr><tr><td>A&amp;B</td><td>0.91</td></tr></table>"
        )
        self.assertEqual(markdown, "| Metric | Score |\n| --- | --- |\n| A&B | 0.91 |")
        self.assertIsNone(
            table_html_to_markdown(
                "<table><tr><th>Metric</th><th>Score</th></tr><tr><td rowspan='2'>A</td><td>0.91</td></tr></table>"
            )
        )

    def test_writer_uses_deterministic_section_interpretation_before_model_repair(self) -> None:
        class RepairingModel(ScriptedModel):
            def call_json(self, operation, prompt):
                if operation == "traceable_interpretation_write":
                    self.calls.append((operation, prompt))
                    request = json.loads(prompt)
                    finding_id = request["reading_answers"][0]["findings"][0]["finding_id"]
                    return {"target_section_id": request["sections"][0]["section_id"], "text": "这说明该机制的价值在于把验证信号贯穿整个治理过程，同时结论受已有证据边界约束。", "finding_refs": [finding_id]}
                if operation == "traceable_note_repair":
                    self.calls.append((operation, prompt))
                    request = json.loads(prompt)
                    draft = request["draft"]
                    for section in draft["sections"]:
                        for block in section["blocks"]:
                            if block["block_id"].endswith(":meaning"):
                                block["explanation_role"] = "interpretation"
                    return draft
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    for section in payload["sections"]:
                        for block in section["blocks"]:
                            if block["explanation_role"] == "interpretation":
                                block["explanation_role"] = "context"
                return payload

        model = RepairingModel()
        receipt = TraceableReadingPipeline(model, self.root / "repair-knowledge").run(self.document, domain="agents")
        self.assertEqual(receipt.publication_status, "published")
        self.assertEqual([name for name, _ in model.calls].count("traceable_interpretation_write"), 0)
        self.assertEqual([name for name, _ in model.calls].count("traceable_note_repair"), 0)

    def test_basic_gate_rejects_empty_and_cross_packet_references(self) -> None:
        model = ScriptedModel()
        survey = __import__("research_pulse.traceable_reading.pipeline", fromlist=["SurveyReader"]).SurveyReader(model).read(self.document)
        packets = build_reading_packets(self.document, survey.reading_plan)
        answers = __import__("research_pulse.traceable_reading.pipeline", fromlist=["FindingReader"]).FindingReader(model).read_all(packets)
        spans = resolve_source_spans(self.document, answers)
        packet_ids = {block.block_id for packet in packets for block in packet.blocks}
        valid = BasicEvidenceGate().evaluate(self.document, packet_ids, answers, spans)
        self.assertEqual((valid.reference_status, valid.semantic_status), ("valid", "not_evaluated"))

        bad_finding = replace(answers[0].findings[0], block_refs=())
        bad_answer = replace(answers[0], findings=(bad_finding,))
        invalid = BasicEvidenceGate().evaluate(self.document, packet_ids, (bad_answer,), ())
        self.assertEqual(invalid.reference_status, "invalid")
        self.assertEqual(invalid.semantic_status, "not_evaluated")
        self.assertNotIn("supported", repr(invalid))

    def test_target_asset_source_blocks_are_in_reading_packet(self) -> None:
        survey = SurveyReader(ScriptedModel()).read(self.document)
        packets = build_reading_packets(self.document, survey.reading_plan)
        targeted = {asset_id for obligation in survey.reading_plan.obligations for asset_id in obligation.target_asset_ids}
        for packet in packets:
            for asset in packet.assets:
                if asset.asset_id in targeted:
                    self.assertIn(asset.block_id, {block.block_id for block in packet.blocks})

    def test_source_span_splits_non_contiguous_blocks(self) -> None:
        first, last = self.document.blocks[0], self.document.blocks[-1]
        finding = Finding("finding:split", "联合事实", "联合", "req:x", "synthesis", (first.block_id, last.block_id))
        answer = ReadingAnswer("answer:x", "obligation:x", "answered", "完整答案", (finding,), (RequirementResult("req:x", "answered", (finding.finding_id,)),))
        spans = resolve_source_spans(self.document, (answer,))
        self.assertEqual(len(spans), 2)
        self.assertEqual({span.finding_id for span in spans}, {finding.finding_id})

    def test_core_reading_gap_prevents_writer(self) -> None:
        model = ScriptedModel()
        survey = SurveyReader(model).read(self.document)
        packets = build_reading_packets(self.document, survey.reading_plan)
        answers = __import__("research_pulse.traceable_reading.pipeline", fromlist=["FindingReader"]).FindingReader(model).read_all(packets)
        partial = tuple(answer for answer in answers if answer.obligation_id != "obligation:method")
        assessment = BasicEvidenceGate().evaluate(self.document, {block.block_id for packet in packets for block in packet.blocks}, partial, resolve_source_spans(self.document, partial))
        coverage = build_coverage(survey.reading_plan, partial, assessment)
        self.assertFalse(coverage.core_complete)

    def test_writer_unknown_finding_fails_safely(self) -> None:
        class BadWriterModel(ScriptedModel):
            def call_json(self, operation, prompt):
                if operation == "traceable_note_write":
                    return {"title": "bad", "sections": [{"section_id": "x", "title": "x", "blocks": [{"block_id": "x", "kind": "paragraph", "text": "bad", "finding_refs": ["finding:missing"], "asset_ref": None}]}]}
                return super().call_json(operation, prompt)

        with self.assertRaisesRegex(TraceableReadingError, "unknown_finding"):
            TraceableReadingPipeline(BadWriterModel(), self.root / "knowledge").run(self.document, domain="agents")

    def test_note_plan_rejects_finding_outside_reading_answers(self) -> None:
        class BadPlanModel(ScriptedModel):
            def call_json(self, operation, prompt):
                payload = super().call_json(operation, prompt)
                if operation.startswith("traceable_note_plan_"):
                    payload["sections"][0]["finding_refs"] = ["finding:not-read"]
                return payload

        with self.assertRaisesRegex(TraceableReadingError, "note_plan_unknown_finding"):
            TraceableReadingPipeline(BadPlanModel(), self.root / "bad-plan-knowledge").run(self.document, domain="agents")

    def test_note_plan_requires_method_walkthrough(self) -> None:
        class NoWalkthroughModel(ScriptedModel):
            def call_json(self, operation, prompt):
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_plan_method":
                    method = next(item for item in payload["sections"] if item["facet"] == "method")
                    method["worked_example"] = None
                return payload

        with self.assertRaisesRegex(TraceableReadingError, "note_plan_missing_worked_example"):
            TraceableReadingPipeline(NoWalkthroughModel(), self.root / "no-example-knowledge").run(self.document, domain="agents")

    def test_note_plan_requires_experiment_argument_unit(self) -> None:
        class NoExperimentUnitModel(ScriptedModel):
            def call_json(self, operation, prompt):
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_plan_experiments":
                    experiments = next(item for item in payload["sections"] if item["facet"] == "experiments")
                    experiments["experiment_units"] = []
                return payload

        with self.assertRaisesRegex(TraceableReadingError, "note_plan_missing_experiment_units"):
            TraceableReadingPipeline(NoExperimentUnitModel(), self.root / "no-experiment-unit-knowledge").run(self.document, domain="agents")

    def test_missing_writer_walkthrough_is_materialized_from_validated_plan(self) -> None:
        class MissingRenderedWalkthroughModel(ScriptedModel):
            def call_json(self, operation, prompt):
                if operation == "traceable_note_repair":
                    self.calls.append((operation, prompt))
                    return json.loads(prompt)["draft"]
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    method = next(item for item in payload["sections"] if item["section_id"] == "note:method")
                    for block in method["blocks"]:
                        block["plan_item_refs"] = []
                return payload

        receipt = TraceableReadingPipeline(MissingRenderedWalkthroughModel(), self.root / "unrendered-example-knowledge").run(self.document, domain="agents")
        note = json.loads(Path(receipt.published_path).with_suffix(".note.json").read_text(encoding="utf-8"))
        method = next(item for item in note["sections"] if item["section_id"] == "note:method")
        example = next(block for block in method["blocks"] if "worked_example" in block["plan_item_refs"])
        self.assertIn("贯穿示例", example["text"])
        self.assertEqual(example["finding_refs"], ["finding:method"])

    def test_incomplete_experiment_summary_is_expanded_from_argument_unit(self) -> None:
        class MissingExperimentUnitModel(ScriptedModel):
            def call_json(self, operation, prompt):
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    experiments = next(item for item in payload["sections"] if item["section_id"] == "note:experiments")
                    for block in experiments["blocks"]:
                        block["plan_item_refs"] = []
                return payload

        receipt = TraceableReadingPipeline(MissingExperimentUnitModel(), self.root / "expanded-experiment-knowledge").run(self.document, domain="agents")
        note = json.loads(Path(receipt.published_path).with_suffix(".note.json").read_text(encoding="utf-8"))
        experiments = next(item for item in note["sections"] if item["section_id"] == "note:experiments")
        unit = next(block for block in experiments["blocks"] if "experiment:main" in block["plan_item_refs"])
        for label in ("实验主张：", "比较方式：", "观察结果：", "这意味着：", "结论边界："):
            self.assertIn(label, unit["text"])

    def test_original_title_is_not_guessed_when_material_has_no_title(self) -> None:
        (self.material_root / "full.md").write_text("正文没有一级标题。", encoding="utf-8")
        content = json.loads((self.material_root / "content_list.json").read_text(encoding="utf-8"))
        content[0]["text_level"] = 0
        (self.material_root / "content_list.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        document = load_material_document(self.material_root, source_id="missing-title", source_url="https://example.test")
        with self.assertRaisesRegex(ValueError, "material_missing_original_title"):
            document.original_title()

    def test_empty_optional_asset_use_is_recorded_without_losing_answer(self) -> None:
        class EmptyAssetUseModel(ScriptedModel):
            def call_json(self, operation, prompt):
                payload = super().call_json(operation, prompt)
                if operation == "finding_read" and payload["answers"][0]["asset_uses"]:
                    payload["answers"][0]["asset_uses"][0]["finding_refs"] = []
                return payload

        receipt = TraceableReadingPipeline(EmptyAssetUseModel(), self.root / "asset-use-issue-knowledge").run(self.document, domain="agents")
        evidence = json.loads(Path(receipt.evidence_index_path).read_text(encoding="utf-8"))
        self.assertTrue(any(answer.get("asset_use_issues") for answer in evidence["answers"]))
        self.assertTrue(any(item["reason"] == "not_explained_by_reading_answer" for item in evidence["asset_decisions"]))

    def test_missing_section_interpretation_is_filled_from_note_plan(self) -> None:
        class MissingInterpretationModel(ScriptedModel):
            def call_json(self, operation, prompt):
                if operation == "traceable_note_repair":
                    self.calls.append((operation, prompt))
                    return json.loads(prompt)["draft"]
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    experiment = next(item for item in payload["sections"] if item["section_id"] == "note:experiments")
                    meaning = next(item for item in experiment["blocks"] if item["block_id"].endswith(":meaning"))
                    meaning["explanation_role"] = "evidence"
                    meaning["text"] = "实验结果及其适用范围需要结合计划理解。"
                return payload

        receipt = TraceableReadingPipeline(MissingInterpretationModel(), self.root / "section-interpretation-knowledge").run(self.document, domain="agents")
        note = json.loads(Path(receipt.published_path).with_suffix(".note.json").read_text(encoding="utf-8"))
        experiment = next(item for item in note["sections"] if item["section_id"] == "note:experiments")
        self.assertIn("interpretation", {block["explanation_role"] for block in experiment["blocks"]})

    def test_reverse_outline_rejects_generic_paragraph_purpose(self) -> None:
        class GenericPurposeModel(ScriptedModel):
            def call_json(self, operation, prompt):
                if operation == "traceable_note_repair":
                    self.calls.append((operation, prompt))
                    return json.loads(prompt)["draft"]
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    payload["sections"][0]["blocks"][0]["paragraph_purpose"] = "概括"
                return payload

        with self.assertRaisesRegex(TraceableReadingError, "reverse_outline_generic_purpose"):
            TraceableReadingPipeline(GenericPurposeModel(), self.root / "generic-purpose-knowledge").run(self.document, domain="agents")

    def test_writer_binds_unlinked_boundary_only_from_section_plan(self) -> None:
        class UnlinkedBoundaryModel(ScriptedModel):
            def call_json(self, operation, prompt):
                if operation == "traceable_note_repair":
                    self.calls.append((operation, prompt))
                    request = json.loads(prompt)
                    allowed = {item["section_id"]: item["finding_refs"] for item in request["note_plan"]["sections"]}
                    draft = request["draft"]
                    for section in draft["sections"]:
                        for block in section["blocks"]:
                            if not block["finding_refs"]:
                                block["finding_refs"] = allowed[section["section_id"]]
                    return draft
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    payload["sections"][0]["blocks"][0]["finding_refs"] = []
                return payload

        model = UnlinkedBoundaryModel()
        receipt = TraceableReadingPipeline(model, self.root / "unlinked-repair-knowledge").run(self.document, domain="agents")
        self.assertEqual(receipt.publication_status, "published")
        self.assertEqual([name for name, _ in model.calls].count("traceable_note_repair"), 0)

    def test_english_only_note_is_rejected_after_bounded_repair(self) -> None:
        class EnglishOnlyModel(ScriptedModel):
            def call_json(self, operation, prompt):
                if operation == "traceable_note_repair":
                    self.calls.append((operation, prompt))
                    return json.loads(prompt)["draft"]
                payload = super().call_json(operation, prompt)
                if operation == "traceable_note_write":
                    payload["title"] = "English note"
                    for section in payload["sections"]:
                        section["title"] = "English section"
                        section["reader_question"] = "What does the paper show?"
                        for block in section["blocks"]:
                            block["text"] = "English explanation only."
                            block["explanation"] = "English asset explanation."
                return payload

        with self.assertRaisesRegex(TraceableReadingError, "non_chinese"):
            TraceableReadingPipeline(EnglishOnlyModel(), self.root / "english-knowledge").run(self.document, domain="agents")


if __name__ == "__main__":
    unittest.main()
