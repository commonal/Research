from __future__ import annotations

from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock, PaperReader, ReadingIntent


def _candidate() -> PaperCandidate:
    return PaperCandidate("paper-quality", "Quality", "https://example.com/quality", "agents")


def _paper() -> CanonicalPaperIR:
    return CanonicalPaperIR(
        "paper-quality",
        "Quality",
        (
            PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1, facets=("problem",)),
            PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2, facets=("method",)),
            PaperIRBlock("formula", "formula", "Method", "R = S - P", 3, latex="R = S - P", facets=("method",)),
            PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 4, facets=("experiment",)),
            PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 5, facets=("limitation",)),
        ),
    )


class _QualityModel:
    text_model = "text-test-model"
    vision_model = None

    def __init__(self, *, missing_obligation: bool = False, repair_writer: bool = False) -> None:
        self.missing_obligation = missing_obligation
        self.repair_writer_enabled = repair_writer

    def read_full_paper(self, request: object) -> dict:
        return {
            "thesis": "Auditing reduces excess authority.",
            "argument_chain": {"problem": "Agents may exercise authority beyond the task."},
            "experiments": [{"experiment_id": "experiment:main"}],
            "must_preserve_facts": [{"fact_id": "fact:f-result", "eligible": True}],
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
            "visual_candidates": [{"block_id": "formula", "decision": "inline", "reason": "The reward definition is central."}],
            "sections": [{
                "section_id": "main",
                "heading": "论文主线",
                "reader_question": "方法如何降低越权，实验说明了什么？",
                "prerequisite_bridges": ["先说明任务成功不等于权限安全。"],
                "reasoning_steps": ["连接审计机制、奖励公式、结果和边界。"],
                "experiment_slots": ["experiment:main"],
                "asset_jobs": [{"asset_id": "asset:formula", "job": "解释奖励关系。"}],
                "transition_in": "从问题进入方法。",
                "transition_out": "最后说明适用边界。",
                "stop_conditions": ["读者能复述机制、公式、结果和局限。"],
                "evidence_handles": ["problem", "method", "formula", "result", "limit"],
                "coverage_obligation_ids": ([] if self.missing_obligation else ["argument:problem", "experiment:main", "fact:f-result", "limitation:scope", "asset:formula"]),
            }],
        }

    def interpret_visual(self, request: object) -> str:
        raise AssertionError("This quality fixture has no visual asset.")

    def repair_writer(self, request: dict) -> dict:
        if not self.repair_writer_enabled:
            return {"markdown": "# 仍未修复\n\nP 表示持久性。\n"}
        return {
            "section_patches": [{
                "after_heading": "# 解释",
                "obligation_ids": ["asset:formula"],
                "append_markdown": "$R = S - P$。只保留来源中定义的关系。",
            }]
        }


class ReadingQualityPolicyTests(TestCase):
    def test_blind_reader_failure_blocks_completion_without_rewriting_the_note(self) -> None:
        class BlindFailingModel(_QualityModel):
            def review_note(self, request: dict) -> dict:
                return {
                    "answers": [{"section_id": "main", "answer": ""}],
                    "unanswered_section_ids": ["main"],
                }

        markdown = "# 一句话\n\n$R = S - P$。\n"
        result = PaperReader(BlindFailingModel(), writer=lambda value: markdown).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.draft.markdown, markdown)
        self.assertEqual((result.receipt.status, result.receipt.blind_review_status), ("failed", "failed"))
        self.assertEqual(
            result.receipt.blind_review_failures,
            (
                "main",
                "argument:problem",
                "experiment:main",
                "fact:f-result",
                "limitation:scope",
                "asset:formula",
            ),
        )

    def test_blind_reader_pass_is_recorded_without_changing_the_note(self) -> None:
        class BlindPassingModel(_QualityModel):
            def review_note(self, request: dict) -> dict:
                return {
                    "answers": [{"section_id": "main", "answer": "正文给出了问题、审计机制、奖励关系、实验结果与边界。"}],
                    "unanswered_section_ids": [],
                    "obligation_evidence": [
                        {"obligation_id": "argument:problem", "supporting_quote": "代理可能在完成任务时使用超出任务所需的权限。"},
                        {"obligation_id": "experiment:main", "supporting_quote": "安全成功率从 64% 提升到 98%。"},
                        {"obligation_id": "fact:f-result", "supporting_quote": "安全成功率从 64% 提升到 98%。"},
                        {"obligation_id": "limitation:scope", "supporting_quote": "它不能替代沙箱。"},
                        {"obligation_id": "asset:formula", "supporting_quote": "$R = S - P$。"},
                    ],
                }

        markdown = (
            "# 一句话\n\n"
            "代理可能在完成任务时使用超出任务所需的权限。broker 在执行前后进行审计。"
            "$R = S - P$。安全成功率从 64% 提升到 98%。它不能替代沙箱。\n"
        )
        result = PaperReader(BlindPassingModel(), writer=lambda value: markdown).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.draft.markdown, markdown)
        self.assertEqual((result.receipt.status, result.receipt.blind_review_status, result.receipt.blind_review_calls), ("completed", "passed", 1))
        self.assertEqual(result.receipt.blind_review_failures, ())

    def test_blind_reader_may_assess_obligations_without_recopying_every_quote(self) -> None:
        class CompactBlindModel(_QualityModel):
            def review_note(self, request: dict) -> dict:
                obligations = request["questions"][0]["coverage_obligations"]
                return {
                    "answers": [{"section_id": "main", "answer": "笔记解释了问题、机制、结果和边界。"}],
                    "unanswered_section_ids": [],
                    "obligation_assessments": [
                        {"obligation_id": item["obligation_id"], "supported": True}
                        for item in obligations
                    ],
                }

        markdown = (
            "# 一句话\n\n代理可能在完成任务时使用超出任务所需的权限。broker 在执行前后进行审计。"
            "$R = S - P$。安全成功率从 64% 提升到 98%。它不能替代沙箱。\n"
        )
        result = PaperReader(CompactBlindModel(), writer=lambda value: markdown).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((result.receipt.status, result.receipt.blind_review_status), ("completed", "passed"))
        self.assertFalse(result.receipt.blind_review_failures)

    def test_blind_reader_reviews_one_section_and_its_obligations_per_call(self) -> None:
        class SectionBlindModel(_QualityModel):
            def __init__(self) -> None:
                super().__init__()
                self.review_requests: list[dict] = []

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                base = response["sections"][0]
                response["sections"] = [
                    {
                        **base,
                        "section_id": "sec_problem",
                        "heading": "问题",
                        "reader_question": "论文要解决什么问题？",
                        "experiment_slots": [],
                        "asset_jobs": [],
                        "evidence_handles": ["problem", "method"],
                        "coverage_obligation_ids": ["argument:problem"],
                    },
                    {
                        **base,
                        "section_id": "sec_result",
                        "heading": "结果",
                        "reader_question": "实验如何验证方法并留下什么边界？",
                        "evidence_handles": ["formula", "result", "limit"],
                        "coverage_obligation_ids": ["experiment:main", "fact:f-result", "limitation:scope", "asset:formula"],
                    },
                ]
                return response

            def review_note(self, request: dict) -> dict:
                self.review_requests.append(request)
                quote_by_id = {
                    "argument:problem": "代理可能使用超出任务所需的权限。",
                    "experiment:main": "安全成功率从 64% 提升到 98%。",
                    "fact:f-result": "安全成功率从 64% 提升到 98%。",
                    "limitation:scope": "它不能替代沙箱。",
                    "asset:formula": "$R = S - P$。",
                }
                answers = []
                evidence = []
                for question in request["questions"]:
                    answers.append({"section_id": question["section_id"], "answer": "该节能够回答问题。"})
                    evidence.extend(
                        {
                            "obligation_id": item["obligation_id"],
                            "supporting_quote": quote_by_id[item["obligation_id"]],
                        }
                        for item in question["coverage_obligations"]
                    )
                return {"answers": answers, "unanswered_section_ids": [], "obligation_evidence": evidence}

        markdown = (
            "# 精读\n\n"
            "## 问题\n\n代理可能使用超出任务所需的权限。broker 会在执行前后审计。\n\n"
            "## 结果\n\n$R = S - P$。安全成功率从 64% 提升到 98%。它不能替代沙箱。\n"
        )
        model = SectionBlindModel()
        result = PaperReader(model, writer=lambda value: markdown).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((result.receipt.status, result.receipt.blind_review_status), ("completed", "passed"))
        self.assertEqual((len(model.review_requests), result.receipt.blind_review_calls), (2, 2))
        self.assertTrue(all(len(request["questions"]) == 1 for request in model.review_requests))
        self.assertNotIn("## 结果", model.review_requests[0]["markdown"])
        self.assertNotIn("## 问题", model.review_requests[1]["markdown"])

    def test_blind_reader_cannot_pass_with_a_quote_absent_from_the_note(self) -> None:
        class FabricatingBlindModel(_QualityModel):
            def review_note(self, request: dict) -> dict:
                return {
                    "answers": [{"section_id": "main", "answer": "内容完整。"}],
                    "unanswered_section_ids": [],
                    "obligation_evidence": [{
                        "obligation_id": "argument:problem",
                        "supporting_quote": "这句话并不存在于笔记中。",
                    }],
                }

        result = PaperReader(
            FabricatingBlindModel(),
            writer=lambda value: "# 笔记\n\n$R = S - P$。\n",
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((result.receipt.status, result.receipt.blind_review_status), ("failed", "failed"))
        self.assertIn("argument:problem", result.receipt.blind_review_failures)

    def test_empty_writer_output_is_a_provider_failure_not_a_placeholder_note(self) -> None:
        result = PaperReader(_QualityModel(), writer=lambda value: "").read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.draft.markdown, "")
        self.assertEqual(result.receipt.status, "failed")
        self.assertIn("empty_writer_output", result.receipt.degradations)

    def test_short_note_is_not_rejected_when_all_quality_checks_pass(self) -> None:
        result = PaperReader(_QualityModel(), writer=lambda value: "# 一句话\n\n$R = S - P$。\n").read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.receipt.status, "completed")

    def test_internal_section_markers_do_not_leak_into_the_reading_draft(self) -> None:
        marked = (
            "# 精读\n\n<!-- rp-section:main -->\n## 论文主线\n\n"
            "代理可能越权，broker 会执行审计；$R = S - P$。安全成功率从 64% 提升到 98%，且不能替代沙箱。\n"
        )
        result = PaperReader(_QualityModel(), writer=lambda value: marked).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.receipt.status, "completed")
        self.assertNotIn("rp-section", result.draft.markdown)
        self.assertIn("## 论文主线", result.draft.markdown)

    def test_missing_coverage_obligation_remains_blocking_even_for_short_note(self) -> None:
        result = PaperReader(_QualityModel(missing_obligation=True), writer=lambda value: "# 看起来完整\n").read(_candidate(), _paper(), ReadingIntent())

        self.assertIn(result.receipt.status, ("bounded", "failed"))

    def test_unsupported_writer_fact_remains_blocking_even_for_short_note(self) -> None:
        result = PaperReader(_QualityModel(), writer=lambda value: "# 解释\n\nP 表示持久性。\n").read(_candidate(), _paper(), ReadingIntent())

        self.assertIn(result.receipt.status, ("bounded", "failed"))

    def test_supported_local_writer_repair_runs_once_for_an_unsupported_claim(self) -> None:
        model = _QualityModel(repair_writer=True)
        result = PaperReader(model, writer=lambda value: "# 解释\n\nP 表示持久性。\n").read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((result.receipt.status, result.receipt.writer_repair_calls), ("completed", 1))
        self.assertNotIn("P 表示持久性", result.draft.markdown)
