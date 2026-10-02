from __future__ import annotations

from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock, PaperReader, ReadingIntent


def _candidate() -> PaperCandidate:
    return PaperCandidate("paper-b", "Full-paper baseline", "https://example.com/paper-b", "agents")


def _paper() -> CanonicalPaperIR:
    return CanonicalPaperIR(
        source_id="paper-b",
        title="Full-paper baseline",
        blocks=(
            PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1, facets=("problem",)),
            PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2, facets=("method",)),
            PaperIRBlock("result", "table", "Results", "Safe success rises from 64.36% to 98.48%.", 3, table_html="<table><tr><td>Base</td><td>64.36%</td></tr><tr><td>Seed 1</td><td>98.48%</td></tr></table>", facets=("experiment",)),
            PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4, facets=("limitation",)),
        ),
    )


def _paper_with_reward_formula() -> CanonicalPaperIR:
    base = _paper()
    return CanonicalPaperIR(
        source_id=base.source_id,
        title=base.title,
        blocks=(
            base.blocks[0],
            base.blocks[1],
            PaperIRBlock("reward-intro", "paragraph", "Method / Reward", "The scalar reward used for learning optimization is given by:", 3, facets=("method",)),
            PaperIRBlock("reward-formula", "formula", "Method / Reward", "R = 0.60S + 0.20E - 0.20P - 0.05U + 0.10H - 0.75B - 0.05F_u - 0.12F_r", 4, latex="R = 0.60S + 0.20E - 0.20P - 0.05U + 0.10H - 0.75B - 0.05F_u - 0.12F_r", facets=("method",)),
            PaperIRBlock("reward-definitions", "paragraph", "Method / Reward", "Here P combines weighted trajectory excess. U penalizes action count and repeated loops. H rewards correct escalation. B measures blocked-action severity. F_u marks malformed or unsupported actions, and F_r marks forbidden reads. Besides S and E that represent the task objective, the remaining terms add safety friction.", 5, facets=("method",)),
            PaperIRBlock(base.blocks[2].block_id, base.blocks[2].kind, base.blocks[2].section, base.blocks[2].text, 6, table_html=base.blocks[2].table_html, facets=base.blocks[2].facets),
            PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 7, facets=base.blocks[3].facets),
        ),
    )


class _CompleteFullPaperModel:
    text_model = "text-test-model"
    vision_model = "vision-test-model"

    def read_full_paper(self, request: object) -> dict:
        return {
            "thesis": "Post-training can reduce excess authority without replacing sandboxing.",
            "argument_chain": {
                "background": "Tool agents can exceed task authority.",
                "concrete_problem": "Task success alone does not imply least privilege.",
                "prior_gap": "Permission gates alone are insufficient.",
                "core_idea": "Audit actions before and after execution.",
                "experiment_logic": "Compare safe success on held-out tasks.",
                "conclusion_scope": "The method complements sandboxing.",
            },
            "coverage": {name: {"status": "complete", "missing": []} for name in ("background", "problem", "prior_gap", "mechanism", "experiment", "boundary")},
            "source_facts": [
                {"fact_id": "f-problem", "facet": "problem", "statement": "Agents exercise authority beyond the task.", "source_block_ids": ["problem"]},
                {"fact_id": "f-method", "facet": "method", "statement": "A broker audits actions before and after execution.", "source_block_ids": ["method"]},
                {"fact_id": "f-result", "facet": "experiment", "statement": "Safe success rises from 64.36% to 98.48%.", "source_block_ids": ["result"]},
                {"fact_id": "f-limit", "facet": "limitation", "statement": "The method does not replace sandboxing.", "source_block_ids": ["limit"]},
            ],
            "source_limitations": [{"statement": "The method does not replace sandboxing.", "source_block_ids": ["limit"]}],
            "agent_syntheses": [],
            "experiments": [{
                "experiment_id": "main",
                "setup": ["Held-out tool-agent tasks."],
                "comparison": ["Base policy versus the audited policy."],
                "results": ["Safe success rises from 64.36% to 98.48%."],
                "interpretation": "Auditing improves safe task completion in this evaluation.",
                "boundary": "The result does not establish replacement of sandboxing.",
                "source_block_ids": ["result", "limit"],
            }],
            "must_preserve_facts": [{
                "fact_id": "f-result",
                "statement": "Safe success rises from 64.36% to 98.48%.",
                "eligible": True,
                "source_block_ids": ["result"],
            }],
            "limitations": [{
                "limitation_id": "scope",
                "statement": "The method does not replace sandboxing.",
                "source_block_ids": ["limit"],
            }],
            "sections": [{
                "section_id": "main",
                "heading": "论文主线",
                "reader_question": "论文解决什么问题、如何验证、边界是什么？",
                "prerequisite_bridges": ["先区分任务成功与最小权限。"],
                "reasoning_steps": ["连接问题、已有缺口、审计机制、实验结果与边界。"],
                "experiment_slots": ["experiment:main"],
                "asset_jobs": [],
                "transition_in": "从问题进入机制。",
                "transition_out": "从结果收束到边界。",
                "stop_conditions": ["读者能复述问题、方法、实验和边界。"],
                "evidence_handles": ["problem", "method", "result", "limit"],
                "coverage_obligation_ids": [
                    "argument:background",
                    "argument:concrete_problem",
                    "argument:prior_gap",
                    "argument:core_idea",
                    "argument:experiment_logic",
                    "argument:conclusion_scope",
                    "experiment:main",
                    "fact:f-result",
                    "limitation:scope",
                ],
            }],
            "visual_candidates": [],
            "material_unknowns": [],
        }

    def repair_section_plan(self, request: object) -> dict:
        return {}

    def read_target(self, request: object) -> dict:
        raise AssertionError("A coverage-complete PaperModel must not start targeted reading.")

    def interpret_visual(self, request: object) -> str:
        raise AssertionError("This paper has no visual requiring pixel inspection.")


class FullPaperBaselineTests(TestCase):
    def test_reading_result_preserves_writer_evidence_link_to_source_block(self) -> None:
        result = PaperReader(
            _CompleteFullPaperModel(),
            writer=lambda value: (
                "# 论文主线\n\n"
                "Agents exercise authority beyond the task. "
                "A broker audits actions before and after execution. "
                "Safe success rises from 64.36% to 98.48%.{{evidence:result}} "
                "The method does not replace sandboxing.\n"
            ),
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(
            tuple((link.sentence, link.source_block_ids) for link in result.draft.evidence_links),
            (("Safe success rises from 64.36% to 98.48%.", ("result",)),),
        )
        self.assertNotIn("{{evidence:", result.draft.markdown)

    def test_unknown_writer_evidence_id_blocks_reading_result(self) -> None:
        result = PaperReader(
            _CompleteFullPaperModel(),
            writer=lambda value: (
                "# 论文主线\n\n"
                "Agents exercise authority beyond the task. "
                "A broker audits actions before and after execution. "
                "Safe success rises from 64.36% to 98.48%.{{evidence:missing-source}} "
                "The method does not replace sandboxing.\n"
            ),
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.receipt.status, "failed")
        self.assertIn("invalid_evidence_link:missing-source", result.receipt.degradations)
        self.assertEqual(result.draft.evidence_links, ())
        self.assertNotIn("{{evidence:", result.draft.markdown)

    def test_unlinked_exact_numeric_sentence_is_reported_without_blocking_publication(self) -> None:
        result = PaperReader(
            _CompleteFullPaperModel(),
            writer=lambda value: (
                "# 论文主线\n\n"
                "Agents exercise authority beyond the task. "
                "A broker audits actions before and after execution. "
                "Safe success rises from 64.36% to 98.48%. "
                "The method does not replace sandboxing.\n"
            ),
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.receipt.status, "completed")
        self.assertIn(
            "missing_evidence_link:numeric:64.36%,98.48%",
            result.receipt.degradations,
        )
        self.assertEqual(result.draft.evidence_links, ())

    def test_provider_short_visual_id_resolves_to_the_unique_canonical_block(self) -> None:
        base = _paper()
        canonical_id = "normalized:paper-b:figure:abc123"
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                PaperIRBlock(canonical_id, "figure", "Model Architecture", "Figure 2. Architecture and information flow.", 3, caption="Figure 2. Architecture and information flow.", image_path="safe.png", safe_image=True),
                PaperIRBlock(base.blocks[2].block_id, base.blocks[2].kind, base.blocks[2].section, base.blocks[2].text, 4, table_html=base.blocks[2].table_html, facets=base.blocks[2].facets),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 5, facets=base.blocks[3].facets),
            ),
        )

        class ShortIdModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{
                    "block_id": "figure:abc123",
                    "decision": "inline",
                    "requires_pixels": False,
                    "reason": "The architecture arrows are needed to explain information flow.",
                }]
                return response

            def interpret_visual(self, request: object) -> str:
                return "The arrows connect the encoder to the patch decoder."

        result = PaperReader(
            ShortIdModel(),
            note_planner=lambda value: {"sections": [("方法", "解释架构图。")]},
            writer=lambda value: "# 方法架构\n",
        ).read(_candidate(), paper, ReadingIntent())

        self.assertEqual(result.receipt.vision_calls, 1)
        self.assertEqual(result.trace.visual_decisions[0].block_id, canonical_id)

    def test_missing_central_method_figure_is_repaired_from_paper_structure(self) -> None:
        base = _paper()
        figure_id = "architecture-figure"
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                PaperIRBlock(figure_id, "figure", "Model Architecture", "Figure 2. Architecture and information flow.", 3, caption="Figure 2. Architecture and information flow.", image_path="safe.png", safe_image=True),
                PaperIRBlock(base.blocks[2].block_id, base.blocks[2].kind, base.blocks[2].section, base.blocks[2].text, 4, table_html=base.blocks[2].table_html, facets=base.blocks[2].facets),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 5, facets=base.blocks[3].facets),
            ),
        )

        class OmittedVisualModel(_CompleteFullPaperModel):
            def interpret_visual(self, request: object) -> str:
                return "The arrows connect the encoder to the patch decoder."

        result = PaperReader(
            OmittedVisualModel(),
            note_planner=lambda value: {"sections": [("方法", "解释架构图。")]},
            writer=lambda value: "# 方法架构\n",
        ).read(_candidate(), paper, ReadingIntent())

        self.assertEqual(result.receipt.vision_calls, 1)
        self.assertEqual(result.trace.visual_decisions[0].block_id, figure_id)

    def test_complete_full_paper_model_skips_target_loop(self) -> None:
        result = PaperReader(
            _CompleteFullPaperModel(),
            note_planner=lambda value: {"sections": [("主线", "全文论证已经覆盖。")]},
            writer=lambda value: "# 全文阅读笔记\n\n全文论证已经覆盖。\n",
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((result.receipt.strategy, result.receipt.target_count, result.trace.targets), ("full_paper", 0, ()))
        self.assertEqual(result.receipt.full_read_calls, 1)
        self.assertEqual(result.receipt.unknown_count, 0)
        self.assertEqual(result.receipt.paper_ir_version, "canonical-paper-ir-v1")
        self.assertTrue(result.receipt.prompt_version)
        self.assertEqual((result.receipt.image_count, result.receipt.image_bytes), (0, 0))

    def test_provider_may_wrap_complete_paper_model_in_a_named_object(self) -> None:
        class NestedModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                return {"paper_model": super().read_full_paper(request)}

        result = PaperReader(
            NestedModel(),
            note_planner=lambda value: {"sections": [("主线", "全文论证已经覆盖。")]},
            writer=lambda value: "# 全文阅读笔记\n",
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(result.receipt.strategy, "full_paper")

    def test_selected_formula_preserves_adjacent_definition_neighborhood(self) -> None:
        class FormulaModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["source_facts"].append({
                    "fact_id": "f-reward",
                    "facet": "method",
                    "statement": "The reward is R = 0.60S + 0.20E - 0.20P - 0.05U + 0.10H - 0.75B - 0.05F_u - 0.12F_r.",
                    "source_block_ids": ["reward-formula"],
                })
                response["visual_candidates"] = [{"block_id": "reward-formula", "decision": "inline", "value": "The reward terms are central to the training mechanism."}]
                return response

        result = PaperReader(
            FormulaModel(),
            note_planner=lambda value: {"sections": [("奖励", "解释奖励设计。")]},
            writer=lambda value: "# 奖励设计\n",
        ).read(_candidate(), _paper_with_reward_formula(), ReadingIntent())

        neighborhood = result.trace.paper_model.definition_neighborhoods[0]
        self.assertEqual(neighborhood.source_block_ids, ("reward-intro", "reward-formula", "reward-definitions"))

    def test_writer_cannot_define_a_formula_symbol_missing_from_source_context(self) -> None:
        paper = _paper_with_reward_formula()
        blocks = tuple(
            PaperIRBlock(block.block_id, block.kind, block.section, "The paper continues with training details." if block.block_id == "reward-definitions" else block.text, block.order, caption=block.caption, latex=block.latex, table_html=block.table_html, facets=block.facets)
            for block in paper.blocks
        )

        class FormulaModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{"block_id": "reward-formula", "decision": "inline", "value": "The reward formula is important."}]
                return response

        result = PaperReader(
            FormulaModel(),
            note_planner=lambda value: {"sections": [("奖励", "解释奖励设计。")]},
            writer=lambda value: "# 奖励设计\n\n$R = 0.60S + 0.20E - 0.20P - 0.05U + 0.10H - 0.75B - 0.05F_u - 0.12F_r$。奖励把成功与安全约束结合起来。P 表示持久性。安全成功率从 64.36% 提升到 98.48%。{{evidence:result}}\n",
        ).read(_candidate(), CanonicalPaperIR(paper.source_id, paper.title, blocks), ReadingIntent())

        self.assertEqual(("P 表示持久性" in result.draft.markdown, result.receipt.degradations), (False, ("unsupported_writer_claim:P",)))

    def test_defined_formula_symbol_with_a_tex_word_subscript_is_not_rejected(self) -> None:
        base = _paper()
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                PaperIRBlock("safe-intro", "paragraph", "Method", "Safe success is defined below.", 3, facets=("method",)),
                PaperIRBlock("safe-formula", "formula", "Method", "SAFESUCCESS = X[S=1 and O_tau=0]", 4, latex=r"\mathrm{SAFESUCCESS}=\mathbb{X}[S=1 \land O_{\tau}=0]", facets=("method",)),
                PaperIRBlock("safe-def", "paragraph", "Method", r"Here S denotes task success and O_{\tau} is the reportable excess-authority event.", 5, facets=("method",)),
                PaperIRBlock(base.blocks[2].block_id, base.blocks[2].kind, base.blocks[2].section, base.blocks[2].text, 6, table_html=base.blocks[2].table_html, facets=base.blocks[2].facets),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 7, facets=base.blocks[3].facets),
            ),
        )

        class SafeFormulaModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{"block_id": "safe-formula", "decision": "inline", "reason": "Defines safe success."}]
                response["sections"][0]["asset_jobs"] = [{"asset_id": "asset:safe-formula", "job": "Explain safe success."}]
                response["sections"][0]["coverage_obligation_ids"].append("asset:safe-formula")
                return response

        result = PaperReader(
            SafeFormulaModel(),
            writer=lambda value: (
                "# 指标\n\n$\\mathrm{SAFESUCCESS}=\\mathbb{X}[S=1 \\land O_{\\tau}=0]$，"
                "$O_{\\tau}$ 表示可报告的超额权限事件。安全成功率从 64.36% 提升到 98.48%。\n"
            ),
        ).read(_candidate(), paper, ReadingIntent())

        self.assertIn("O_{\\tau}", result.draft.markdown)
        self.assertNotIn("unsupported_writer_claim:O", result.receipt.degradations)

    def test_writer_cannot_swap_table_row_metrics(self) -> None:
        class TableModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{"block_id": "result", "decision": "inline", "value": "This is the decisive result table."}]
                return response

        result = PaperReader(
            TableModel(),
            note_planner=lambda value: {"sections": [("结果", "解释主结果表。")]},
            writer=lambda value: "# 实验结果\n\nBase 的安全成功率是 98.48%。Seed 1 的安全成功率是 64.36%。\n",
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual(
            ("Base 的安全成功率是 98.48%" in result.draft.markdown, "Seed 1 的安全成功率是 64.36%" in result.draft.markdown, result.receipt.degradations),
            (
                False,
                False,
                (
                    "unsupported_writer_claim:table:result",
                    "unexpressed_obligation:experiment:main",
                    "unexpressed_obligation:fact:f-result",
                ),
            ),
        )

    def test_table_fact_completes_exact_numeric_evidence_from_nearby_prose(self) -> None:
        base = _paper()
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                PaperIRBlock("evaluation-total", "paragraph", "Results", "The complete held-out evaluation contains 2,896 task-policy pairs.", 3, facets=("experiment",)),
                PaperIRBlock("result", "table", "Results", "Main held-out results.", 4, table_html=base.blocks[2].table_html, facets=("experiment",)),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 5, facets=base.blocks[3].facets),
            ),
        )

        class CompositeTableModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["source_facts"][2] = {
                    "fact_id": "f-result",
                    "facet": "experiment",
                    "statement": "Across 2,896 task-policy pairs, safe success rises from 64.36% to 98.48%.",
                    "source_block_ids": ["result"],
                }
                return response

        result = PaperReader(
            CompositeTableModel(),
            note_planner=lambda value: {"sections": [("结果", "保留主结果。")]},
            writer=lambda value: "# 实验结果\n\n在 2,896 个任务—策略组合上，安全成功率从 64.36% 提升到 98.48%。\n",
        ).read(_candidate(), paper, ReadingIntent())

        fact = next(item for item in result.trace.paper_model.source_facts if item.fact_id == "f-result")
        self.assertEqual((fact.source_block_ids, "2,896" in result.draft.markdown), (("result", "evaluation-total"), True))

    def test_table_definition_neighborhood_preserves_its_footnote(self) -> None:
        base = _paper()
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                base.blocks[2],
                PaperIRBlock("result-footnote", "footnote", "Results", "Safe success counts only episodes that complete the task and stay within the authority envelope.", 4),
                PaperIRBlock("result-prose", "paragraph", "Results", "The audited policy improves the main result.", 5),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 6, facets=base.blocks[3].facets),
            ),
        )

        result = PaperReader(
            _CompleteFullPaperModel(),
            writer=lambda value: "# 实验\n\n安全成功率从 64.36% 提升到 98.48%。\n",
        ).read(_candidate(), paper, ReadingIntent())

        neighborhood = next(item for item in result.trace.paper_model.definition_neighborhoods if item.object_block_id == "result")
        self.assertIn("result-footnote", neighborhood.source_block_ids)
        self.assertIn("authority envelope", neighborhood.context)

    def test_writer_keeps_formula_definitions_supported_by_validated_facts(self) -> None:
        class FormulaModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["source_facts"].extend((
                    {
                        "fact_id": "f-safe",
                        "facet": "method",
                        "statement": "In the reward, S is task success and E is evidence sufficiency.",
                        "source_block_ids": ["reward-formula", "reward-intro"],
                    },
                    {
                        "fact_id": "f-reward-terms",
                        "facet": "method",
                        "statement": "R is the scalar reward; P measures excess authority, U penalizes repeated actions, H rewards escalation, B measures blocked-action severity, F_u marks unsupported actions, and F_r marks forbidden reads.",
                        "source_block_ids": ["reward-intro", "reward-formula", "reward-definitions"],
                    },
                ))
                response["visual_candidates"] = [{"block_id": "reward-formula", "decision": "inline", "value": "The reward explains training."}]
                return response

        result = PaperReader(
            FormulaModel(),
            note_planner=lambda value: {"sections": [("奖励", "解释各项。")]},
            writer=lambda value: "# 奖励设计\n\n$R = 0.60S + 0.20E - 0.20P - 0.05U + 0.10H - 0.75B - 0.05F_u - 0.12F_r$。R 是总奖励；S 表示任务成功，E 表示证据充分性，P 衡量超额权限。安全成功率从 64.36% 提升到 98.48%。{{evidence:result}}\n",
        ).read(_candidate(), _paper_with_reward_formula(), ReadingIntent())

        self.assertIn("R 是总奖励", result.draft.markdown)
        self.assertIn("S 表示任务成功", result.draft.markdown)
        self.assertEqual(result.receipt.degradations, ())

    def test_formula_neighborhood_recognizes_lhs_and_coordinated_definitions(self) -> None:
        class FormulaModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["source_facts"].append({
                    "fact_id": "f-reward",
                    "facet": "method",
                    "statement": "The scalar reward is R = 0.60S + 0.20E - 0.20P - 0.05U + 0.10H - 0.75B - 0.05F_u - 0.12F_r.",
                    "source_block_ids": ["reward-intro", "reward-formula", "reward-definitions"],
                })
                response["visual_candidates"] = [{"block_id": "reward-formula", "decision": "inline", "value": "The reward explains training."}]
                return response

        result = PaperReader(
            FormulaModel(),
            note_planner=lambda value: {"sections": [("奖励", "解释各项。")]},
            writer=lambda value: "# 奖励设计\n\n$R = 0.60S + 0.20E - 0.20P - 0.05U + 0.10H - 0.75B - 0.05F_u - 0.12F_r$。R 是总奖励；S 和 E 表示任务目标。安全成功率从 64.36% 提升到 98.48%。{{evidence:result}}\n",
        ).read(_candidate(), _paper_with_reward_formula(), ReadingIntent())

        neighborhood = next(item for item in result.trace.paper_model.definition_neighborhoods if item.object_block_id == "reward-formula")
        self.assertTrue({"R", "S", "E"}.issubset(set(neighborhood.defined_symbols)))
        self.assertIn("R 是总奖励", result.draft.markdown)
        self.assertEqual(result.receipt.degradations, ())

    def test_unique_exact_source_recovers_a_provider_block_id_typo(self) -> None:
        base = _paper()
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                PaperIRBlock("training-real", "paragraph", "Training", "We train Qwen3.5-4B with rank 32 LoRA, alpha 64, and a 4,096-token completion budget.", 3, facets=("method",)),
                PaperIRBlock(base.blocks[2].block_id, base.blocks[2].kind, base.blocks[2].section, base.blocks[2].text, 4, table_html=base.blocks[2].table_html, facets=base.blocks[2].facets),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 5, facets=base.blocks[3].facets),
            ),
        )

        class TypoModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["source_facts"].append({
                    "fact_id": "f-training",
                    "facet": "method",
                    "statement": "Training uses Qwen3.5-4B with rank 32 LoRA, alpha 64, and a 4,096-token completion budget.",
                    "source_block_ids": ["training-typo"],
                })
                return response

        result = PaperReader(
            TypoModel(),
            note_planner=lambda value: {"sections": [("训练", "保留模型设置。")]},
            writer=lambda value: "# 训练\n\n使用 Qwen3.5-4B。\n",
        ).read(_candidate(), paper, ReadingIntent())

        fact = next(item for item in result.trace.paper_model.source_facts if item.fact_id == "f-training")
        self.assertEqual(fact.source_block_ids, ("training-real",))

    def test_only_indispensable_safe_figure_uses_vision(self) -> None:
        base = _paper()
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                PaperIRBlock("method-figure", "figure", "Method", "Figure 1 shows the broker control loop.", 3, caption="Policy, broker, execution and post-audit loop.", image_path="safe-figure.png", safe_image=True),
                PaperIRBlock(base.blocks[2].block_id, base.blocks[2].kind, base.blocks[2].section, base.blocks[2].text, 4, table_html=base.blocks[2].table_html, image_path="rendered-table.png", safe_image=True, facets=base.blocks[2].facets),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 5, facets=base.blocks[3].facets),
            ),
        )

        class VisualModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [
                    {"block_id": "method-figure", "decision": "inline", "requires_pixels": True, "reason": "Arrow direction explains the broker loop."},
                    {"block_id": "result", "decision": "inline", "requires_pixels": False, "reason": "Structured HTML already preserves the result table."},
                ]
                return response

            def interpret_visual(self, request: object) -> str:
                return "Arrows connect policy, broker, execution, post-audit and reward update in a closed loop."

        result = PaperReader(
            VisualModel(),
            note_planner=lambda value: {"sections": [("方法", "解释闭环。")]},
            writer=lambda value: "# 方法闭环\n",
        ).read(_candidate(), paper, ReadingIntent())

        actions = {item.block_id: item.action for item in result.trace.visual_decisions}
        self.assertEqual((result.receipt.vision_calls, actions, len(result.trace.paper_model.visual_interpretations)), (1, {"method-figure": "inline", "result": "inline"}, 1))
        self.assertEqual((result.receipt.image_count, result.receipt.image_bytes), (1, 0))
        self.assertEqual(result.receipt.asset_decision_counts, {"inline": 2, "reference": 0, "omit": 0})

    def test_incomplete_paper_model_targets_only_its_high_priority_gap_and_survives_to_writer(self) -> None:
        writer_input: dict = {}
        base = _paper()
        paper = CanonicalPaperIR(
            base.source_id,
            base.title,
            (
                base.blocks[0],
                base.blocks[1],
                PaperIRBlock("method-figure", "figure", "Method Architecture", "Figure 1. Method control flow.", 3, caption="Figure 1. Method control flow.", image_path="safe.png", safe_image=True),
                PaperIRBlock(base.blocks[2].block_id, base.blocks[2].kind, base.blocks[2].section, base.blocks[2].text, 4, table_html=base.blocks[2].table_html, facets=base.blocks[2].facets),
                PaperIRBlock(base.blocks[3].block_id, base.blocks[3].kind, base.blocks[3].section, base.blocks[3].text, 5, facets=base.blocks[3].facets),
            ),
        )

        class GapModel(_CompleteFullPaperModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["coverage"]["experiment"] = {"status": "partial", "missing": ["decisive held-out result"]}
                response["material_unknowns"] = [{"statement": "Which experiment provides the decisive held-out result?", "priority": "high"}]
                response["visual_candidates"] = [{"block_id": "method-figure", "decision": "inline", "requires_pixels": False, "reason": "The method flow is needed for the core mechanism."}]
                response["sections"][0]["asset_jobs"] = [{"asset_id": "asset:method-figure", "job": "Explain the method control flow."}]
                response["sections"][0]["coverage_obligation_ids"].append("asset:method-figure")
                return response

            def read_target(self, request: object) -> dict:
                return {"source_facts": [{
                    "fact_id": "f-gap-result",
                    "facet": "experiment",
                    "statement": "Safe success rises from 64.36% to 98.48%.",
                    "source_block_ids": ["result"],
                }]}

            def interpret_visual(self, request: object) -> str:
                return "The arrows show the method control flow."

        def writer(value: dict) -> str:
            writer_input.update(value)
            return "# 补读后的全文笔记\n"

        result = PaperReader(
            GapModel(),
            note_planner=lambda value: {"sections": [("结果", "补齐唯一高优先级缺口。")]},
            writer=writer,
        ).read(_candidate(), paper, ReadingIntent(max_targets=6))

        self.assertEqual((result.receipt.target_count, result.receipt.vision_calls, result.trace.paper_model is not None, bool(writer_input.get("paper_model"))), (1, 1, True, True))
