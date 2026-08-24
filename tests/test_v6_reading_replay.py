from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from research_pulse.production.normalized import load_normalized_jsonl
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    CanonicalPaperIR,
    DeterministicReadingModel,
    PaperReader,
    ReadingIntent,
    ReadingQuestion,
    ReadingTarget,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "2608.18351v1"
NORMALIZED = ROOT / "tmp" / "reading-experiment-root" / SOURCE_ID / "normalized" / "blocks.jsonl"


def _v6_ir() -> CanonicalPaperIR:
    blocks = load_normalized_jsonl(NORMALIZED)
    return CanonicalPaperIR.from_normalized(
        source_id=SOURCE_ID,
        title="Task-Conditioned Least-Privilege Learning for Executable Terminal and MCP Agents",
        blocks=blocks,
    )


def _candidate() -> PaperCandidate:
    return PaperCandidate(SOURCE_ID, "Task-Conditioned Least-Privilege Learning", "https://arxiv.org/abs/2608.18351v1", "security")


class V6ReadingReplayTests(TestCase):
    def test_raw_block_does_not_support_question_without_target_success(self) -> None:
        result = PaperReader(DeterministicReadingModel(target_responses={"t-1": {}})).read(
            _candidate(), _v6_ir(), ReadingIntent(max_targets=1, max_expansions_per_target=1)
        )

        record = result.trace.records[0]
        self.assertFalse(record.source_facts, "raw bundle blocks must not be promoted to source facts")
        self.assertTrue(record.unknowns or record.evidence_gaps)
        self.assertNotEqual(result.trace.questions[0].status, "supported")

    def test_reading_record_source_fact_is_atomic_and_answers_target(self) -> None:
        ir = _v6_ir()
        model = DeterministicReadingModel(
            skim_response={"questions": [
                {"text": "核心机制如何回应问题？", "priority": "high"},
                {"text": "问题是什么？", "priority": "high"},
                {"text": "已有方法为什么不够？", "priority": "high"},
                {"text": "哪些实验验证机制？", "priority": "high"},
                {"text": "结论边界是什么？", "priority": "high"},
                {"text": "还有哪些未知？", "priority": "medium"},
            ]},
            target_responses={
                "t-1": {
                    "source_facts": [{
                        "fact_id": "broker-fact",
                        "facet": "method",
                        "statement": "每条轨迹先由 broker 分析提议，再进行前置和后置审计。",
                        "source_block_ids": ("normalized:2608.18351v1:text:daaf6cdff07f",),
                    }],
                }
            }
        )
        result = PaperReader(model).read(_candidate(), ir, ReadingIntent(max_targets=1))

        record = result.trace.records[0]
        self.assertTrue(record.source_facts)
        for fact in record.source_facts:
            self.assertNotIn("Abstract—", fact.statement)
            self.assertLess(len(fact.statement), len(ir.block_by_id[fact.source_block_ids[0]].text))
        self.assertTrue(any("broker" in fact.statement.lower() for fact in record.source_facts))

    def test_unanswered_success_criteria_creates_gap_and_expands_bundle(self) -> None:
        result = PaperReader(DeterministicReadingModel(target_responses={"t-1": {}})).read(
            _candidate(), _v6_ir(), ReadingIntent(max_targets=1, max_expansions_per_target=1)
        )

        self.assertGreaterEqual(len(result.trace.bundles), 2)
        self.assertTrue(result.trace.records[0].unknowns or result.trace.records[0].evidence_gaps)
        self.assertEqual(result.trace.bundles[1].previous_version, 1)
        self.assertTrue(result.trace.bundles[1].expansion_reason)

    def test_method_target_joint_bundle_recalls_broker_audits_and_figure(self) -> None:
        ir = _v6_ir()
        question = ReadingQuestion("q-method", "核心机制如何回应问题？", "high")
        target = ReadingTarget(
            "t-method", (question.question_id,), ("arg-4",),
            "解释 broker、前后置审计和相关机制块",
            "同时找到 broker、前置审计和后置审计的来源事实",
            "保留机制证据缺口",
        )
        bundle = PaperReader(DeterministicReadingModel())._initial_bundle(target, ir, question)
        selected = "\n".join(ir.block_by_id[item].text for item in bundle.source_block_ids)
        self.assertIn("broker", selected.lower())
        self.assertIn("pre-execution", selected.lower())
        self.assertIn("post-execution", selected.lower())
        self.assertTrue(any(ir.block_by_id[item].is_visual for item in bundle.source_block_ids), "Fig.1 is part of this mechanism target")

    def test_experiment_target_joint_bundle_preserves_task_table_and_result_numbers(self) -> None:
        ir = _v6_ir()
        question = ReadingQuestion("q-experiment", "哪些实验真正验证了机制？", "high")
        target = ReadingTarget(
            "t-experiment", (question.question_id,), ("arg-5",),
            "联合任务设计、Table II 和结果解释",
            "覆盖任务设计、Table II 与结果解释中的全部关键数字",
            "保留实验数字证据缺口",
        )
        bundle = PaperReader(DeterministicReadingModel())._initial_bundle(target, ir, question)
        selected = "\n".join(ir.block_by_id[item].text + "\n" + (ir.block_by_id[item].table_html or "") for item in bundle.source_block_ids)
        self.assertIn("2000", selected.replace(",", ""), "the 2,000-task catalog must be present, regardless of comma formatting")
        for token in ("1,500", "500", "2,896", "64.36%", "98.48%", "4.56%", "0.79%"):
            self.assertIn(token, selected, token)

    def test_argument_map_updates_complete_claim_nodes_each_round(self) -> None:
        responses = {
            f"t-{index}": {
                "source_facts": [{
                    "facet": facet,
                    "statement": statement,
                    "source_block_ids": ("normalized:2608.18351v1:text:4b240b4d1589",),
                }]
            }
            for index, (facet, statement) in enumerate((
                ("problem", "代理会在任务不需要时行使多余权限。"),
                ("problem", "静态权限门控会遗漏等价状态变化。"),
                ("method", "broker 对动作执行前后进行审计。"),
                ("experiment", "Table II 比较基线与训练策略。"),
            ), 1)
        }
        result = PaperReader(DeterministicReadingModel(target_responses=responses)).read(
            _candidate(), _v6_ir(), ReadingIntent(max_targets=4)
        )

        final_map = result.trace.argument_maps[-1]
        changed = [node for node in final_map.nodes if node.status != "hypothesis"]
        self.assertGreaterEqual(len(changed), 4)
        self.assertTrue(all(node.statement and node.evidence_refs and node.revision_reason for node in changed))
        self.assertTrue(any(node.statement != result.trace.argument_maps[0].nodes[index].statement for index, node in enumerate(final_map.nodes)))

    def test_planner_and_writer_receive_typed_preservation_state(self) -> None:
        planned: list[dict] = []
        written: list[dict] = []

        def planner(value: dict) -> dict:
            planned.append(value)
            return {"sections": [{"name": "方法", "paragraph_goal": "保留机制和数字。"}]}

        def writer(value: dict) -> str:
            written.append(value)
            return "# note\n"

        model = DeterministicReadingModel(target_responses={
            "t-1": {"source_facts": [{
                "facet": "problem",
                "statement": "Qwen3.5-4B was trained over 1,500 tasks to reduce authority that the task does not require.",
                "source_block_ids": ("normalized:2608.18351v1:text:4b240b4d1589",),
            }]}
        })
        result = PaperReader(model).read(
            _candidate(), _v6_ir(), ReadingIntent(max_targets=1)
        )
        # Re-run through the injected fixed downstream seam so the payload itself is observable.
        result = PaperReader(model, note_planner=planner, writer=writer).read(
            _candidate(), _v6_ir(), ReadingIntent(max_targets=1)
        )

        self.assertIn("must_preserve_facts", planned[0])
        preservation = planned[0]["must_preserve_facts"]
        self.assertTrue(preservation["facts"])
        self.assertTrue(preservation.get("numeric_groups"))
        self.assertTrue(preservation.get("named_entities"))
        self.assertIn("must_preserve_facts", written[0])
        self.assertIn("compressed_records", written[0])
        self.assertNotIn("paragraph_goal", written[0])
        self.assertEqual(result.draft.markdown, "# note\n")

    def test_fig1_visual_decision_does_not_require_cjk_english_overlap(self) -> None:
        ir = _v6_ir()
        figure = next(block for block in ir.blocks if block.kind == "figure" and "Fig. 1" in block.text)
        target = ReadingTarget(
            "t-figure", ("q-method",), ("arg-4",),
            "解释图一中的控制流和审计关系",
            "说明图一是否回答当前机制目标",
            "保留视觉证据边界",
        )

        decision = __import__("research_pulse.production.reading", fromlist=["visual_decision"]).visual_decision(figure, target)
        self.assertIn(decision.action, {"inspect", "skip"})
        self.assertTrue(decision.reason)
        self.assertEqual(decision.block_id, figure.block_id)
        self.assertEqual(decision.target_id, target.target_id)
        self.assertEqual(decision.action, "inspect")
