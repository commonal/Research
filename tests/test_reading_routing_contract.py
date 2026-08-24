from __future__ import annotations

from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock, PaperReader, ReadingIntent, ReadingTarget, visual_decision


def _candidate() -> PaperCandidate:
    return PaperCandidate("paper-routing", "Routing", "https://example.com/routing", "agents")


def _paper() -> CanonicalPaperIR:
    return CanonicalPaperIR(
        "paper-routing",
        "Routing",
        (
            PaperIRBlock("problem", "paragraph", "Introduction", "Agents exercise authority beyond the task.", 1, facets=("problem",)),
            PaperIRBlock("method", "paragraph", "Method", "A broker audits actions before and after execution.", 2, facets=("method",)),
            PaperIRBlock("result", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3, facets=("experiment",)),
            PaperIRBlock("limit", "paragraph", "Limitations", "The method does not replace sandboxing.", 4, facets=("limitation",)),
        ),
    )


class _RoutingModel:
    text_model = "text-test-model"
    vision_model = None

    def __init__(self, *, assignment_conflict: bool = False, evidence_gap: bool = False) -> None:
        self.assignment_conflict = assignment_conflict
        self.evidence_gap = evidence_gap
        self.section_repair_calls = 0
        self.target_calls = 0

    def read_full_paper(self, request: object) -> dict:
        return {
            "thesis": "Auditing reduces excess authority.",
            "argument_chain": {"problem": "Agents may exercise authority beyond the task."},
            "experiments": [{"experiment_id": "experiment:main"}],
            "must_preserve_facts": [{"fact_id": "fact:f-result", "eligible": True}],
            "limitations": [{"limitation_id": "limitation:scope"}],
            "coverage": {
                name: {"status": "partial" if self.evidence_gap and name == "experiment" else "complete", "missing": ["decisive experiment"] if self.evidence_gap and name == "experiment" else []}
                for name in ("background", "problem", "prior_gap", "mechanism", "experiment", "boundary")
            },
            "source_facts": [
                {"fact_id": "f-problem", "facet": "problem", "statement": "Agents exercise authority beyond the task.", "source_block_ids": ["problem"]},
                {"fact_id": "f-method", "facet": "method", "statement": "A broker audits actions before and after execution.", "source_block_ids": ["method"]},
                {"fact_id": "f-result", "facet": "experiment", "statement": "Safe success rises from 64% to 98%.", "source_block_ids": ["result"]},
                {"fact_id": "f-limit", "facet": "limitation", "statement": "The method does not replace sandboxing.", "source_block_ids": ["limit"]},
            ],
            "source_limitations": [],
            "material_unknowns": ([{"statement": "Which experiment provides the decisive evidence for the mechanism?", "priority": "high"}] if self.evidence_gap else []),
            "visual_candidates": [],
            "sections": [
                {
                    "section_id": "main",
                    "heading": "论文主线",
                    "reader_question": "方法如何回应问题，实验又验证了什么？",
                    "prerequisite_bridges": ["先区分任务成功与权限安全。"],
                    "reasoning_steps": ["连接问题、审计机制、实验结果与适用边界。"],
                    "experiment_slots": ["experiment:main"],
                    "asset_jobs": [],
                    "transition_in": "从问题进入方法。",
                    "transition_out": "最后说明结论边界。",
                    "stop_conditions": ["读者能复述问题、方法、实验与局限。"],
                    "evidence_handles": ["problem", "method", "result", "limit"],
                    "coverage_obligation_ids": ([] if self.assignment_conflict else ["argument:problem", "experiment:main", "fact:f-result", "limitation:scope"]),
                }
            ],
        }

    def repair_section_plan(self, request: object) -> dict:
        self.section_repair_calls += 1
        return {"sections": [{"section_id": "main", "coverage_obligation_ids": ["argument:problem", "experiment:main", "fact:f-result", "limitation:scope"]}]}

    def read_target(self, request: object) -> dict:
        self.target_calls += 1
        return {
            "source_facts": [{
                "fact_id": "f-target-result",
                "facet": "experiment",
                "statement": "Safe success rises from 64% to 98%.",
                "source_block_ids": ["result"],
            }]
        }

    def interpret_visual(self, request: object) -> str:
        raise AssertionError("This routing fixture has no visual asset.")


class ReadingRoutingContractTests(TestCase):
    def test_local_explanation_unknown_does_not_force_target_fallback(self) -> None:
        class LocalUnknownModel(_RoutingModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [{
                    "statement": "The reason for a benchmark discrepancy is not fully explained.",
                    "priority": "high",
                }]
                return response

        model = LocalUnknownModel()
        result = PaperReader(model, writer=lambda value: "# 已覆盖的笔记\n").read(
            _candidate(),
            _paper(),
            ReadingIntent(),
        )

        self.assertEqual((result.receipt.strategy, result.receipt.target_count, model.target_calls), ("full_paper", 0, 0))
        self.assertEqual(result.trace.paper_model.material_unknowns[0].priority, "medium")

    def test_not_fully_defined_reward_detail_is_not_a_high_priority_target(self) -> None:
        class RewardDetailModel(_RoutingModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["material_unknowns"] = [{
                    "statement": "The composition of the P reward term is not fully defined.",
                    "priority": "high",
                }]
                return response

        model = RewardDetailModel(assignment_conflict=True)
        writer_payloads: list[dict] = []
        result = PaperReader(
            model,
            writer=lambda value: writer_payloads.append(value) or "# 已覆盖笔记\n",
        ).read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((model.target_calls, model.section_repair_calls), (0, 1))
        self.assertEqual(result.receipt.strategy, "full_paper")
        self.assertEqual(result.trace.paper_model.material_unknowns[0].priority, "medium")
        self.assertEqual(len(writer_payloads), 1)

    def test_method_architecture_visual_is_inspected_without_target_paper_markers(self) -> None:
        block = PaperIRBlock(
            "architecture-figure",
            "figure",
            "Model Architecture",
            "Figure 2. The encoder routes repository context into a patch decoder.",
            4,
            caption="Figure 2. Model architecture and information flow.",
            image_path="architecture.png",
            safe_image=True,
        )
        target = ReadingTarget(
            "t-method",
            ("q-method",),
            (),
            "How does the model architecture connect repository context to patch generation?",
            "Explain the named components and their causal information flow.",
            "Keep unclear arrows as a visual unknown.",
        )

        self.assertEqual(visual_decision(block, target).action, "inspect")

    def test_writer_preservation_contract_uses_provider_declared_generic_terms(self) -> None:
        class GenericFullPaperModel(_RoutingModel):
            def read_full_paper(self, request: object) -> dict:
                return {
                    "thesis": "RepoGrade-X evaluates repository agents with an execution-conditioned judge.",
                    "argument_chain": {"problem": "Synthetic tasks do not represent repository work."},
                    "experiments": [{"experiment_id": "experiment:judge"}],
                    "must_preserve_facts": [
                        {
                            "fact_id": "fact:method",
                            "statement": "RepoGrade-X uses an execution-conditioned judge to validate patches.",
                            "facet": "method",
                            "source_block_ids": ["method"],
                            "eligible": True,
                            "named_entities": ["RepoGrade-X"],
                            "mechanism_terms": ["execution-conditioned judge"],
                        },
                        {
                            "fact_id": "fact:judge",
                            "statement": "RepoGrade-X reaches 91.2% agreement with human reviewers.",
                            "facet": "experiment",
                            "source_block_ids": ["judge-result"],
                            "eligible": True,
                            "numeric_tokens": ["91.2%"],
                            "named_entities": ["RepoGrade-X"],
                        },
                    ],
                    "limitations": [{"limitation_id": "limitation:scope"}],
                    "coverage": {name: {"status": "complete", "missing": []} for name in ("background", "problem", "prior_gap", "mechanism", "experiment", "boundary")},
                    "source_facts": [
                        {"fact_id": "f-problem", "facet": "problem", "statement": "Synthetic tasks do not represent repository work.", "source_block_ids": ["problem"]},
                        {"fact_id": "fact:method", "facet": "method", "statement": "RepoGrade-X uses an execution-conditioned judge to validate patches.", "source_block_ids": ["method"]},
                        {"fact_id": "fact:judge", "facet": "experiment", "statement": "RepoGrade-X reaches 91.2% agreement with human reviewers.", "source_block_ids": ["judge-result"]},
                        {"fact_id": "f-limit", "facet": "limitation", "statement": "The benchmark covers only public Python repositories.", "source_block_ids": ["limit"]},
                    ],
                    "source_limitations": [],
                    "material_unknowns": [],
                    "visual_candidates": [],
                    "sections": [{
                        "section_id": "main",
                        "heading": "论文主线",
                        "reader_question": "基准如何验证仓库级代理？",
                        "prerequisite_bridges": ["先说明短合成任务的代表性缺口。"],
                        "reasoning_steps": ["连接基准构造、执行条件评审器和人类一致性。"],
                        "experiment_slots": ["experiment:judge"],
                        "asset_jobs": [],
                        "transition_in": "从问题进入基准设计。",
                        "transition_out": "最后限定基准覆盖范围。",
                        "stop_conditions": ["读者能解释基准设计、验证和边界。"],
                        "evidence_handles": ["problem", "method", "judge-result", "limit"],
                        "coverage_obligation_ids": ["argument:problem", "experiment:judge", "fact:method", "fact:judge", "limitation:scope"],
                    }],
                }

        paper = CanonicalPaperIR(
            "paper-routing",
            "Repository benchmark",
            (
                PaperIRBlock("problem", "paragraph", "Introduction", "Synthetic tasks do not represent repository work.", 1, facets=("problem",)),
                PaperIRBlock("method", "paragraph", "Method", "RepoGrade-X uses an execution-conditioned judge to validate patches.", 2, facets=("method",)),
                PaperIRBlock("judge-result", "paragraph", "Evaluation", "RepoGrade-X reaches 91.2% agreement with human reviewers.", 3, facets=("experiment",)),
                PaperIRBlock("limit", "paragraph", "Limitations", "The benchmark covers only public Python repositories.", 4, facets=("limitation",)),
            ),
        )
        writer_payloads: list[dict] = []

        result = PaperReader(
            GenericFullPaperModel(),
            writer=lambda value: writer_payloads.append(value) or "# Generic note\n",
        ).read(_candidate(), paper, ReadingIntent())

        self.assertEqual(result.receipt.strategy, "full_paper")
        preservation = writer_payloads[0]["must_preserve_facts"]
        self.assertIn("RepoGrade-X", preservation["named_entities"])
        self.assertIn("execution-conditioned judge", preservation["mechanism_terms"])
        self.assertIn("91.2%", preservation["numeric_groups"][0])
        serialized = str(preservation).casefold()
        for leaked_term in ("qwen", "broker", "toolprivbench", "safe success"):
            self.assertNotIn(leaked_term, serialized)

    def test_target_fallback_for_an_unrelated_paper_has_no_target_paper_vocabulary(self) -> None:
        class BenchmarkPaperModel(_RoutingModel):
            def __init__(self) -> None:
                super().__init__(evidence_gap=True)
                self.target_request = None

            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["thesis"] = "A benchmark measures whether software agents solve realistic repository tasks."
                response["material_unknowns"] = [{
                    "statement": "Which evaluation establishes agreement between the automatic judge and human reviewers?",
                    "priority": "high",
                }]
                return response

            def read_target(self, request: object) -> dict:
                self.target_request = request
                return {
                    "source_facts": [{
                        "fact_id": "f-judge-agreement",
                        "facet": "experiment",
                        "statement": "The automatic judge agrees with human reviewers on 91% of the audited cases.",
                        "source_block_ids": ["judge-result"],
                    }]
                }

        paper = CanonicalPaperIR(
            "paper-routing",
            "Repository benchmark",
            (
                PaperIRBlock("problem", "paragraph", "Introduction", "Existing coding benchmarks use short artificial tasks.", 1, facets=("problem",)),
                PaperIRBlock("method", "paragraph", "Benchmark construction", "The authors collect repository issues and validate patches with tests.", 2, facets=("method",)),
                PaperIRBlock("judge-setup", "paragraph", "Evaluation", "Human reviewers audit a stratified sample of judge decisions.", 3, facets=("experiment",)),
                PaperIRBlock("judge-result", "table", "Evaluation", "The automatic judge agrees with human reviewers on 91% of the audited cases.", 4, facets=("experiment",)),
                PaperIRBlock("decoy", "paragraph", "Related work", "A different security paper reports broker results in Table II at 64.36%.", 5),
                PaperIRBlock("limit", "paragraph", "Limitations", "The benchmark covers only public Python repositories.", 6, facets=("limitation",)),
            ),
        )
        model = BenchmarkPaperModel()

        result = PaperReader(model, writer=lambda value: "# Benchmark note\n").read(
            _candidate(),
            paper,
            ReadingIntent(max_targets=1),
        )

        self.assertEqual(model.target_calls, 0, "subclass captures the request without using the parent counter")
        self.assertIsNotNone(model.target_request)
        target = result.trace.targets[0]
        target_text = f"{target.task} {target.success_criteria}".casefold()
        for leaked_term in ("broker", "table ii", "64.36%", "pre-execution", "post-execution"):
            self.assertNotIn(leaked_term, target_text)
        self.assertIn("judge-result", model.target_request.bundle.source_block_ids)
        self.assertNotIn("decoy", model.target_request.bundle.source_block_ids)

    def test_invalid_full_paper_response_does_not_start_unguided_target_fallback(self) -> None:
        class InvalidFullPaperModel(_RoutingModel):
            def read_full_paper(self, request: object) -> dict:
                return {
                    "_fallback": "invalid_json",
                    "_provider_failure": {
                        "operation": "full_paper_read",
                        "kind": "invalid_json",
                        "finish_reason": "stop",
                    },
                }

        result = PaperReader(InvalidFullPaperModel(), writer=lambda value: "# 不应生成\n").read(
            _candidate(),
            _paper(),
            ReadingIntent(max_targets=6),
        )

        self.assertEqual(
            (result.receipt.status, result.receipt.stop_reason, result.receipt.target_count, result.trace.targets),
            ("failed", "failed", 0, ()),
        )
        self.assertEqual((result.receipt.full_read_calls, result.receipt.unknown_count), (1, 0))
        self.assertEqual(result.receipt.paper_ir_version, "canonical-paper-ir-v1")
        self.assertEqual(result.draft.markdown, "")

    def test_section_planning_repair_is_used_once_for_assignment_conflict_without_target(self) -> None:
        model = _RoutingModel(assignment_conflict=True)

        result = PaperReader(model, writer=lambda value: "# 已覆盖的笔记\n").read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((model.section_repair_calls, model.target_calls), (1, 0))
        self.assertEqual((result.receipt.planner_calls, result.receipt.target_count), (1, 0))

    def test_adapter_counted_section_repair_is_not_double_counted_in_receipt(self) -> None:
        class CountingRoutingModel(_RoutingModel):
            def __init__(self) -> None:
                super().__init__(assignment_conflict=True)
                self.planner_call_count = 0

            def repair_section_plan(self, request: object) -> dict:
                self.planner_call_count += 1
                return super().repair_section_plan(request)

        result = PaperReader(CountingRoutingModel(), writer=lambda value: "# 已覆盖的笔记\n").read(
            _candidate(),
            _paper(),
            ReadingIntent(),
        )

        self.assertEqual(result.receipt.planner_calls, 1)

    def test_target_fallback_requires_a_named_high_priority_evidence_gap(self) -> None:
        model = _RoutingModel(evidence_gap=True)

        result = PaperReader(model, writer=lambda value: "# 补读后的笔记\n").read(_candidate(), _paper(), ReadingIntent())

        self.assertEqual((model.section_repair_calls, model.target_calls), (0, 1))
        self.assertEqual(result.receipt.target_count, 1)
        self.assertIn("Which experiment provides the decisive evidence", result.trace.targets[0].task)

    def test_target_fallback_merges_recovered_source_facts_into_the_draft(self) -> None:
        result = PaperReader(
            _RoutingModel(evidence_gap=True),
            writer=lambda value: "# 补读后的笔记\n",
        ).read(_candidate(), _paper(), ReadingIntent(max_targets=1))

        self.assertIn("f-target-result", result.draft.source_fact_ids)
        self.assertIn("f-result", result.draft.source_fact_ids)
        self.assertIn("f-target-result", tuple(fact.fact_id for fact in result.trace.paper_model.source_facts))
        self.assertEqual(result.trace.paper_model.material_unknowns, ())
        self.assertEqual(result.trace.paper_model.coverage["experiment"]["status"], "complete")
        self.assertEqual(result.draft.unresolved_boundaries, ())
        self.assertEqual((result.receipt.full_read_calls, result.receipt.unknown_count), (1, 0))

    def test_target_fallback_repairs_a_new_ledger_conflict_once_before_writing(self) -> None:
        class UnrepairableConflictModel(_RoutingModel):
            def read_full_paper(self, request: object) -> dict:
                response = super().read_full_paper(request)
                response["sections"][0]["coverage_obligation_ids"] = ["invented:bad"]
                return response

            def repair_section_plan(self, request: object) -> dict:
                self.section_repair_calls += 1
                return {"sections": [{"section_id": "main", "coverage_obligation_ids": ["invented:bad"]}]}

        model = UnrepairableConflictModel(evidence_gap=True)
        writer_payloads: list[dict] = []

        result = PaperReader(
            model,
            writer=lambda value: writer_payloads.append(value) or "# 不应生成\n",
        ).read(_candidate(), _paper(), ReadingIntent(max_targets=1))

        self.assertEqual(len(writer_payloads), 1)
        self.assertEqual(model.section_repair_calls, 1)
        self.assertEqual((result.receipt.status, result.receipt.stop_reason), ("completed", "coverage"))
        self.assertEqual(result.receipt.ledger_conflicts, ())
        self.assertIsNotNone(result.trace.coverage_ledger)
