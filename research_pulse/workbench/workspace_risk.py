"""Static risk classification for research-workspace patches (M3).

The RiskClassifier answers one orthogonal question to the Gate: for a given
operation, should the change be accepted automatically, shown for review, or
require a user decision before it commits? It is a *programmatic*, deterministic
mapping from operation type + object attributes to a ``RiskLevel`` — never an
agent self-report, and never merged into the tool-permission preflight (that
guards *which tools exist*, not *whether a canonical change is acceptable*).

Classification follows the design table:

    auto   — additive, no semantic reversal        (add evidence reference)
    review — structural addition / wording rewrite (add subquestion, map branch,
             subquestion text update, deprioritize a question)
    hitl   — semantic state reversal / direction   (resolve a subquestion,
             evidence becomes conflicting)
"""

from __future__ import annotations

from enum import IntEnum

from research_pulse.workbench.workspace_gate import (
    EVIDENCE_ADD,
    RESEARCH_MAP_ADD,
    RESEARCH_MAP_UPDATE,
    RESEARCH_PLAN_REPLACE,
    RESEARCH_ITERATION_APPEND,
    RESEARCH_ITERATION_UPDATE,
    SUBQUESTION_ADD,
    SUBQUESTION_DEPRIORITIZE,
    SUBQUESTION_RESOLVE,
    SUBQUESTION_UPDATE,
    WorkspacePatch,
    WorkspacePatchOperation,
)
from research_pulse.workbench.workspace import (
    HypothesisStatus,
    PlanArtifactStatus,
    ResearchPlanStage,
)


class RiskLevel(IntEnum):
    """Ordered severity: AUTO < REVIEW < HITL."""

    AUTO = 0
    REVIEW = 1
    HITL = 2


# Only *resolving* a subquestion is a hard direction reversal needing a human
# ruling. Deprioritizing is a soft "park this for now" and commits (review).
_HITL_KINDS = frozenset({SUBQUESTION_RESOLVE})
_REVIEW_KINDS = frozenset({SUBQUESTION_ADD, SUBQUESTION_UPDATE, SUBQUESTION_DEPRIORITIZE, RESEARCH_MAP_ADD, RESEARCH_MAP_UPDATE})


class WorkspaceRiskClassifier:
    """Deterministic risk routing for a proposed patch.

    ``classify`` returns the most severe level across all operations in the patch,
    so a patch mixing an auto addition with a semantic reversal correctly lands on
    ``HITL``.
    """

    def classify(self, patch: WorkspacePatch) -> RiskLevel:
        return max((self._classify_operation(op) for op in patch.operations), default=RiskLevel.AUTO)

    def classify_operation(self, operation: WorkspacePatchOperation) -> RiskLevel:
        return self._classify_operation(operation)

    @staticmethod
    def _classify_operation(op: WorkspacePatchOperation) -> RiskLevel:
        if op.kind in {RESEARCH_ITERATION_APPEND, RESEARCH_ITERATION_UPDATE}:
            # A runtime receipt records what happened; it does not select a
            # hypothesis, approve a plan, or reverse a prior decision.
            return RiskLevel.AUTO
        if op.kind in _HITL_KINDS:
            return RiskLevel.HITL
        if op.kind == RESEARCH_PLAN_REPLACE:
            plan = op.research_plan
            # Drafting is a reviewable artifact update. Selecting a candidate,
            # approving an artifact, or declaring the plan ready changes the
            # research direction and therefore pauses for an explicit user
            # decision before it can become canonical.
            if plan is not None and (
                plan.stage is ResearchPlanStage.READY
                or any(item.status is HypothesisStatus.SELECTED for item in plan.hypotheses)
                or (
                    plan.method_map is not None
                    and plan.method_map.status is PlanArtifactStatus.APPROVED
                )
                or any(item.status is PlanArtifactStatus.APPROVED for item in plan.critiques)
                or any(item.status is PlanArtifactStatus.APPROVED for item in plan.experiment_plans)
            ):
                return RiskLevel.HITL
            return RiskLevel.REVIEW
        if op.kind in _REVIEW_KINDS:
            return RiskLevel.REVIEW
        if op.kind == EVIDENCE_ADD:
            # An additive evidence reference is auto; flipping it to conflicting
            # reverses the research position and needs a human judgement.
            if (op.evidence_role or "supporting") == "conflicting":
                return RiskLevel.HITL
            return RiskLevel.AUTO
        # Only remaining kind is research_map_update; covered above. Unreachable here.
        return RiskLevel.REVIEW
