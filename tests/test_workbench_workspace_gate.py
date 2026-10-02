"""M3.1 — WorkspacePatch + WorkspaceGate integrity gate (with failure tests).

The gate answers only the integrity question: schema valid, stable IDs never
changed, evidence references complete, source_id + block_ids present, version
optimistic lock, provenance. Research judgement (should this be accepted?) lives
in the RiskClassifier, never here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import TestCase

from tests._workspace_store import MemoryWorkspaceStore

from research_pulse.workbench.workspace import (
    CandidateHypothesis,
    ResearchPlan,
    ResearchPlanStage,
    SubQuestion,
    Workspace,
    WorkspaceNotFoundError,
)
from research_pulse.workbench.workspace_gate import (
    EVIDENCE_ADD,
    RESEARCH_MAP_ADD,
    RESEARCH_MAP_UPDATE,
    RESEARCH_PLAN_REPLACE,
    SUBQUESTION_ADD,
    SUBQUESTION_DEPRIORITIZE,
    SUBQUESTION_RESOLVE,
    SUBQUESTION_UPDATE,
    WorkspaceGate,
    WorkspaceGateError,
    WorkspacePatch,
    WorkspacePatchError,
    WorkspacePatchOperation,
)

_FIXED_NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def _seed(store: MemoryWorkspaceStore, workspace_id: str = "w1") -> Workspace:
    workspace = Workspace(
        workspace_id=workspace_id,
        research_question="How should shadow variables address MNAR?",
        anchor_paper_id="paper-1",
    )
    store.create(workspace)
    store.upsert_subquestion(
        workspace_id,
        SubQuestion(question_id="Q-001", text="existing subquestion"),
    )
    return workspace


class WorkspacePatchTypeTests(TestCase):
    def test_valid_patch_construction(self) -> None:
        op = WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-005", text="new question")
        patch = WorkspacePatch(base_workspace_revision=3, operations=(op,), provenance="agent:run-7")
        self.assertEqual(patch.base_workspace_revision, 3)
        self.assertEqual(patch.operations, (op,))

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(WorkspacePatchError):
            WorkspacePatchOperation(kind="delete_all", object_id="x")

    def test_blank_object_id_is_rejected(self) -> None:
        with self.assertRaises(WorkspacePatchError):
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="   ", text="x")

    def test_patch_requires_positive_base_revision(self) -> None:
        op = WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-2", text="x")
        with self.assertRaises(WorkspacePatchError):
            WorkspacePatch(base_workspace_revision=0, operations=(op,), provenance="agent")

    def test_patch_requires_at_least_one_operation(self) -> None:
        with self.assertRaises(WorkspacePatchError):
            WorkspacePatch(base_workspace_revision=1, operations=(), provenance="agent")

    def test_patch_requires_provenance(self) -> None:
        op = WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-2", text="x")
        with self.assertRaises(WorkspacePatchError):
            WorkspacePatch(base_workspace_revision=1, operations=(op,), provenance="  ")


class WorkspaceGateTests(TestCase):
    def setUp(self) -> None:
        self.store = MemoryWorkspaceStore()
        self.gate = WorkspaceGate(self.store, clock=lambda: _FIXED_NOW)
        self.workspace = _seed(self.store)

    def _patch(self, *operations: WorkspacePatchOperation, base: int = 1) -> WorkspacePatch:
        return WorkspacePatch(base_workspace_revision=base, operations=tuple(operations), provenance="agent:run-1")

    def test_valid_patch_projects_and_bumps_revision(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="sub"),
            WorkspacePatchOperation(
                kind=EVIDENCE_ADD,
                object_id="E-001",
                source_id="paper-2",
                block_ids=("b1", "b2"),
                supports_question_ids=("Q-002",),
            ),
            WorkspacePatchOperation(
                kind=RESEARCH_MAP_ADD,
                object_id="M-001",
                related_question_ids=("Q-002",),
                evidence_ids=("E-001",),
            ),
        )
        projected = self.gate.validate(self.workspace.workspace_id, patch)
        self.assertEqual(projected.workspace_revision, 2)
        ids = {q.question_id for q in projected.subquestions}
        self.assertIn("Q-001", ids)
        self.assertIn("Q-002", ids)
        self.assertEqual(projected.evidence[0].added_at, _FIXED_NOW)

    def test_meta_is_preserved_and_revision_bumped(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-003", text="x"),
        )
        projected = self.gate.validate(self.workspace.workspace_id, patch)
        stored = self.store.get(self.workspace.workspace_id)
        self.assertEqual(projected.workspace_revision, stored.workspace_revision + 1)
        self.assertEqual(projected.workspace_revision, 2)

    def test_optimistic_lock_conflict_is_rejected(self) -> None:
        # Current workspace is at revision 1; a patch riding a different revision (2) fails.
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-003", text="x"),
            base=2,
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_optimistic_lock_conflict_after_advance(self) -> None:
        # Simulate a commit bumping revision to 2, then a stale patch rides base 1.
        self.store.save(self.workspace.with_revision(2))
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-003", text="x"),
            base=1,
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_add_existing_stable_id_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-001", text="clobber"),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_update_nonexistent_subquestion_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_UPDATE, object_id="Q-999", text="x"),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_update_existing_subquestion_rewrites_text_not_id(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_UPDATE, object_id="Q-001", text="rewritten"),
        )
        projected = self.gate.validate(self.workspace.workspace_id, patch)
        updated = next(q for q in projected.subquestions if q.question_id == "Q-001")
        self.assertEqual(updated.question_id, "Q-001")
        self.assertEqual(updated.text, "rewritten")

    def test_resolve_nonexistent_subquestion_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_RESOLVE, object_id="Q-999", answer="a"),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_resolve_requires_answer(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_RESOLVE, object_id="Q-001", answer="  "),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_deprioritize_nonexistent_subquestion_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_DEPRIORITIZE, object_id="Q-999"),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_evidence_missing_source_id_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", block_ids=("b1",)),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_evidence_missing_block_ids_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", source_id="paper-2"),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_evidence_duplicate_block_ids_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(
                kind=EVIDENCE_ADD,
                object_id="E-001",
                source_id="paper-2",
                block_ids=("b1", "b1"),
            ),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_evidence_referencing_unknown_subquestion_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(
                kind=EVIDENCE_ADD,
                object_id="E-001",
                source_id="paper-2",
                block_ids=("b1",),
                supports_question_ids=("Q-777",),
            ),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_research_map_referencing_unknown_subquestion_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(
                kind=RESEARCH_MAP_ADD,
                object_id="M-001",
                related_question_ids=("Q-777",),
            ),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_research_map_referencing_unknown_evidence_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(
                kind=RESEARCH_MAP_ADD,
                object_id="M-001",
                evidence_ids=("E-777",),
            ),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_research_map_update_nonexistent_node_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=RESEARCH_MAP_UPDATE, object_id="M-999", label="x"),
        )
        with self.assertRaises(WorkspaceGateError):
            self.gate.validate(self.workspace.workspace_id, patch)

    def test_workspace_not_found_is_rejected(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-003", text="x"),
        )
        with self.assertRaises(WorkspaceNotFoundError):
            self.gate.validate("does-not-exist", patch)

    def test_research_plan_replace_projects_typed_plan(self) -> None:
        plan = ResearchPlan(
            stage=ResearchPlanStage.HYPOTHESIS_REVIEW,
            hypotheses=(CandidateHypothesis("H-001", "干预是否减少位置偏差？", question_id="Q-001"),),
        )
        projected = self.gate.validate(
            self.workspace.workspace_id,
            self._patch(
                WorkspacePatchOperation(
                    kind=RESEARCH_PLAN_REPLACE,
                    object_id="RESEARCH-PLAN",
                    research_plan=plan,
                )
            ),
        )
        self.assertIsNotNone(projected.research_plan)
        self.assertEqual(projected.research_plan.stage, ResearchPlanStage.HYPOTHESIS_REVIEW)
        self.assertEqual(projected.research_plan.hypotheses[0].hypothesis_id, "H-001")

    def test_research_plan_rejects_unknown_references_and_duplicate_ids(self) -> None:
        unknown = ResearchPlan(
            hypotheses=(CandidateHypothesis("H-001", "h", question_id="Q-999"),),
        )
        with self.assertRaisesRegex(WorkspaceGateError, "unknown subquestion"):
            self.gate.validate(
                self.workspace.workspace_id,
                self._patch(WorkspacePatchOperation(
                    kind=RESEARCH_PLAN_REPLACE,
                    object_id="RESEARCH-PLAN",
                    research_plan=unknown,
                )),
            )
        duplicate = ResearchPlan(
            hypotheses=(
                CandidateHypothesis("H-001", "h1"),
                CandidateHypothesis("H-001", "h2"),
            ),
        )
        with self.assertRaisesRegex(WorkspaceGateError, "hypothesis ids must be unique"):
            self.gate.validate(
                self.workspace.workspace_id,
                self._patch(WorkspacePatchOperation(
                    kind=RESEARCH_PLAN_REPLACE,
                    object_id="RESEARCH-PLAN",
                    research_plan=duplicate,
                )),
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
