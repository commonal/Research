from __future__ import annotations

from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock, PaperReader, ReadingIntent


class _FullPaperWriterModel:
    text_model = "text-test-model"
    vision_model = None

    def read_full_paper(self, request: object) -> dict:
        return {
            "thesis": "A broker audits task-conditioned authority before and after execution.",
            "argument_chain": {"problem": "Agents may exercise authority beyond the task."},
            "experiments": [{"experiment_id": "experiment:main"}],
            "must_preserve_facts": [{"fact_id": "fact:result", "eligible": True}],
            "limitations": [{"limitation_id": "limitation:scope"}],
            "coverage": {name: {"status": "complete", "missing": []} for name in ("background", "problem", "prior_gap", "mechanism", "experiment", "boundary")},
            "source_facts": [
                {"fact_id": "f-problem", "facet": "problem", "statement": "Agents exercise authority beyond the task.", "source_block_ids": ["problem"]},
                {"fact_id": "f-method", "facet": "method", "statement": "A broker audits actions before and after execution.", "source_block_ids": ["method"]},
                {"fact_id": "f-result", "facet": "experiment", "statement": "Safe success rises from 64% to 98%.", "source_block_ids": ["result"]},
                {"fact_id": "f-limit", "facet": "limitation", "statement": "The method does not replace sandboxing.", "source_block_ids": ["limit"]},
            ],
            "source_limitations": [],
            "material_unknowns": [],
            "visual_candidates": [],
            "sections": [{
                "section_id": "main",
                "heading": "论文主线",
                "reader_question": "How does the method work and what does the experiment show?",
                "prerequisite_bridges": ["Explain task success before permission scope."],
                "reasoning_steps": ["Connect pre-audit and post-audit to the result."],
                "experiment_slots": ["experiment:main"],
                "asset_jobs": [],
                "transition_in": "",
                "transition_out": "",
                "stop_conditions": ["All core obligations are explained."],
                "evidence_handles": ["problem", "method", "result", "limit"],
                "coverage_obligation_ids": ["argument:problem", "experiment:main", "fact:result", "limitation:scope"],
            }],
        }

    def interpret_visual(self, request: object) -> str:
        raise AssertionError("This writer fixture has no visual asset.")


class V2WriterContractTests(TestCase):
    def test_writer_repair_rejects_a_full_document_replacement(self) -> None:
        class WholeDocumentRepairModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [{
                    "statement": "The exact method for calculating z_req(x) is not described.",
                    "priority": "medium",
                }]
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                return {"markdown": "# 被整篇替换的内容\n\n论文没有说明 z_req(x)。\n"}

        original = "# 论文精读\n\n## 方法\n\n方法正文必须保留。\n\n## 结论\n\n结论正文必须保留。\n"
        model = WholeDocumentRepairModel()
        result = PaperReader(model, writer=lambda payload: original).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(model.repair_calls, 1)
        self.assertEqual(result.draft.markdown, original)
        self.assertEqual(result.receipt.status, "failed")
        self.assertTrue(any("unexpressed_obligation" in item for item in result.receipt.unsupported_writer_claims))

    def test_chinese_unknown_explanation_does_not_trigger_duplicate_repair(self) -> None:
        class UnknownAwareWriterModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [
                    {
                        "statement": "Exact implementation details of the broker's deterministic verifiers are not fully specified.",
                        "priority": "medium",
                    },
                    {
                        "statement": "The method for assigning six-dimensional risk vector components is not detailed.",
                        "priority": "medium",
                    },
                ]
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                raise AssertionError("Equivalent Chinese unknowns are already present.")

        model = UnknownAwareWriterModel()
        result = PaperReader(
            model,
            writer=lambda payload: (
                "# 论文精读\n\n"
                "代理会越权，安全成功率从 64% 提升到 98%。"
                "论文尚未说明代理的确定性验证器实现，也未详细说明六维风险向量各分量的赋值方法。\n"
            ),
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (0, 0))
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_writer_repair_applies_only_returned_section_patch(self) -> None:
        class PatchingWriterModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [{
                    "statement": "The exact method for calculating z_req(x) is not described.",
                    "priority": "medium",
                }]
                base = response["sections"][0]
                response["sections"] = [
                    {
                        **base,
                        "section_id": "sec_method",
                        "heading": "方法",
                        "reader_question": "方法如何回应越权问题？",
                        "evidence_handles": ["problem", "method", "result"],
                        "coverage_obligation_ids": ["argument:problem", "experiment:main", "fact:result"],
                    },
                    {
                        **base,
                        "section_id": "sec_limitations",
                        "heading": "局限",
                        "reader_question": "论文的证据边界是什么？",
                        "experiment_slots": [],
                        "evidence_handles": ["limit"],
                        "coverage_obligation_ids": ["limitation:scope"],
                    },
                    {
                        **base,
                        "section_id": "sec_conclusion",
                        "heading": "结论",
                        "reader_question": "如何收束论文结论？",
                        "experiment_slots": [],
                        "evidence_handles": ["limit"],
                        "coverage_obligation_ids": [],
                    },
                ]
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "## 局限",
                        "obligation_ids": ["unknown:full-unknown-1"],
                        "append_markdown": "论文没有说明 z_req(x) 的具体计算方法。",
                    }]
                }

        original = "# 论文精读\n\n## 方法\n\n方法正文保持不变。\n\n## 局限\n\n现有局限正文。\n\n## 结论\n\n结论正文保持不变。\n"
        model = PatchingWriterModel()
        result = PaperReader(model, writer=lambda payload: original).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        self.assertEqual(model.last_repair_request["affected_sections"], ("sec_limitations",))
        self.assertIn("现有局限正文。\n\n论文没有说明 z_req(x) 的具体计算方法。", result.draft.markdown)
        self.assertIn("## 方法\n\n方法正文保持不变。", result.draft.markdown)
        self.assertIn("## 结论\n\n结论正文保持不变。", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_writer_repair_replaces_an_overlapping_experiment_paragraph_instead_of_repeating_it(self) -> None:
        class OverlapRepairModel(_FullPaperWriterModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [{
                    "statement": "The exact method for calculating z_req(x) is not described.",
                    "priority": "medium",
                }]
                return response

            def repair_writer(self, request: object) -> dict:
                return {
                    "section_patches": [{
                        "after_heading": "# 论文主线",
                        "obligation_ids": ["unknown:full-unknown-1"],
                        "append_markdown": (
                            "在 500 个留出任务、2,896 个 episode 中，安全成功率从 64% 提升到 98%，"
                            "说明越权下降并非源于不作为。\n\n"
                            "论文没有说明 z_req(x) 的具体计算方法。"
                        ),
                    }]
                }

        original = (
            "# 论文主线\n\n"
            "代理可能在完成任务时越权，broker 会在执行前后审计动作。\n\n"
            "在 500 个留出任务中，安全成功率从 64% 提升到 98%。\n\n"
            "该方法不能替代沙箱。\n"
        )
        result = PaperReader(OverlapRepairModel(), writer=lambda payload: original).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(result.receipt.writer_repair_calls, 1)
        self.assertEqual(result.draft.markdown.count("安全成功率从 64% 提升到 98%"), 1)
        self.assertIn("2,896 个 episode", result.draft.markdown)
        self.assertIn("论文没有说明 z_req(x) 的具体计算方法", result.draft.markdown)

    def test_writer_repair_uses_section_id_when_contract_and_chinese_headings_differ(self) -> None:
        class TranslatedHeadingRepairModel(_FullPaperWriterModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["must_preserve_facts"] = [{
                    "fact_id": "result",
                    "eligible": True,
                    "statement": "Safe success rises from 64% to 98%.",
                }]
                return response

            def repair_writer(self, request: object) -> dict:
                return {
                    "section_patches": [{
                        "section_id": "main",
                        "after_heading": "# 论文主线",
                        "obligation_ids": ["fact:result"],
                        "append_markdown": "安全成功率从 64% 提升到 98%。",
                    }]
                }

        original = "# 中文精读\n\n代理可能越权，broker 会执行审计；该方法不能替代沙箱。\n"
        result = PaperReader(TranslatedHeadingRepairModel(), writer=lambda payload: original).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(result.receipt.status, "completed")
        self.assertIn("安全成功率从 64% 提升到 98%", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_explicit_named_unknown_anchor_survives_chinese_explanation(self) -> None:
        class NamedAnchorRepairModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [{
                    "statement": "Exact implementation details of the broker's pre- and post-execution auditing are not fully specified.",
                    "priority": "medium",
                    "anchor_terms": ["broker", "pre-execution auditing", "post-execution auditing"],
                }]
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                return {
                    "section_patches": [{
                        "section_id": "main",
                        "after_heading": "# 论文主线",
                        "obligation_ids": ["unknown:full-unknown-1"],
                        "append_markdown": "论文没有说明 broker 的预执行与后执行审计细节。",
                    }]
                }

        model = NamedAnchorRepairModel()
        result = PaperReader(
            model,
            writer=lambda payload: (
                "# 中文精读\n\n代理可能越权，主机代理会进行审计，安全成功率从 64% 提升到 98%，"
                "但不能替代沙箱；其预执行与后执行审计细节没有说明。\n"
            ),
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        self.assertIn("broker", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_direct_writer_removes_numeric_repetition_outside_the_ledger_owned_section(self) -> None:
        class SectionOwnedExperimentModel(_FullPaperWriterModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                base = response["sections"][0]
                response["must_preserve_facts"] = [{
                    "fact_id": "result",
                    "eligible": True,
                    "statement": "Safe success rises from 64% to 98%.",
                }]
                response["sections"] = [
                    {
                        **base,
                        "section_id": "sec_method",
                        "heading": "Method",
                        "reader_question": "How does the method work?",
                        "experiment_slots": [],
                        "evidence_handles": ["problem", "method", "limit"],
                        "coverage_obligation_ids": ["argument:problem", "limitation:scope"],
                    },
                    {
                        **base,
                        "section_id": "sec_results",
                        "heading": "Results",
                        "reader_question": "What does the main experiment establish?",
                        "evidence_handles": ["result"],
                        "coverage_obligation_ids": ["experiment:main", "fact:result"],
                    },
                ]
                return response

        repeated = "安全成功率从 64% 提升到 98%，说明训练并非依靠拒绝行动。"
        markdown = (
            "# 中文精读\n\n"
            "<!-- rp-section:sec_method -->\n## 方法\n\n代理可能越权，broker 会执行审计，该方法不能替代沙箱。\n\n"
            f"{repeated}\n\n"
            "<!-- rp-section:sec_results -->\n## 结果\n\n"
            f"{repeated}\n"
        )
        result = PaperReader(SectionOwnedExperimentModel(), writer=lambda payload: markdown).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(result.draft.markdown.count(repeated), 1)
        self.assertNotIn(repeated, result.draft.markdown.split("## 结果", 1)[0])
        self.assertIn(repeated, result.draft.markdown.split("## 结果", 1)[1])

    def test_scattered_experiment_setup_is_repaired_into_its_owner_without_deleting_the_original(self) -> None:
        class ScatteredExperimentModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                base = response["sections"][0]
                response["experiments"] = [{
                    "experiment_id": "experiment:main",
                    "setup": "206 tasks with 8 generations per task.",
                    "comparison": "Base vs checkpoint 500.",
                    "results": "Safe success rises from 61.65% to 96.91%.",
                    "boundary": "Internal evaluation only.",
                }]
                response["sections"] = [
                    {
                        **base,
                        "section_id": "sec_method",
                        "heading": "Method",
                        "reader_question": "How does the method work?",
                        "experiment_slots": [],
                        "evidence_handles": ["problem", "method", "limit"],
                        "coverage_obligation_ids": ["argument:problem", "limitation:scope"],
                    },
                    {
                        **base,
                        "section_id": "sec_results",
                        "heading": "Results",
                        "reader_question": "How is the method evaluated?",
                        "evidence_handles": ["result"],
                        "coverage_obligation_ids": ["experiment:main", "fact:result"],
                    },
                ]
                return response

            def repair_writer(self, request: dict) -> dict:
                self.repair_calls += 1
                return {"section_patches": [{
                    "section_id": "sec_results",
                    "after_heading": "## Results",
                    "obligation_ids": ["experiment:main"],
                    "append_markdown": (
                        "内部评估使用 206 个任务、每个任务生成 8 次，比较 Base 与 checkpoint 500；"
                        "安全成功率从 61.65% 提升到 96.91%，且只说明内部评估。"
                    ),
                }]}

        model = ScatteredExperimentModel()
        markdown = (
            "# 中文精读\n\n<!-- rp-section:sec_method -->\n## 方法\n\n代理可能越权，broker 会执行审计，该方法不能替代沙箱。\n\n"
            "实验在 206 个任务上每个任务生成 8 次，并比较 Base 与 checkpoint 500。\n\n"
            "<!-- rp-section:sec_results -->\n## 结果\n\n安全成功率从 61.65% 提升到 96.91%。\n"
        )
        result = PaperReader(model, writer=lambda payload: markdown).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "206 tasks use 8 generations. Safe success rises from 61.65% to 96.91%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (0, 0))
        # Scattered-but-present content is accepted under the whole-note gate: the
        # experiment setup may stay in the method section (no re-graft into its
        # owner, no repair), as long as every key number survives in the note.
        self.assertIn(result.receipt.status, ("completed", "bounded"))
        self.assertIn("206 个任务", result.draft.markdown)
        self.assertIn("61.65%", result.draft.markdown)
        self.assertIn("96.91%", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_ambiguous_unmarked_chinese_sections_are_not_destructively_reassigned(self) -> None:
        class AmbiguousSectionsModel(_FullPaperWriterModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                base = response["sections"][0]
                response["experiments"] = [{
                    "experiment_id": "experiment:main",
                    "setup": "206 tasks with 8 generations per task.",
                    "results": "Safe success rises from 61.65% to 96.91%.",
                }]
                response["sections"] = [
                    {**base, "section_id": "sec_method", "heading": "Method", "experiment_slots": [], "coverage_obligation_ids": ["argument:problem", "limitation:scope"]},
                    {**base, "section_id": "sec_results", "heading": "Results", "coverage_obligation_ids": ["experiment:main", "fact:result"]},
                ]
                return response

        setup_sentence = "实验在 206 个任务上每个任务生成 8 次。"
        markdown = (
            "# 中文精读\n\n## 方法\n\n代理可能越权，broker 会执行审计，该方法不能替代沙箱。\n\n"
            f"{setup_sentence}\n\n## 结果\n\n安全成功率从 61.65% 提升到 96.91%。\n"
        )
        result = PaperReader(AmbiguousSectionsModel(), writer=lambda payload: markdown).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "206 tasks use 8 generations. Safe success rises from 61.65% to 96.91%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertIn(setup_sentence, result.draft.markdown)

    def test_writer_repair_runs_once_per_affected_section_in_one_repair_round(self) -> None:
        class TwoSectionRepairModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_requests: list[dict] = []

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                base = response["sections"][0]
                response["experiments"] = [{"experiment_id": "exp_internal"}, {"experiment_id": "exp_external"}]
                response["must_preserve_facts"] = [
                    {"fact_id": "internal", "eligible": True, "statement": "Safe success rises from 64% to 98%."},
                    {"fact_id": "external", "eligible": True, "statement": "Over-privilege falls from 45.2% to 37.9%."},
                ]
                response["sections"] = [
                    {
                        **base,
                        "section_id": "sec_internal",
                        "heading": "Internal Results",
                        "reader_question": "What happens internally?",
                        "evidence_handles": ["problem", "method", "result"],
                        "coverage_obligation_ids": ["argument:problem", "experiment:exp_internal", "fact:internal"],
                    },
                    {
                        **base,
                        "section_id": "sec_external",
                        "heading": "External Results",
                        "reader_question": "What transfers externally?",
                        "evidence_handles": ["result", "limit"],
                        "coverage_obligation_ids": ["experiment:exp_external", "fact:external", "limitation:scope"],
                    },
                ]
                return response

            def repair_writer(self, request: dict) -> dict:
                self.repair_requests.append(request)
                obligation_id = request["missing_obligations"][0]
                if obligation_id == "fact:internal":
                    return {"section_patches": [{
                        "section_id": "sec_internal",
                        "after_heading": "## Internal Results",
                        "obligation_ids": [obligation_id],
                        "append_markdown": "安全成功率从 64% 提升到 98%。",
                    }]}
                return {"section_patches": [{
                    "section_id": "sec_external",
                    "after_heading": "## External Results",
                    "obligation_ids": [obligation_id],
                    "append_markdown": "过度权限从 45.2% 降至 37.9%。",
                }]}

        model = TwoSectionRepairModel()
        result = PaperReader(
            model,
            writer=lambda payload: (
                "# 中文精读\n\n## 内部结果\n\n代理可能越权，broker 进行审计。\n\n"
                "## 外部结果\n\n该方法不能替代沙箱。\n"
            ),
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%, while over-privilege falls from 45.2% to 37.9%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((len(model.repair_requests), result.receipt.writer_repair_calls), (2, 2))
        self.assertTrue(all(len(request["missing_obligations"]) == 1 for request in model.repair_requests))
        self.assertIn("64% 提升到 98%", result.draft.markdown)
        self.assertIn("45.2% 降至 37.9%", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_writer_repair_gets_one_bounded_follow_up_for_obligations_missed_in_the_first_response(self) -> None:
        class PartialRepairModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_requests: list[dict] = []

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["must_preserve_facts"] = [
                    {"fact_id": "setup", "eligible": True, "statement": "The evaluation uses 206 tasks, 1,648 episodes, and 8 generations per task."},
                    {"fact_id": "result", "eligible": True, "statement": "Across 206 tasks and 1,648 episodes, safe success rises from 61.65% to 96.91%."},
                ]
                response["sections"][0]["coverage_obligation_ids"] = [
                    "argument:problem", "experiment:main", "fact:setup", "fact:result", "limitation:scope",
                ]
                return response

            def repair_writer(self, request: dict) -> dict:
                self.repair_requests.append(request)
                obligation_id = request["missing_obligations"][0]
                prose = (
                    "评估使用 206 个任务、1,648 个回合，每个任务生成 8 次。"
                    if obligation_id == "fact:setup"
                    else "在 206 个任务的 1,648 个回合中，安全成功率从 61.65% 提升到 96.91%。"
                )
                return {"section_patches": [{
                    "section_id": "main",
                    "after_heading": "# 论文主线",
                    "obligation_ids": [obligation_id],
                    "append_markdown": prose,
                }]}

        model = PartialRepairModel()
        result = PaperReader(
            model,
            writer=lambda payload: "# 论文主线\n\n代理可能越权，broker 会审计动作，且该方法不能替代沙箱。\n",
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "206 tasks use 8 generations. Safe success rises from 61.65% to 96.91%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((len(model.repair_requests), result.receipt.writer_repair_calls), (2, 2))
        self.assertEqual(model.repair_requests[0]["missing_obligations"], ("fact:setup", "fact:result"))
        self.assertEqual(model.repair_requests[1]["missing_obligations"], ("fact:result",))
        self.assertIn("206 个任务", result.draft.markdown)
        self.assertIn("每个任务生成 8 次", result.draft.markdown)
        self.assertIn("61.65% 提升到 96.91%", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_table_guard_does_not_treat_an_adjacent_single_percentage_as_a_row_value(self) -> None:
        class TableContextModel(_FullPaperWriterModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{
                    "block_id": "checkpoint-table",
                    "decision": "reference",
                    "reason": "The table supports checkpoint progression.",
                }]
                response["sections"][0]["evidence_handles"].append("checkpoint-table")
                return response

        paper = CanonicalPaperIR(
            "paper-writer",
            "Writer",
            (
                PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%. Most of the total gain, about 98%, appears by checkpoint 500.", 3),
                PaperIRBlock(
                    "checkpoint-table",
                    "table",
                    "Results",
                    "Checkpoint results.",
                    4,
                    table_html=(
                        "<table><tr><td>Policy</td><td>Safe success</td><td>Over-privilege</td></tr>"
                        "<tr><td>Base</td><td>61.65%</td><td>3.88%</td></tr>"
                        "<tr><td>Checkpoint 500</td><td>96.91%</td><td>1.52%</td></tr></table>"
                    ),
                ),
                PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 5),
            ),
        )
        markdown = (
            "# 中文精读\n\n代理可能越权，broker 会执行审计；安全成功率从 64% 提升到 98%，"
            "但该方法不能替代沙箱。Base 与 Checkpoint 500 的进展显示，约 98% 的总体改进在早期完成。\n"
        )
        result = PaperReader(TableContextModel(), writer=lambda payload: markdown).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            paper,
            ReadingIntent(),
        )

        self.assertEqual(result.receipt.status, "completed")
        self.assertIn("约 98% 的总体改进", result.draft.markdown)
        self.assertFalse(any("unsupported_writer_claim:table" in item for item in result.receipt.unsupported_writer_claims))

    def test_named_material_unknowns_survive_chinese_writer(self) -> None:
        class RepairingWriterModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [
                    {
                        "statement": "Exact implementation details of the broker's deterministic verifiers are not fully specified.",
                        "priority": "medium",
                    },
                    {
                        "statement": "The method for assigning six-dimensional risk vector components is not detailed.",
                        "priority": "medium",
                    },
                ]
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "# 论文精读",
                        "obligation_ids": ("unknown:full-unknown-1", "unknown:full-unknown-2"),
                        "append_markdown": "论文未完整说明 broker 的确定性验证器实现，也未详细说明六维风险向量的分量赋值方法。",
                    }]
                }

        model = RepairingWriterModel()
        result = PaperReader(
            model,
            writer=lambda payload: "# 论文精读\n\n代理会越权，方法通过审计降低风险，但实现细节未展开。\n",
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        evidence = model.last_repair_request["missing_obligation_evidence"]
        self.assertEqual(tuple(item["kind"] for item in evidence), ("material_unknown", "material_unknown"))
        self.assertIn("broker", evidence[0]["anchor_terms"])
        self.assertIn("six-dimensional risk vector", evidence[1]["anchor_terms"])
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_validated_section_contracts_bypass_legacy_note_planner(self) -> None:
        planner_calls = 0
        writer_payloads: list[dict] = []

        def legacy_planner(_payload: dict) -> dict:
            nonlocal planner_calls
            planner_calls += 1
            raise AssertionError("Validated section contracts must go directly to the v2 Writer.")

        def writer(payload: dict) -> str:
            writer_payloads.append(payload)
            return "# 论文精读\n\n代理会越权；broker 在执行前后审计动作，安全成功率从 64% 提升到 98%，但这不替代沙箱。\n"

        result = PaperReader(
            _FullPaperWriterModel(),
            note_planner=legacy_planner,
            writer=writer,
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(planner_calls, 0)
        self.assertEqual(len(writer_payloads), 1)
        sections = writer_payloads[0]["note_plan"]["sections"]
        self.assertEqual(sections[0]["section_id"], "main")
        self.assertIn("reader_question", sections[0])
        self.assertIn("reasoning_steps", sections[0])
        self.assertEqual(result.receipt.status, "completed")

    def test_missing_material_unknown_triggers_one_writer_repair(self) -> None:
        class RepairingWriterModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [{
                    "statement": "The exact method for calculating the sufficient envelope z_req(x) is not described.",
                    "priority": "medium",
                }]
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "# 论文精读",
                        "obligation_ids": ["unknown:full-unknown-1"],
                        "append_markdown": "充分权限包络 z_req(x) 的具体构造方法未描述，这是当前证据边界。",
                    }]
                }

        model = RepairingWriterModel()
        result = PaperReader(
            model,
            writer=lambda payload: (
                "# 论文精读\n\n"
                "代理会越权；broker 在执行前后审计动作，安全成功率从 64% 提升到 98%。"
                "每个任务都有充分权限包络 z_req(x)。\n"
            ),
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        repair_evidence = model.last_repair_request["missing_obligation_evidence"][0]
        self.assertEqual(repair_evidence["kind"], "material_unknown")
        self.assertIn("z_req(x)", repair_evidence["statement"])
        self.assertIn("未描述", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_missing_inline_visual_obligation_triggers_one_writer_repair(self) -> None:
        class RepairingWriterModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{
                    "block_id": "method-figure",
                    "decision": "inline",
                    "requires_pixels": False,
                    "reason": "The architecture flow is required to explain the mechanism.",
                }]
                response["sections"][0]["asset_jobs"] = [{"asset_id": "asset:method-figure", "job": "Explain the architecture flow."}]
                response["sections"][0]["coverage_obligation_ids"].append("asset:method-figure")
                return response

            def interpret_visual(self, request: object) -> str:
                return "The diagram shows policy actions flowing through the host controller to execution and telemetry, with a training-update loop back to the policy."

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "# 论文精读",
                        "obligation_ids": ["asset:method-figure"],
                        "append_markdown": "方法图显示策略动作经过主机控制器进入执行与遥测，并由训练更新箭头返回策略。",
                    }]
                }

        model = RepairingWriterModel()
        paper = CanonicalPaperIR(
            "paper-writer",
            "Writer",
            (
                PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                PaperIRBlock("method-figure", "figure", "Method Architecture", "Figure 1. Architecture flow.", 3, caption="Figure 1. Architecture flow.", image_path="safe.png", safe_image=True),
                PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 4),
                PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 5),
            ),
        )

        result = PaperReader(
            model,
            writer=lambda payload: "# 论文精读\n\n问题与实验结论完整，但这里没有解释视觉对象。\n",
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            paper,
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        repair_evidence = model.last_repair_request["missing_obligation_evidence"][0]
        self.assertEqual(repair_evidence["section_id"], "main")
        self.assertIn("training-update loop", repair_evidence["visual_interpretation"])
        self.assertIn("方法图", result.draft.markdown)
        self.assertNotIn("figure:method-figure", result.draft.markdown)

    def test_inline_visual_preserves_a_named_component_label(self) -> None:
        class NamedVisualRepairModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{
                    "block_id": "method-figure",
                    "decision": "inline",
                    "requires_pixels": False,
                    "reason": "The Host broker is central to the method loop.",
                }]
                response["sections"][0]["asset_jobs"] = [{"asset_id": "asset:method-figure", "job": "Explain the Host broker loop."}]
                response["sections"][0]["coverage_obligation_ids"].append("asset:method-figure")
                return response

            def interpret_visual(self, request: object) -> str:
                return "The diagram shows the Qwen policy sending actions to the Host broker before execution and telemetry."

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                return {
                    "section_patches": [{
                        "section_id": "main",
                        "after_heading": "# 论文主线",
                        "obligation_ids": ["asset:method-figure"],
                        "append_markdown": "图1中的 Host broker（主机代理）在执行前接收并审计动作。",
                    }]
                }

        model = NamedVisualRepairModel()
        paper = CanonicalPaperIR(
            "paper-writer",
            "Writer",
            (
                PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                PaperIRBlock("method-figure", "figure", "Method", "Figure 1. Architecture flow.", 3, caption="Figure 1. Architecture flow.", image_path="safe.png", safe_image=True),
                PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 4),
                PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 5),
            ),
        )
        result = PaperReader(
            model,
            writer=lambda payload: (
                "# 中文精读\n\n图1显示主机代理连接策略、执行环境与遥测；"
                "代理可能越权，安全成功率从 64% 提升到 98%，但该方法不能替代沙箱。\n"
            ),
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            paper,
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        self.assertIn("Host broker", result.draft.markdown)
        self.assertEqual(result.draft.markdown.count("图1"), 1)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_missing_inline_formula_obligation_triggers_local_writer_repair(self) -> None:
        class FormulaRepairingModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["sections"][0]["heading"] = "方法"
                response["sections"][0]["asset_jobs"] = [{
                    "asset_id": "asset:formula-bundle",
                    "job": "Explain the state update equation and its terms.",
                }]
                response["sections"][0]["coverage_obligation_ids"].append("asset:formula-bundle")
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "## 方法",
                        "obligation_ids": ["asset:state-equation"],
                        "append_markdown": "关键状态更新公式为 $h_t = A h_{t-1} + B x_t$：上一状态经 A 传播，当前输入经 B 写入。",
                    }]
                }

            def repair_section_plan(self, request: object) -> dict:
                return {}

        model = FormulaRepairingModel()
        paper = CanonicalPaperIR(
            "paper-writer",
            "Writer",
            (
                PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                PaperIRBlock("method", "paragraph", "Method", "A denotes state propagation and B denotes input injection.", 2),
                PaperIRBlock("formula-bundle", "formula", "Method", "h_t = A h_{t-1} + B x_t (1a) y_t = C h_t (2a)", 3, parse_status="unparsed"),
                PaperIRBlock("state-equation", "formula", "Method", r"h_t = A h_{t-1} + B x_t \tag{1a}", 4, latex=r"h_t = A h_{t-1} + B x_t \tag{1a}"),
                PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 5),
                PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 6),
            ),
        )

        result = PaperReader(
            model,
            writer=lambda payload: "# 论文精读\n\n## 方法\n\n方法使用状态空间模型，但没有给出关键状态更新关系。\n",
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            paper,
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        repair_evidence = model.last_repair_request["missing_obligation_evidence"][0]
        self.assertEqual(repair_evidence["kind"], "inline_formula")
        self.assertIn("h_t = A h_{t-1} + B x_t", repair_evidence["formula"])
        self.assertIn("$h_t = A h_{t-1} + B x_t$", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_missing_numeric_must_preserve_fact_triggers_local_writer_repair(self) -> None:
        class NumericFactRepairingModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["must_preserve_facts"].append({
                    "fact_id": "selective-copying",
                    "statement": "S6 achieves 99.8% accuracy while the ungated S4 baseline achieves 18.3%.",
                    "eligible": True,
                    "source_block_ids": ["result"],
                })
                response["sections"][0]["heading"] = "实验"
                response["sections"][0]["coverage_obligation_ids"].append("fact:selective-copying")
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "## 实验",
                        "obligation_ids": ["fact:selective-copying"],
                        "append_markdown": "在选择性复制中，S6 的准确率为 99.8%，无门控 S4 基线为 18.3%。",
                    }]
                }

        model = NumericFactRepairingModel()
        result = PaperReader(
            model,
            writer=lambda payload: "# 论文精读\n\n## 实验\n\n选择机制在合成任务上明显更好，但正文漏了主结果。\n",
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "S6 achieves 99.8% accuracy while the ungated S4 baseline achieves 18.3%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        evidence = model.last_repair_request["missing_obligation_evidence"][0]
        self.assertEqual(evidence["kind"], "must_preserve_fact")
        self.assertEqual(evidence["exact_numeric_tokens"], ("99.8%", "18.3%"))
        self.assertIn("99.8%", result.draft.markdown)
        self.assertIn("18.3%", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_missing_named_mechanism_fact_triggers_local_writer_repair(self) -> None:
        class MechanismRepairingModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["must_preserve_facts"].append({
                    "fact_id": "broker-loop",
                    "statement": "A broker audits actions before and after execution.",
                    "eligible": True,
                    "source_block_ids": ["method"],
                    "mechanism_terms": ["broker"],
                })
                response["sections"][0]["coverage_obligation_ids"].append("fact:broker-loop")
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "## 方法",
                        "obligation_ids": ["fact:broker-loop"],
                        "append_markdown": "方法通过 broker 在执行前后审计动作。",
                    }]
                }

        model = MechanismRepairingModel()
        result = PaperReader(
            model,
            writer=lambda payload: "# 论文精读\n\n## 方法\n\n方法通过一个控制组件降低越权风险。\n",
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        evidence = model.last_repair_request["missing_obligation_evidence"][0]
        self.assertEqual(evidence["kind"], "must_preserve_fact")
        self.assertEqual(evidence["required_terms"], ("broker",))
        self.assertIn("broker", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_experiment_obligation_requires_its_setup_and_result_terms(self) -> None:
        class ExperimentRepairingModel(_FullPaperWriterModel):
            def __init__(self) -> None:
                self.repair_calls = 0
                self.last_repair_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["experiments"] = [{
                    "experiment_id": "main",
                    "setup": ["Held-out ToolPrivBench tasks."],
                    "comparison": ["Base versus audited policy."],
                    "results": ["Safe success rises from 64% to 98%."],
                    "interpretation": "The audited policy succeeds more safely.",
                    "boundary": "This does not replace sandboxing.",
                    "source_block_ids": ["result", "limit"],
                    "required_terms": ["ToolPrivBench"],
                }]
                response["sections"][0]["coverage_obligation_ids"] = [
                    "argument:problem",
                    "experiment:main",
                    "fact:result",
                    "limitation:scope",
                ]
                return response

            def repair_writer(self, request: object) -> dict:
                self.repair_calls += 1
                self.last_repair_request = request
                return {
                    "section_patches": [{
                        "after_heading": "## 实验",
                        "obligation_ids": ["experiment:main"],
                        "append_markdown": "在 ToolPrivBench 留出任务上，安全成功率从 64% 提升到 98%。",
                    }]
                }

        model = ExperimentRepairingModel()
        result = PaperReader(
            model,
            writer=lambda payload: "# 论文精读\n\n## 实验\n\n安全成功率从 64% 提升到 98%，但没有交代实验对象。\n",
        ).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "On held-out ToolPrivBench tasks, safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual((model.repair_calls, result.receipt.writer_repair_calls), (1, 1))
        evidence = model.last_repair_request["missing_obligation_evidence"][0]
        self.assertEqual(evidence["kind"], "experiment")
        self.assertEqual(evidence["required_terms"], ("ToolPrivBench",))
        self.assertIn("ToolPrivBench", result.draft.markdown)
        self.assertFalse(result.receipt.unsupported_writer_claims)

    def test_direct_writer_receives_full_contract_once_without_unit_memo_synthesis(self) -> None:
        writer_payloads: list[dict] = []

        def writer(payload: dict) -> str:
            writer_payloads.append(payload)
            return "# 论文精读\n\n代理会越权；broker 在执行前后审计动作，安全成功率从 64% 提升到 98%，但这不替代沙箱。\n"

        result = PaperReader(_FullPaperWriterModel(), writer=writer).read(
            PaperCandidate("paper-writer", "Writer", "https://example.com/writer", "agents"),
            CanonicalPaperIR(
                "paper-writer",
                "Writer",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(len(writer_payloads), 1)
        payload = writer_payloads[0]
        for field in (
            "paper_model",
            "section_contracts",
            "coverage_ledger",
            "definition_neighborhoods",
            "asset_plan",
            "evidence_allow_list",
            "unresolved_boundaries",
        ):
            self.assertIn(field, payload)
        self.assertNotIn("unit_memos", payload)
        self.assertNotIn("section_writer_calls", payload)
        self.assertIn("broker", result.draft.markdown)
        self.assertIn("64%", result.draft.markdown)
        self.assertIn("98%", result.draft.markdown)
