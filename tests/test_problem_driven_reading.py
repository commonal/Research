from __future__ import annotations

from unittest import TestCase

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    ArgumentMap,
    ArgumentNode,
    CanonicalPaperIR,
    DeterministicReadingModel,
    EvidenceBundle,
    EvidenceGap,
    PaperIRBlock,
    PaperReader,
    ReadingIntent,
    ReadingQuestion,
    ReadingRecord,
    ReadingTarget,
    SourceFact,
    StopController,
    TransportUnit,
    VisualInterpretation,
    apply_argument_patch,
    route_block,
    source_fact_satisfies_facet,
)


def _candidate() -> PaperCandidate:
    return PaperCandidate("paper-2", "Selective reading", "https://example.com/paper-2", "agents")


def _ir() -> CanonicalPaperIR:
    return CanonicalPaperIR(
        "paper-2",
        "Selective reading",
        (
            PaperIRBlock("intro", "paragraph", "introduction", "The problem is excess authority.", 1, facets=("problem",)),
            PaperIRBlock("method", "paragraph", "method", "The broker enforces a task-relative envelope.", 3, facets=("method",)),
            PaperIRBlock("figure", "figure", "method", "Figure 1 control flow", 4, caption="Broker flow", image_path="safe.png", safe_image=True),
            PaperIRBlock("result", "table", "experiments", "Safe success improves.", 8, table_html="<table><tr><td>safe</td></tr></table>", facets=("experiment",)),
            PaperIRBlock("limit", "paragraph", "limitations", "The result does not replace a sandbox.", 12, facets=("limitation",)),
        ),
    )


class ProblemDrivenReadingTests(TestCase):
    def test_fixed_writer_receives_only_compressed_final_reading_state(self) -> None:
        planned: list[dict] = []
        written: list[dict] = []

        def planner(value: dict) -> dict:
            planned.append(value)
            return {"sections": [("方法", "固定规划器收到的是论文级阅读状态。")]}

        def writer(value: dict) -> str:
            written.append(value)
            return "# 固定写作者\n\n## 方法\n\n论文级阅读状态。\n"

        result = PaperReader(DeterministicReadingModel(), note_planner=planner, writer=writer).read(
            _candidate(), _ir(), ReadingIntent(max_targets=1)
        )

        self.assertIn("final_argument_map", planned[0])
        self.assertIn("compressed_records", planned[0])
        self.assertIn("unresolved_boundaries", planned[0])
        self.assertIn("evidence_references", planned[0])
        self.assertNotIn("transport_units", planned[0])
        self.assertNotIn("provider_payload", planned[0])
        self.assertEqual(result.draft.markdown, "# 固定写作者\n\n## 方法\n\n论文级阅读状态。\n")
        self.assertNotIn("normalized:", result.draft.markdown)

    def test_global_skim_keeps_v0_hypotheses_and_prioritizes_five_to_seven_questions(self) -> None:
        model = DeterministicReadingModel(
            skim_response={
                "paper_type": "security method",
                "questions": [
                    {"text": f"Question {i}", "priority": "high" if i < 5 else "medium"}
                    for i in range(6)
                ],
            }
        )
        result = PaperReader(model).read(_candidate(), _ir(), ReadingIntent(max_targets=6))

        self.assertEqual(len(result.trace.questions), 6)
        self.assertTrue(all(item.priority in {"high", "medium", "low"} for item in result.trace.questions))
        self.assertEqual(result.trace.argument_maps[0].version, 0)
        self.assertTrue(all(node.status == "hypothesis" for node in result.trace.argument_maps[0].nodes))
        self.assertTrue(result.trace.skeleton.provisional)

    def test_question_and_target_are_many_to_many_and_transport_is_not_reasoning_state(self) -> None:
        questions = (
            ReadingQuestion("q-1", "Why is the problem hard?", "high"),
            ReadingQuestion("q-2", "What validates the mechanism?", "high"),
        )
        target = ReadingTarget("t-1", ("q-1", "q-2"), ("arg-1",), "Read method and experiment jointly.", "Both links are explained.", "Keep the gap.")
        bundle = EvidenceBundle("t-1", 1, ("method", "result"), {"method": "mechanism", "result": "validation"})
        unit = TransportUnit("u-1", ("method",), "method text", 3, 3)

        self.assertEqual(set(target.question_ids), {question.question_id for question in questions})
        self.assertEqual(unit.block_ids, ("method",))
        self.assertFalse(hasattr(unit, "status"))
        self.assertFalse(hasattr(unit, "unknowns"))
        self.assertEqual(bundle.target_id, target.target_id)

    def test_bundle_expansion_is_recorded_and_record_versions_preserve_gap(self) -> None:
        model = DeterministicReadingModel(
            target_responses={
                "t-1": {
                    "unknowns": [{"statement": "Which experiment validates the mechanism?", "priority": "high"}],
                    "evidence_gaps": [{"statement": "Need the result block.", "requested_facets": ["experiment"]}],
                }
            }
        )
        result = PaperReader(model).read(_candidate(), _ir(), ReadingIntent(max_targets=1, max_expansions_per_target=1))

        self.assertGreaterEqual(len(result.trace.bundles), 2)
        self.assertEqual(result.trace.bundles[1].previous_version, 1)
        self.assertIn("result", result.trace.bundles[1].added_block_ids)
        self.assertEqual(result.trace.records[-1].previous_version, 1)
        self.assertTrue(result.trace.records[0].evidence_gaps)

    def test_conservative_patches_and_stop_controller_keep_boundaries(self) -> None:
        initial = ArgumentMap(0, (ArgumentNode("arg-1", "The representation is the whole innovation"),))
        revised = apply_argument_patch(initial, node_id="arg-1", status="revised", evidence_refs=("record-1",), reason="Detailed reading narrows the claim.")
        rejected = apply_argument_patch(revised, node_id="arg-1", status="rejected", evidence_refs=("record-2",), reason="Contradictory experiment.")
        question = ReadingQuestion("q-1", "Core question", "high", status="unanswered")
        decision = StopController().decide((question,), (), target_count=1, expansion_count=0, intent=ReadingIntent(max_targets=1))

        self.assertEqual(revised.nodes[0].status, "revised")
        self.assertEqual(rejected.nodes[0].status, "rejected")
        self.assertEqual(rejected.previous_version, revised.version)
        self.assertEqual(decision.reason, "budget")
        self.assertIn("q-1", decision.unresolved_high_priority)

    def test_coverage_does_not_stop_before_a_medium_core_question_is_read(self) -> None:
        questions = (
            ReadingQuestion("q-high", "Core", "high", status="supported"),
            ReadingQuestion("q-medium", "Limitations", "medium", status="unanswered"),
        )
        decision = StopController().decide(questions, (), target_count=1, expansion_count=0, intent=ReadingIntent(max_targets=4))
        self.assertFalse(decision.should_stop)

    def test_routing_and_facet_boundary_do_not_promote_visual_or_synthesis(self) -> None:
        target = ReadingTarget("t", ("q",), (), "Understand the method diagram", "diagram explains flow", "unknown")
        figure = _ir().block_by_id["figure"]
        formula = PaperIRBlock("formula", "formula", "method", r"z(a_t)=f(a_t)", 5, latex=r"z(a_t)=f(a_t)")
        self.assertEqual(route_block(figure, target), "vision")
        self.assertEqual(route_block(formula, target), "text")
        record = ReadingRecord(
            "t", ("q",), 1, 1, ("figure",),
            visual_interpretations=(VisualInterpretation("figure", "The arrows show a flow.", "t"),),
            evidence_gaps=(EvidenceGap("g", "Missing author fact", ("method",)),),
        )
        self.assertFalse(source_fact_satisfies_facet(record, "method"))
        self.assertFalse(source_fact_satisfies_facet(record, "experiment"))
