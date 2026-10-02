"""M5.3 — reverse acceptance (必测): the user redirects the research direction.

The agent proposes a map plus subquestions, the user says \"don't investigate Q1,
investigate Q3 first\" — the Harness must land on INVESTIGATING for Q3, never
auto-follow Q1. This drives the real domain services (tools → propose → Gate →
Risk → Commit/HITL) so the invariants are verified against the actual
canonical state, not a mock.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from research_pulse.workbench.workspace import WorkspaceService, WorkspaceStatus
from research_pulse.workbench.workspace_json import WorkspaceJsonStore
from research_pulse.workbench.workspace_pipeline import build_workspace_pipeline
from research_pulse.workbench.workspace_tools import WorkspaceResearchTools


def _now() -> datetime:
    return datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


class ReverseAcceptanceTests(TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = WorkspaceJsonStore(self.root)
        self.service = WorkspaceService(self.store, workspace_id_factory=lambda: "ws-1", clock=_now)
        self.service.create(research_question="如何降低推荐位置偏差？", anchor_paper_id="paper-x")
        self.pipeline = build_workspace_pipeline(self.store, workspace_root=self.root, clock=_now)
        self.tools = WorkspaceResearchTools(
            self.service, "ws-1",
            gate=self.pipeline.gate,
            risk_classifier=self.pipeline.risk_classifier,
            commit_service=self.pipeline.commit_service,
            hitl_service=self.pipeline.hitl_service,
            run_id="run-1",
        )
        self.tools.ensure_initial_research()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_user_redirect_to_q3_does_not_follow_q1(self) -> None:
        # Agent's initial research proposes Q1, Q2, Q3 in the map and stops for
        # a direction decision.
        self.tools.update_subquestions("add", "Q-001", text="位置偏差是否可分离？")
        self.tools.update_subquestions("add", "Q-002", text="哪种损失函数更稳？")
        self.tools.update_subquestions("add", "Q-003", text="影子变量能否补全缺失交互？")
        self.tools.update_research_map("M-001", related_question_ids=["Q-001", "Q-002", "Q-003"])
        direction_decision = self.tools.report_direction_decision()
        self.assertIsNotNone(direction_decision)
        self.assertEqual(self.service.get("ws-1").status, WorkspaceStatus.WAITING_FOR_USER_ACTION)

        # The user redirects: investigate Q3, NOT Q1. Approve the direction, then
        # the resumed run commits the redirect and starts investigating Q3.
        self.pipeline.hitl_service.resolve(
            direction_decision, decision="先调查 Q3，不要调查 Q1", approved=True, resolved_run_id="run-2",
        )
        self.service.advance_status("ws-1", WorkspaceStatus.INVESTIGATING)

        # Resumed run (resumes_from + decision_id): the agent now works Q3 ONLY.
        self.tools2 = WorkspaceResearchTools(
            self.service, "ws-1",
            gate=self.pipeline.gate,
            risk_classifier=self.pipeline.risk_classifier,
            commit_service=self.pipeline.commit_service,
            hitl_service=self.pipeline.hitl_service,
            run_id="run-2",
        )
        self.tools2.add_evidence("E-010", "paper-z", ["b1", "b2"], supports_question_ids=["Q-003"], claim="影子变量能估计缺失交互", research_interpretation="为补全 MNAR 数据提供了可观测代理", confidence="high")
        self.tools2.update_research_map("M-001", related_question_ids=["Q-001", "Q-002", "Q-003"])
        self.tools2.update_research_map("M-002", label="影子变量补全", related_question_ids=["Q-003"], evidence_ids=["E-010"])

        state = self.service.get("ws-1")
        self.assertEqual(state.status, WorkspaceStatus.INVESTIGATING)

        # Q1 was never touched by the resumed run (still open, no answer).
        q1 = next(q for q in self.service.list_subquestions("ws-1") if q.question_id == "Q-001")
        self.assertEqual(q1.status.value, "open")
        self.assertIsNone(q1.answer)

        # New evidence is tied to Q3 only, not Q1.
        evidence = {item.evidence_id: item for item in self.service.list_evidence("ws-1")}
        self.assertIn("E-010", evidence)
        self.assertEqual(evidence["E-010"].supports_question_ids, ("Q-003",))
        self.assertNotIn("Q-001", evidence["E-010"].supports_question_ids)

        # Research map grew a Q3 branch; Q1 has no dedicated branch or new link.
        nodes = {node.node_id: node for node in self.service.list_research_map("ws-1")}
        self.assertIn("M-002", nodes)
        self.assertEqual(nodes["M-002"].related_question_ids, ("Q-003",))
        self.assertEqual(nodes["M-002"].evidence_ids, ("E-010",))
        # M-001 still references the original question set — no Q1-only progression.
        self.assertEqual(set(nodes["M-001"].related_question_ids), {"Q-001", "Q-002", "Q-003"})

        # No run/tool kept driving along Q1: the workspace is INVESTIGATING (a
        # deliberate state), and no Q1 evidence was produced.
        self.assertFalse(any(e.evidence_id == "E-010" and "Q-001" in e.supports_question_ids for e in self.service.list_evidence("ws-1")))


if __name__ == "__main__":
    import unittest

    unittest.main()
