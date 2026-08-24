from __future__ import annotations

from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock, PaperReader, ReadingIntent


class _PaperModelWithContracts:
    text_model = "text-test-model"
    vision_model = None

    def __init__(self) -> None:
        self.full_calls = 0
        self.section_planning_calls = 0

    def read_full_paper(self, request: object) -> dict:
        self.full_calls += 1
        return {
            "thesis": "The method addresses excess authority with task-conditioned auditing.",
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
            "sections": [
                {
                    "section_id": "problem",
                    "heading": "问题与缺口",
                    "reader_question": "为什么现有方法不足？",
                    "prerequisite_bridges": ["先说明任务成功不等于权限最小化。"],
                    "reasoning_steps": ["从过度权限现象连接到权限门控缺口。"],
                    "experiment_slots": [],
                    "asset_jobs": [],
                    "transition_in": "",
                    "transition_out": "转向解决机制。",
                    "stop_conditions": ["读者能说明问题与现有缺口。"],
                    "evidence_handles": ["problem"],
                    "coverage_obligation_ids": ["argument:problem"],
                },
                {
                    "section_id": "results",
                    "heading": "实验与边界",
                    "reader_question": "实验如何验证方法，结果不能说明什么？",
                    "prerequisite_bridges": ["承接方法中的审计闭环。"],
                    "reasoning_steps": ["先交代比较，再解释结果，最后限定结论。"],
                    "experiment_slots": ["experiment:main"],
                    "asset_jobs": [],
                    "transition_in": "接着看验证。",
                    "transition_out": "回到结论边界。",
                    "stop_conditions": ["读者能复述主结果及其边界。"],
                    "evidence_handles": ["result", "limit"],
                    "coverage_obligation_ids": ["experiment:main", "fact:result", "limitation:scope"],
                },
            ],
        }

    def interpret_visual(self, request: object) -> str:
        raise AssertionError("This contract fixture has no visual asset.")


class SectionExplanationContractTests(TestCase):
    def test_missing_section_contract_fields_block_writing_instead_of_being_fabricated(self) -> None:
        class IncompleteContractModel(_PaperModelWithContracts):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["sections"] = [{
                    "section_id": "main",
                    "coverage_obligation_ids": [
                        "argument:problem",
                        "experiment:main",
                        "fact:result",
                        "limitation:scope",
                    ],
                }]
                return response

        writer_payloads: list[dict] = []
        result = PaperReader(
            IncompleteContractModel(),
            writer=lambda value: writer_payloads.append(value) or "# 不应生成\n",
        ).read(
            PaperCandidate("paper-contracts", "Contracts", "https://example.com/contracts", "agents"),
            CanonicalPaperIR(
                "paper-contracts",
                "Contracts",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(writer_payloads, [])
        self.assertEqual((result.receipt.status, result.receipt.stop_reason), ("failed", "failed"))
        self.assertIn("paper_model_contract", result.receipt.degradations)
        self.assertEqual(result.trace.section_contracts, ())

    def test_deterministic_repair_assigns_inline_result_asset_to_result_section(self) -> None:
        class ConflictedAssetModel(_PaperModelWithContracts):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["visual_candidates"] = [{
                    "block_id": "result",
                    "decision": "inline",
                    "reason": "The main result table is essential experimental evidence.",
                    "requires_pixels": False,
                }]
                return response

            def repair_section_plan(self, request: object) -> dict:
                self.section_planning_calls += 1
                return {}

        model = ConflictedAssetModel()
        result = PaperReader(model, writer=lambda value: "# Result note\n").read(
            PaperCandidate("paper-contracts", "Contracts", "https://example.com/contracts", "agents"),
            CanonicalPaperIR(
                "paper-contracts",
                "Contracts",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "table", "Results", "Safe success rises from 64% to 98%.", 3, table_html="<table><tr><td>64%</td><td>98%</td></tr></table>"),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        self.assertEqual(model.section_planning_calls, 1)
        self.assertEqual(result.trace.coverage_ledger.section_for("asset:result"), "results")

    def test_contracts_are_ordered_and_created_with_the_full_paper_model(self) -> None:
        model = _PaperModelWithContracts()
        result = PaperReader(model, writer=lambda value: "# 短但完整的笔记\n").read(
            PaperCandidate("paper-contracts", "Contracts", "https://example.com/contracts", "agents"),
            CanonicalPaperIR(
                "paper-contracts",
                "Contracts",
                (
                    PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1),
                    PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2),
                    PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
                    PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4),
                ),
            ),
            ReadingIntent(),
        )

        contracts = result.trace.paper_model.section_contracts

        self.assertEqual(model.full_calls, 1)
        self.assertEqual(model.section_planning_calls, 0)
        self.assertEqual(tuple(contract.section_id for contract in contracts), ("problem", "results"))
        for contract in contracts:
            self.assertTrue(contract.reader_question)
            self.assertTrue(contract.prerequisite_bridges)
            self.assertTrue(contract.reasoning_steps)
            self.assertIsNotNone(contract.experiment_slots)
            self.assertIsNotNone(contract.asset_jobs)
            self.assertIsNotNone(contract.transition_in)
            self.assertIsNotNone(contract.transition_out)
            self.assertTrue(contract.stop_conditions)
        self.assertFalse(any(hasattr(contract, "target_characters") for contract in contracts))
