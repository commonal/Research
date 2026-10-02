"""M3.6 — HITLService: DecisionPoint persistence and resolve lifecycle."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.workbench.hitl import (
    DecisionKind,
    DecisionPointError,
    DecisionPointJsonStore,
    DecisionStatus,
    HITLService,
)
from research_pulse.workbench.workspace_gate import (
    SUBQUESTION_ADD,
    SUBQUESTION_RESOLVE,
    WorkspacePatch,
    WorkspacePatchOperation,
)


def _now():
    return __import__("datetime").datetime(2026, 9, 2, 11, 0)


def _service(store, *, counter=lambda: "D-007"):
    return HITLService(store, decision_id_factory=counter, clock=_now)


class HITLServiceTests(TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.store = DecisionPointJsonStore(self._temp.name)
        self.service = _service(self.store)
        self.patch = WorkspacePatch(
            base_workspace_revision=7,
            provenance="agent:run-021",
            operations=(
                WorkspacePatchOperation(kind=SUBQUESTION_RESOLVE, object_id="Q-003", answer="已答"),
            ),
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_create_patch_approval_decision_point(self) -> None:
        decision = self.service.create_decision_point(
            workspace_id="ws-1", run_id="run-021", kind=DecisionKind.PATCH_APPROVAL,
            prompt="把 Q-003 标记为 resolved？", proposed_patch=self.patch,
        )
        self.assertEqual(decision.decision_id, "D-007")
        self.assertEqual(decision.kind, DecisionKind.PATCH_APPROVAL)
        self.assertEqual(decision.status, DecisionStatus.PENDING)
        self.assertEqual(decision.proposed_patch.base_workspace_revision, 7)
        self.assertIsNotNone(decision.created_at)

    def test_create_research_direction_decision_point_without_patch(self) -> None:
        decision = self.service.create_decision_point(
            workspace_id="ws-1", run_id="run-018", kind=DecisionKind.RESEARCH_DIRECTION,
            prompt="优先深入哪条路线？",
        )
        self.assertEqual(decision.kind, DecisionKind.RESEARCH_DIRECTION)
        self.assertIsNone(decision.proposed_patch)

    def test_list_for_workspace_scopes_by_workspace(self) -> None:
        self.service.create_decision_point(
            workspace_id="ws-1", run_id="run-021", kind=DecisionKind.PATCH_APPROVAL,
            prompt="p", proposed_patch=self.patch,
        )
        self.service.create_decision_point(
            workspace_id="ws-2", run_id="run-022", kind=DecisionKind.RESEARCH_DIRECTION,
            prompt="q",
        )
        self.assertEqual(len(self.service.list_for_workspace("ws-1")), 1)
        self.assertEqual(len(self.service.list_for_workspace("ws-2")), 1)

    def test_resolve_approved_records_decision_and_run(self) -> None:
        decision = self.service.create_decision_point(
            workspace_id="ws-1", run_id="run-021", kind=DecisionKind.PATCH_APPROVAL,
            prompt="p", proposed_patch=self.patch,
        )
        resolved = self.service.resolve(
            decision.decision_id, decision="批准", approved=True, resolved_run_id="run-022",
        )
        self.assertEqual(resolved.status, DecisionStatus.APPROVED)
        self.assertEqual(resolved.decision, "批准")
        self.assertEqual(resolved.resolved_run_id, "run-022")
        self.assertIsNotNone(resolved.resolved_at)
        self.assertEqual(self.service.get(decision.decision_id).status, DecisionStatus.APPROVED)

    def test_resolve_rejected_records_status(self) -> None:
        decision = self.service.create_decision_point(
            workspace_id="ws-1", run_id="run-021", kind=DecisionKind.PATCH_APPROVAL,
            prompt="p", proposed_patch=self.patch,
        )
        resolved = self.service.resolve(
            decision.decision_id, decision="先查 Q-003", approved=False, resolved_run_id="run-022",
        )
        self.assertEqual(resolved.status, DecisionStatus.REJECTED)

    def test_double_resolve_is_rejected(self) -> None:
        decision = self.service.create_decision_point(
            workspace_id="ws-1", run_id="run-021", kind=DecisionKind.PATCH_APPROVAL,
            prompt="p", proposed_patch=self.patch,
        )
        self.service.resolve(decision.decision_id, decision="ok", approved=True, resolved_run_id="run-022")
        with self.assertRaises(DecisionPointError):
            self.service.resolve(decision.decision_id, decision="no", approved=False, resolved_run_id="run-023")

    def test_decision_point_survives_service_restart(self) -> None:
        self.service.create_decision_point(
            workspace_id="ws-1", run_id="run-021", kind=DecisionKind.PATCH_APPROVAL,
            prompt="p", proposed_patch=self.patch,
        )
        restarted = _service(self.store, counter=lambda: "D-999")
        recovered = restarted.get("D-007")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.kind, DecisionKind.PATCH_APPROVAL)
        self.assertEqual(recovered.proposed_patch.base_workspace_revision, 7)
        self.assertEqual(recovered.proposed_patch.operations[0].kind, SUBQUESTION_RESOLVE)
        self.assertEqual(recovered.proposed_patch.operations[0].object_id, "Q-003")

    def test_reloaded_counter_does_not_overwrite_persisted_decision(self) -> None:
        first = _service(self.store, counter=lambda: "D-101")
        created = first.create_decision_point(
            workspace_id="ws-1", run_id="run-021", kind=DecisionKind.RESEARCH_DIRECTION,
            prompt="第一轮方向？",
        )
        self.assertEqual(created.decision_id, "D-101")

        # A development hot reload recreates HITLService and resets the
        # process-local counter.  The new decision must get a fresh durable id
        # instead of replacing D-101.json.
        reloaded = _service(self.store, counter=lambda: "D-101")
        next_decision = reloaded.create_decision_point(
            workspace_id="ws-1", run_id="run-022", kind=DecisionKind.RESEARCH_DIRECTION,
            prompt="第二轮方向？",
        )
        self.assertEqual(next_decision.decision_id, "D-102")
        self.assertEqual(first.get("D-101").prompt, "第一轮方向？")
        self.assertEqual(reloaded.get("D-102").prompt, "第二轮方向？")

    def test_new_decision_store_can_start_from_missing_root(self) -> None:
        root = Path(self._temp.name) / "not-created-yet"
        service = _service(DecisionPointJsonStore(root), counter=lambda: "D-201")
        decision = service.create_decision_point(
            workspace_id="ws-1", run_id="run-023", kind=DecisionKind.RESEARCH_DIRECTION,
            prompt="从空存储开始？",
        )
        self.assertEqual(decision.decision_id, "D-201")


from research_pulse.workbench.workspace import WorkspaceService
from research_pulse.workbench.workspace_json import WorkspaceJsonStore
from research_pulse.workbench.workspace_pipeline import build_workspace_pipeline, commit_resolved_decision
from research_pulse.workbench.workspace_tools import WorkspaceResearchTools


def _now():
    return __import__("datetime").datetime(2026, 9, 2, 10, 0)


class HITLResumeCommitTests(TestCase):
    """M3.6 — an approved patch_approval decision re-validates and commits."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = WorkspaceJsonStore(self.root)
        self.service = WorkspaceService(
            self.store,
            workspace_id_factory=lambda: "ws-1",
            clock=_now,
        )
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

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_approved_decision_commits_patch_and_bumps_revision(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="q")
        result = self.tools.update_subquestions("resolve", "Q-001", answer="a")
        decision_id = result["decision_point_id"]
        # The patch is NOT committed while pending.
        q = self.service.list_subquestions("ws-1")[0]
        self.assertEqual(q.status.value, "open")

        # User approves; the resumed run re-validates + commits.
        self.pipeline.hitl_service.resolve(
            decision_id, decision="approved", approved=True, resolved_run_id="run-2"
        )
        projected = commit_resolved_decision(self.pipeline, decision_id)
        self.assertIsNotNone(projected)
        self.assertEqual(projected.workspace_revision, 3)  # add(1)+resolve commit(2)->3
        q = self.service.list_subquestions("ws-1")[0]
        self.assertEqual(q.status.value, "resolved")

    def test_stale_approved_decision_is_rejected(self) -> None:
        self.tools.update_subquestions("add", "Q-001", text="q")
        result = self.tools.update_subquestions("resolve", "Q-001", answer="a")
        decision_id = result["decision_point_id"]
        # Make the workspace advance past the proposed base revision.
        self.store.save(self.service.get("ws-1").with_revision(99))
        self.pipeline.hitl_service.resolve(
            decision_id, decision="approved", approved=True, resolved_run_id="run-2"
        )
        with self.assertRaises(Exception):
            commit_resolved_decision(self.pipeline, decision_id)


if __name__ == "__main__":
    import unittest

    unittest.main()
