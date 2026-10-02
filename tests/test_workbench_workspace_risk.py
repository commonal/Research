"""M3.2 — WorkspaceRiskClassifier static grading (deterministic, not agent self-report).

The classifier maps operation type + object attributes to a risk level and,
for a whole patch, takes the most severe level. The decision is programmatic so
tests pin the exact classification table — it is never an agent's self-reported
confidence, and never mixed with the tool-permission preflight.
"""

from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.workspace import CandidateHypothesis, ResearchPlan, ResearchPlanStage

from research_pulse.workbench.workspace_gate import (
    EVIDENCE_ADD,
    RESEARCH_MAP_ADD,
    RESEARCH_MAP_UPDATE,
    RESEARCH_PLAN_REPLACE,
    SUBQUESTION_ADD,
    SUBQUESTION_DEPRIORITIZE,
    SUBQUESTION_RESOLVE,
    SUBQUESTION_UPDATE,
    WorkspacePatch,
    WorkspacePatchOperation,
)
from research_pulse.workbench.workspace_risk import RiskLevel, WorkspaceRiskClassifier


class WorkspaceRiskClassifierTests(TestCase):
    def setUp(self) -> None:
        self.classifier = WorkspaceRiskClassifier()

    def _patch(self, *operations: WorkspacePatchOperation) -> WorkspacePatch:
        return WorkspacePatch(base_workspace_revision=1, operations=tuple(operations), provenance="agent:run-1")

    def test_add_evidence_reference_is_auto(self) -> None:
        op = WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", source_id="p", block_ids=("b1",))
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.AUTO)
        self.assertEqual(self.classifier.classify(self._patch(op)), RiskLevel.AUTO)

    def test_evidence_becoming_conflicting_is_hitl(self) -> None:
        op = WorkspacePatchOperation(
            kind=EVIDENCE_ADD, object_id="E-001", source_id="p", block_ids=("b1",), evidence_role="conflicting"
        )
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.HITL)

    def test_subquestion_add_is_review(self) -> None:
        op = WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="x")
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.REVIEW)

    def test_subquestion_text_update_is_review(self) -> None:
        op = WorkspacePatchOperation(kind=SUBQUESTION_UPDATE, object_id="Q-001", text="x")
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.REVIEW)

    def test_subquestion_resolve_is_hitl(self) -> None:
        op = WorkspacePatchOperation(kind=SUBQUESTION_RESOLVE, object_id="Q-001", answer="a")
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.HITL)

    def test_subquestion_deprioritize_is_review(self) -> None:
        op = WorkspacePatchOperation(kind=SUBQUESTION_DEPRIORITIZE, object_id="Q-001")
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.REVIEW)

    def test_research_map_add_is_review(self) -> None:
        op = WorkspacePatchOperation(kind=RESEARCH_MAP_ADD, object_id="M-001")
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.REVIEW)

    def test_research_map_update_is_review(self) -> None:
        op = WorkspacePatchOperation(kind=RESEARCH_MAP_UPDATE, object_id="M-001", label="x")
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.REVIEW)

    def test_draft_research_plan_is_review(self) -> None:
        op = WorkspacePatchOperation(
            kind=RESEARCH_PLAN_REPLACE,
            object_id="RESEARCH-PLAN",
            research_plan=ResearchPlan(),
        )
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.REVIEW)

    def test_ready_research_plan_is_hitl(self) -> None:
        op = WorkspacePatchOperation(
            kind=RESEARCH_PLAN_REPLACE,
            object_id="RESEARCH-PLAN",
            research_plan=ResearchPlan(
                stage=ResearchPlanStage.READY,
                hypotheses=(CandidateHypothesis("H-001", "h"),),
            ),
        )
        self.assertEqual(self.classifier.classify_operation(op), RiskLevel.HITL)

    def test_mixed_patch_takes_most_severe(self) -> None:
        # auto addition + a semantic reversal must land on HITL, not auto.
        patch = self._patch(
            WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", source_id="p", block_ids=("b1",)),
            WorkspacePatchOperation(kind=SUBQUESTION_RESOLVE, object_id="Q-001", answer="a"),
        )
        self.assertEqual(self.classifier.classify(patch), RiskLevel.HITL)

    def test_mixed_patch_of_review_and_auto_is_review(self) -> None:
        patch = self._patch(
            WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", source_id="p", block_ids=("b1",)),
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="x"),
        )
        self.assertEqual(self.classifier.classify(patch), RiskLevel.REVIEW)

    def test_risk_level_orders_by_severity(self) -> None:
        self.assertLess(RiskLevel.AUTO, RiskLevel.REVIEW)
        self.assertLess(RiskLevel.REVIEW, RiskLevel.HITL)


if __name__ == "__main__":
    import unittest

    unittest.main()
