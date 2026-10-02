"""Controlled workspace write tools: propose → Gate → Risk → Commit/HITL.

The controlled tools now express a ``WorkspacePatch`` and route it through the
domain pipeline: additive/structural writes commit (auto/review); a semantic
reversal (resolve a subquestion) suspends as a DecisionPoint (hitl).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.workbench.workspace import (
    HypothesisStatus,
    ResearchIteration,
    ResearchIterationStatus,
    ResearchPlanStage,
    Researchability,
    WorkspaceService,
    WorkspaceStatus,
)
from research_pulse.workbench.workspace_json import WorkspaceJsonStore
from research_pulse.workbench.workspace_pipeline import build_workspace_pipeline
from research_pulse.workbench.workspace_tools import (
    WorkspaceResearchTools,
    WorkspaceToolError,
)


def _now():
    return __import__("datetime").datetime(2026, 9, 2, 10, 0)


def _make_tools(root: Path, workspace_id: str = "ws-1", *, run_id: str = "run-1") -> WorkspaceResearchTools:
    store = WorkspaceJsonStore(root)
    service = WorkspaceService(
        store,
        workspace_id_factory=lambda: workspace_id,
        clock=_now,
    )
    service.create(research_question="如何降低推荐位置偏差？", anchor_paper_id="paper-x")
    pipeline = build_workspace_pipeline(store, workspace_root=root, clock=_now)
    return WorkspaceResearchTools(
        service, workspace_id,
        gate=pipeline.gate,
        risk_classifier=pipeline.risk_classifier,
        commit_service=pipeline.commit_service,
        hitl_service=pipeline.hitl_service,
        run_id=run_id,
    )


class WorkspaceResearchToolsTests(TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.tools = _make_tools(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_read_state_returns_canonical_snapshot(self) -> None:
        state = self.tools.read_workspace_state()
        self.assertEqual(state["workspace_id"], "ws-1")
        self.assertEqual(state["research_question"], "如何降低推荐位置偏差？")
        self.assertEqual(state["status"], "created")
        self.assertEqual(state["subquestions"], [])
        self.assertEqual(state["research_map"], [])
        self.assertEqual(state["research_plan"]["stage"], "not_started")
        self.assertEqual(state["research_plan"]["artifact_stage"], "overview")

    def test_add_subquestion_commits(self) -> None:
        # add is review risk → commits automatically and returns the produced row.
        added = self.tools.update_subquestions("add", "Q-001", text="是否有偏置？")
        self.assertEqual(added["question_id"], "Q-001")
        self.assertEqual(added["status"], "open")
        state = self.tools.read_workspace_state()
        self.assertEqual(len(state["subquestions"]), 1)

    def test_resolve_subquestion_requires_user_decision(self) -> None:
        # resolve is hitl risk → suspends as a DecisionPoint, does not commit.
        self.tools.update_subquestions("add", "Q-001", text="是否有偏置？")
        result = self.tools.update_subquestions("resolve", "Q-001", answer="是")
        self.assertEqual(result["status"], "awaiting_decision")
        self.assertEqual(result["decision_kind"], "patch_approval")
        self.assertIsNotNone(result["decision_point_id"])
        self.assertEqual(self.tools.pending_decision_id, result["decision_point_id"])
        state = self.tools.read_workspace_state()
        # The resolved status is NOT committed; a decision point stands instead.
        q1 = next(q for q in state["subquestions"] if q["question_id"] == "Q-001")
        self.assertEqual(q1["status"], "open")
        self.assertEqual(state["status"], "waiting_for_user_action")

    def test_illegal_hitl_transition_fails_before_creating_decision(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="是否有偏置？")
        self.tools._service.archive("ws-1")

        with self.assertRaisesRegex(ValueError, "illegal workspace transition"):
            self.tools.update_subquestions("resolve", "Q-001", answer="是")

        self.assertEqual(
            (), self.tools._orchestrator._hitl.list_for_workspace("ws-1")
        )
        self.assertEqual(
            "archived", self.tools.read_workspace_state()["status"]
        )

    def test_update_subquestion_rejects_unknown_operation(self) -> None:
        with self.assertRaises(WorkspaceToolError):
            self.tools.update_subquestions("overwrite", "Q-001", text="x")

    def test_add_evidence_requires_source_and_block_ids(self) -> None:
        with self.assertRaises(WorkspaceToolError):
            self.tools.add_evidence("E-012", "", ["b31"])  # blank source
        with self.assertRaises(WorkspaceToolError):
            self.tools.add_evidence("E-012", "paper-x", [])  # no blocks
        with self.assertRaises(WorkspaceToolError):
            self.tools.add_evidence("", "paper-x", ["b31"])  # blank id

    def test_add_evidence_stores_claim_not_paper_body(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="是否有偏置？")
        produced = self.tools.add_evidence(
            "E-012", "paper-x", ["b31", "b32"],
            supports_question_ids=["Q-001"], claim="论文证明了有偏置", research_interpretation="对当前问题意味着要对齐",
        )
        self.assertEqual(produced["evidence_id"], "E-012")
        state = self.tools.read_workspace_state()
        self.assertEqual(len(state["evidence"]), 1)
        self.assertEqual(state["evidence"][0]["supports_question_ids"], ["Q-001"])

    def test_conflicting_evidence_is_hitl_and_suspends(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="是否有偏置？")
        result = self.tools.add_evidence(
            "E-012", "paper-x", ["b31"], supports_question_ids=["Q-001"], evidence_role="conflicting",
        )
        self.assertEqual(result["status"], "awaiting_decision")
        self.assertEqual(len(self.tools.read_workspace_state()["evidence"]), 0)

    def test_update_research_map_adds_and_merges_by_node_id(self) -> None:
        # A map node must reference existing subquestions (gate integrity).
        self.tools.update_subquestions("add", "Q-001", text="q")
        self.tools.update_subquestions("add", "Q-003", text="q3")
        added = self.tools.update_research_map("M-007", label="DPO 路线", related_question_ids=["Q-001"])
        self.assertEqual(added["node_id"], "M-007")
        updated = self.tools.update_research_map("M-007", related_question_ids=["Q-003"])
        state = self.tools.read_workspace_state()
        node = next(n for n in state["research_map"] if n["node_id"] == "M-007")
        # update sets the related ids to the given set (sibling label preserved).
        self.assertEqual(node["related_question_ids"], ["Q-003"])
        self.assertEqual(node["label"], "DPO 路线")

    def test_update_research_map_requires_node_id(self) -> None:
        with self.assertRaises(WorkspaceToolError):
            self.tools.update_research_map("  ")

    def test_update_research_plan_commits_draft_and_preserves_typed_shape(self) -> None:
        result = self.tools.update_research_plan({
            "stage": "hypothesis_review",
            "hypotheses": [{
                "hypothesis_id": "H-001",
                "text": "干预是否减少位置偏差？",
                "rationale": "锚点论文暴露了该缺口",
                "falsifiers": ["离线指标无改善"],
            }],
        })
        self.assertEqual(result["status"], "committed")
        state = self.tools.read_workspace_state()
        self.assertEqual(state["research_plan"]["stage"], "hypothesis_review")
        self.assertEqual(state["research_plan"]["hypotheses"][0]["hypothesis_id"], "H-001")

    def test_research_iteration_history_is_persisted_and_append_only(self) -> None:
        first = {
            "iteration_id": "I-001",
            "sequence": 1,
            "title": "第一轮：确认锚点论文边界",
            "status": "completed",
            "summary": "确认需要补充外部证据。",
            "candidate_question_ids": [],
            "evidence_ids": [],
            "decision": "继续探索",
            "next_step": "检索相关论文",
        }
        self.tools.update_research_plan({"iterations": [first]})
        state = self.tools.read_workspace_state()
        self.assertEqual(state["research_plan"]["iterations"][0]["iteration_id"], "I-001")

        second = {
            "iteration_id": "I-002",
            "sequence": 2,
            "title": "第二轮：补充相关工作",
            "status": "in_progress",
            "summary": "开始验证候选方向。",
            "candidate_question_ids": [],
            "evidence_ids": [],
            "next_step": "读取候选论文",
        }
        result = self.tools.update_research_plan({"iterations": [first, second]})
        self.assertEqual(result["status"], "committed")
        persisted = self.tools.read_workspace_state()["research_plan"]["iterations"]
        self.assertEqual([item["iteration_id"] for item in persisted], ["I-001", "I-002"])

    def test_research_iteration_history_cannot_be_silently_dropped(self) -> None:
        self.tools.update_research_plan({"iterations": [{
            "iteration_id": "I-001",
            "sequence": 1,
            "title": "第一轮",
        }]})
        with self.assertRaises(WorkspaceToolError) as error:
            self.tools.update_research_plan({"iterations": [{
                "iteration_id": "I-002",
                "sequence": 2,
                "title": "第二轮",
            }]})
        self.assertIn("preserve existing history", str(error.exception))

    def test_runtime_iteration_append_does_not_replace_other_plan_artifacts(self) -> None:
        self.tools.update_research_plan({
            "stage": "hypothesis_review",
            "hypotheses": [{
                "hypothesis_id": "H-001",
                "text": "偏差修正是否改善排序？",
            }],
        })
        result = self.tools.append_research_iteration(ResearchIteration(
            iteration_id="ITER-run-1",
            sequence=1,
            title="第一轮",
            status=ResearchIterationStatus.COMPLETED,
            summary="已完成一轮探索",
            run_id="run-1",
        ))
        self.assertEqual(result["status"], "committed")
        plan = self.tools.read_workspace_state()["research_plan"]
        self.assertEqual(plan["stage"], "hypothesis_review")
        self.assertEqual(plan["hypotheses"][0]["hypothesis_id"], "H-001")
        self.assertEqual(plan["iterations"][0]["iteration_id"], "ITER-run-1")

        # Recovery may reconstruct a receipt with a different local id.  The
        # stable run_id must still make the append idempotent.
        duplicate = self.tools.append_research_iteration(ResearchIteration(
            iteration_id="ITER-recovered-run-1",
            sequence=99,
            title="恢复后的重复轮次",
            status=ResearchIterationStatus.COMPLETED,
            run_id="run-1",
        ))
        self.assertEqual(duplicate["status"], "already_recorded")
        self.assertEqual(
            len(self.tools.read_workspace_state()["research_plan"]["iterations"]),
            1,
        )

    def test_runtime_iteration_update_recovers_an_abandoned_round(self) -> None:
        self.tools.append_research_iteration(ResearchIteration(
            iteration_id="ITER-recoverable",
            sequence=1,
            title="第一轮：预算中断",
            status=ResearchIterationStatus.ABANDONED,
            run_id="logical-run-recoverable",
            summary="上一 Attempt 在预算耗尽后停止。",
        ))
        result = self.tools.update_research_iteration(ResearchIteration(
            iteration_id="ITER-recoverable",
            sequence=1,
            title="第一轮：预算中断",
            status=ResearchIterationStatus.COMPLETED,
            run_id="logical-run-recoverable",
            summary="续跑后完成本轮研究。",
        ))
        self.assertEqual(result["status"], "committed")
        iteration = self.tools.read_workspace_state()["research_plan"]["iterations"][0]
        self.assertEqual(iteration["status"], "completed")
        self.assertEqual(iteration["summary"], "续跑后完成本轮研究。")

    def test_selected_hypothesis_requires_explicit_user_decision(self) -> None:
        result = self.tools.update_research_plan({
            "stage": ResearchPlanStage.HYPOTHESIS_REVIEW.value,
            "hypotheses": [{
                "hypothesis_id": "H-001",
                "text": "干预是否减少位置偏差？",
                "status": HypothesisStatus.SELECTED.value,
            }],
        })
        self.assertEqual(result["status"], "awaiting_decision")
        self.assertEqual(result["decision_kind"], "patch_approval")
        self.assertEqual(self.tools.read_workspace_state()["research_plan"]["hypotheses"], [])

    def test_subquestion_operations_never_overwrite_other_questions(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="第一问")
        self.tools.update_subquestions("add", "Q-002", text="第二问")
        # Deprioritizing Q-001 must leave Q-002 untouched (deprioritize is review).
        self.tools.update_subquestions("deprioritize", "Q-001")
        state = self.tools.read_workspace_state()
        q1 = next(q for q in state["subquestions"] if q["question_id"] == "Q-001")
        q2 = next(q for q in state["subquestions"] if q["question_id"] == "Q-002")
        self.assertEqual(q1["status"], "deprioritized")
        self.assertEqual(q2["status"], "open")
        self.assertEqual(q2["text"], "第二问")

    def test_hitl_write_transitions_workspace_to_waiting_for_user_action(self) -> None:
        # Move the workspace to INVESTIGATING so the HITL transition is legal.
        self.tools._service.advance_status("ws-1", WorkspaceStatus.INITIAL_RESEARCH)
        self.tools._service.advance_status("ws-1", WorkspaceStatus.WAITING_FOR_USER_ACTION)
        self.tools._service.advance_status("ws-1", WorkspaceStatus.INVESTIGATING)
        self.tools.update_subquestions("add", "Q-001", text="q")
        result = self.tools.update_subquestions("resolve", "Q-001", answer="a")
        self.assertEqual(result["status"], "awaiting_decision")
        state = self.tools.read_workspace_state()
        self.assertEqual(state["status"], WorkspaceStatus.WAITING_FOR_USER_ACTION.value)

    def test_gate_violation_is_rejected(self) -> None:
        # Adding a subquestion that its evidence references before it exists is
        # fine within one patch, but adding evidence referencing an unknown
        # question in a separate patch is a gate violation.
        self.tools.update_subquestions("add", "Q-001", text="q")
        with self.assertRaises(WorkspaceToolError):
            self.tools.add_evidence("E-012", "paper-x", ["b31"], supports_question_ids=["Q-NOT-EXIST"])

    def test_ensure_initial_research_advances_fresh_workspace(self) -> None:
        self.tools.ensure_initial_research()
        self.assertEqual(self.tools.read_workspace_state()["status"], "initial_research")

    def test_initial_report_pauses_for_research_direction(self) -> None:
        # The agent made a map + a subquestion; the initial report (INITIAL_RESEARCH)
        # must raise a research_direction decision and pause the workspace.
        self.tools.ensure_initial_research()
        self.tools.update_subquestions("add", "Q-001", text="是否有偏置？")
        self.tools.update_research_map("M-001", related_question_ids=["Q-001"])
        decision_id = self.tools.report_direction_decision()
        self.assertIsNotNone(decision_id)
        state = self.tools.read_workspace_state()
        self.assertEqual(state["status"], "waiting_for_user_action")

    def test_begin_investigation_enters_investigating_after_focus_decision(self) -> None:
        """The next user turn must leave the visible waiting state."""
        self.tools.ensure_initial_research()
        self.tools.update_subquestions("add", "Q-FOCUS", text="验证一个候选方向")
        self.tools.update_research_map("M-FOCUS", related_question_ids=["Q-FOCUS"])
        decision_id = self.tools.report_direction_decision()
        self.assertIsNotNone(decision_id)

        self.tools._service.set_active_focus("ws-1", "Q-FOCUS")
        self.tools._orchestrator._hitl.resolve(
            decision_id,
            decision="选择 Q-FOCUS",
            approved=True,
            resolved_run_id="run-2",
        )
        self.assertEqual(self.tools.read_workspace_state()["status"], "waiting_for_user_action")

        self.tools.begin_investigation()

        self.assertEqual(self.tools.read_workspace_state()["status"], "investigating")

    def test_begin_investigation_does_not_bypass_pending_hitl(self) -> None:
        self.tools.ensure_initial_research()
        self.tools.update_subquestions("add", "Q-FOCUS", text="验证一个候选方向")
        self.tools.update_research_map("M-FOCUS", related_question_ids=["Q-FOCUS"])
        self.tools.report_direction_decision()
        self.tools._service.set_active_focus("ws-1", "Q-FOCUS")

        self.tools.begin_investigation()

        self.assertEqual(self.tools.read_workspace_state()["status"], "waiting_for_user_action")

    def test_paper_boundary_does_not_trigger_follow_up_decision(self) -> None:
        self.tools.ensure_initial_research()
        self.tools.update_subquestions(
            "add", "Q-BOUNDARY", text="作者没有评估在线长期收益", researchability="boundary"
        )
        self.tools.update_research_map("M-BOUNDARY", related_question_ids=["Q-BOUNDARY"])
        self.assertIsNone(self.tools.report_direction_decision())
        self.assertEqual(self.tools.read_workspace_state()["status"], "initial_research")

    def test_unknown_researchability_is_fail_closed(self) -> None:
        self.tools.ensure_initial_research()
        self.tools.update_subquestions(
            "add", "Q-UNKNOWN", text="这个限制是否值得继续研究？", researchability="unknown"
        )
        self.tools.update_research_map("M-UNKNOWN", related_question_ids=["Q-UNKNOWN"])
        self.assertIsNone(self.tools.report_direction_decision())

    def test_report_direction_does_not_pause_after_direction_chosen(self) -> None:
        # Once INVESTIGATING, a completed report round-trip keeps auto-driving.
        self.tools.ensure_initial_research()
        self.tools.update_subquestions("add", "Q-001", text="q")
        self.tools.update_research_map("M-001", related_question_ids=["Q-001"])
        self.tools.report_direction_decision()
        self.tools._service.advance_status("ws-1", WorkspaceStatus.INVESTIGATING)
        self.assertIsNone(self.tools.report_direction_decision())

    def test_later_round_can_offer_a_new_candidate_focus(self) -> None:
        self.tools.ensure_initial_research()
        self.tools.update_subquestions("add", "Q-FOCUS", text="验证当前焦点")
        self.tools.update_subquestions("add", "Q-NEXT", text="比较下一种方法")
        self.tools.update_research_map("M-001", related_question_ids=["Q-FOCUS", "Q-NEXT"])
        self.tools._service.set_active_focus("ws-1", "Q-FOCUS")
        self.tools._service.advance_status("ws-1", WorkspaceStatus.WAITING_FOR_USER_ACTION)
        self.tools._service.advance_status("ws-1", WorkspaceStatus.INVESTIGATING)

        decision_id = self.tools.report_direction_decision()

        self.assertIsNotNone(decision_id)
        decision = self.tools._orchestrator._hitl.get(decision_id)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.candidate_question_ids, ("Q-NEXT",))
        self.assertIn("本轮研究已完成", decision.prompt)
        self.assertEqual(
            self.tools.read_workspace_state()["status"],
            WorkspaceStatus.WAITING_FOR_USER_ACTION.value,
        )

    def test_runtime_can_finish_later_round_without_auto_direction_checkpoint(self) -> None:
        self.tools.ensure_initial_research()
        self.tools.update_subquestions("add", "Q-FOCUS", text="验证当前焦点")
        self.tools.update_subquestions("add", "Q-NEXT", text="比较下一种方法")
        self.tools.update_research_map("M-001", related_question_ids=["Q-FOCUS", "Q-NEXT"])
        self.tools._service.set_active_focus("ws-1", "Q-FOCUS")
        self.tools._service.advance_status("ws-1", WorkspaceStatus.WAITING_FOR_USER_ACTION)
        self.tools._service.advance_status("ws-1", WorkspaceStatus.INVESTIGATING)

        self.assertIsNone(self.tools.report_direction_decision(allow_later=False))
        self.assertEqual(
            self.tools.read_workspace_state()["status"],
            WorkspaceStatus.INVESTIGATING.value,
        )

    def test_iteration_promotes_artifact_from_overview_to_stage_note(self) -> None:
        self.tools.ensure_initial_research()
        iteration = ResearchIteration(
            iteration_id="I-001",
            sequence=1,
            title="第一轮：形成阶段结论",
            status=ResearchIterationStatus.COMPLETED,
            run_id="run-1",
            summary="已保存一轮可追溯证据。",
        )
        result = self.tools.append_research_iteration(iteration)
        self.assertEqual(result["status"], "committed")
        state = self.tools.read_workspace_state()
        self.assertEqual(state["research_plan"]["artifact_stage"], "stage_note")

    def test_invoke_routes_by_tool_name(self) -> None:
        result = self.tools.invoke("read_workspace_state", {})
        self.assertEqual(result["workspace_id"], "ws-1")
        with self.assertRaises(WorkspaceToolError):
            self.tools.invoke("write_file", {"path": "workspace.json"})


if __name__ == "__main__":
    import unittest

    unittest.main()
