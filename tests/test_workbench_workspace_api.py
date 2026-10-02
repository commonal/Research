"""M4 — workspace state HTTP boundary + HITL resolve."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.hitl import HITLService
from research_pulse.workbench.workspace import WorkspaceService
from research_pulse.workbench.workspace_json import WorkspaceJsonStore
from research_pulse.workbench.workspace_pipeline import build_workspace_pipeline, commit_resolved_decision
from research_pulse.workbench.workspace_tools import WorkspaceResearchTools


def _now():
    return datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


class _Knowledge:
    def recent(self, *, limit: int):
        return ()

    def get_current(self, knowledge_id: str):
        return None


class WorkspaceApiTests(TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = WorkspaceJsonStore(self.root)
        self.service = WorkspaceService(self.store, workspace_id_factory=lambda: "ws-session-1", clock=_now)
        self.service.create(research_question="如何降低推荐位置偏差？", anchor_paper_id="paper-x")
        self.pipeline = build_workspace_pipeline(self.store, workspace_root=self.root, clock=_now)
        self.tools = WorkspaceResearchTools(
            self.service, "ws-session-1",
            gate=self.pipeline.gate,
            risk_classifier=self.pipeline.risk_classifier,
            commit_service=self.pipeline.commit_service,
            hitl_service=self.pipeline.hitl_service,
            run_id="run-1",
        )
        self.tools.ensure_initial_research()

        def service_provider(session_id):
            return self.service if session_id == "session-1" else None

        def decision_provider(session_id):
            return self.pipeline.hitl_service if session_id == "session-1" else None

        self.client = TestClient(create_app(
            knowledge_reader=_Knowledge(),
            workbench_workspace_service_provider=service_provider,
            workbench_workspace_decision_provider=decision_provider,
        ))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_get_workspace_state_projects_canonical_state(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="是否有偏置？")
        self.tools.update_subquestions("add", "Q-002", text="是否能缓解？")
        self.tools.update_research_map("M-001", label="路线", related_question_ids=["Q-001"])
        self.tools.add_evidence(
            "E-001", "paper-x", ["b31"], supports_question_ids=["Q-001"], claim="有偏置"
        )
        response = self.client.get("/api/workbench/workspaces/session-1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["workspace_id"], "ws-session-1")
        self.assertEqual(payload["status"], "initial_research")
        self.assertEqual(len(payload["subquestions"]), 2)
        self.assertEqual(payload["research_map"][0]["node_id"], "M-001")
        self.assertEqual(payload["evidence"][0]["evidence_id"], "E-001")
        self.assertEqual(payload["research_plan"]["stage"], "not_started")
        self.assertEqual(payload["research_plan"]["artifact_stage"], "stage_note")
        self.assertEqual(payload["research_plan"]["artifact"]["label"], "阶段性研究笔记")
        self.assertIn("decision_points", payload)

    def test_get_workspace_returns_404_when_not_bound(self) -> None:
        response = self.client.get("/api/workbench/workspaces/unknown-session")
        self.assertEqual(response.status_code, 404)

    def test_documents_are_user_facing_projections(self) -> None:
        response = self.client.get("/api/workbench/workspaces/session-1/documents")
        self.assertEqual(response.status_code, 200)
        documents = response.json()["items"]
        self.assertEqual([item["filename"] for item in documents], [
            "research-brief.md", "current-progress.md", "evidence-index.md", "research-plan.md",
        ])
        self.assertIn("研究议题", documents[0]["markdown"])
        self.assertIn("当前产物", documents[0]["markdown"])
        self.assertIn("初步研究中", documents[1]["markdown"])
        self.assertIn("方法路线图", documents[3]["markdown"])
        self.assertIn("研究迭代记录", documents[3]["markdown"])

    def test_research_iterations_are_projected_by_workspace_api(self) -> None:
        self.tools.update_research_plan({"iterations": [{
            "iteration_id": "I-001",
            "sequence": 1,
            "title": "第一轮：确认研究缺口",
            "status": "completed",
            "summary": "锚点论文未覆盖该问题。",
            "decision": "继续探索",
            "next_step": "检索相关工作",
        }]})
        response = self.client.get("/api/workbench/workspaces/session-1")
        self.assertEqual(response.status_code, 200)
        iteration = response.json()["research_plan"]["iterations"][0]
        self.assertEqual(iteration["iteration_id"], "I-001")
        self.assertEqual(iteration["status"], "completed")
        documents = self.client.get("/api/workbench/workspaces/session-1/documents").json()["items"]
        plan_doc = next(item for item in documents if item["filename"] == "research-plan.md")
        self.assertIn("第一轮：确认研究缺口", plan_doc["markdown"])

    def test_focus_endpoint_requires_and_persists_candidate_question(self) -> None:
        self.tools.update_subquestions("add", "Q-FOCUS", text="验证一个候选方向")
        response = self.client.post(
            "/api/workbench/workspaces/session-1/focus",
            json={"question_id": "Q-FOCUS"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active_focus_id"], "Q-FOCUS")

    def test_resolve_decision_routes_through_hitl_service(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="q")
        result = self.tools.update_subquestions("resolve", "Q-001", answer="a")
        decision_id = result["decision_point_id"]
        response = self.client.post(
            f"/api/workbench/workspaces/session-1/decisions/{decision_id}/resolve",
            json={"approved": True, "decision": "同意", "resolved_run_id": "run-2"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"]["status"], "approved")
        # Approving resolves the decision record, then the resumed run commits it.
        projected = commit_resolved_decision(self.pipeline, decision_id)
        self.assertIsNotNone(projected)
        q = self.service.list_subquestions("ws-session-1")[0]
        self.assertEqual(q.status.value, "resolved")

    def test_resolve_already_resolved_is_rejected(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="q")
        result = self.tools.update_subquestions("resolve", "Q-001", answer="a")
        decision_id = result["decision_point_id"]
        self.client.post(
            f"/api/workbench/workspaces/session-1/decisions/{decision_id}/resolve",
            json={"approved": True},
        )
        response = self.client.post(
            f"/api/workbench/workspaces/session-1/decisions/{decision_id}/resolve",
            json={"approved": True},
        )
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    import unittest

    unittest.main()
