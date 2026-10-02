"""Assemble the workspace write pipeline components (M3.4 wiring seam).

``WorkspaceGate`` / ``WorkspaceRiskClassifier`` / ``CommitService`` /
``HITLService`` are independent, single-responsibility components. This module
wires them together (share one clock, share the workspace repository, root the
decision-point store at the workspace directory) so the runtime and tests use
one assembly path instead of duplicating dependency wiring.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from research_pulse.workbench.hitl import (
    DecisionPoint,
    DecisionKind,
    DecisionPointJsonStore,
    DecisionPointRepository,
    DecisionStatus,
    HITLService,
)
from research_pulse.workbench.workspace import (
    ProjectedWorkspaceState,
    WorkspaceRepository,
    WorkspaceService,
    WorkspaceStatus,
    Researchability,
    SubQuestionStatus,
)
from research_pulse.workbench.workspace_commit import CommitService
from research_pulse.workbench.workspace_gate import (
    WorkspaceGate,
    WorkspacePatch,
    WorkspacePatchOperation,
)
from research_pulse.workbench.workspace_risk import RiskLevel, WorkspaceRiskClassifier

_DEFAULT_CLOCK: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class WorkspacePipeline:
    """The assembled write pipeline handed to the controlled tools."""

    gate: WorkspaceGate
    risk_classifier: WorkspaceRiskClassifier
    commit_service: CommitService
    hitl_service: HITLService


@dataclass(frozen=True)
class WorkspaceRouteResult:
    projected: ProjectedWorkspaceState | None = None
    decision: DecisionPoint | None = None


class WorkspaceOrchestrator:
    """Sole owner of Gate/Risk/HITL/Commit routing and workspace status progress."""

    def __init__(
        self,
        service: WorkspaceService,
        *,
        gate: WorkspaceGate,
        risk_classifier: WorkspaceRiskClassifier,
        commit_service: CommitService,
        hitl_service: HITLService,
    ) -> None:
        self._service = service
        self._gate = gate
        self._risk = risk_classifier
        self._commit = commit_service
        self._hitl = hitl_service

    def route_patch(
        self,
        workspace_id: str,
        run_id: str,
        operations: tuple[WorkspacePatchOperation, ...],
        *,
        prompt: str,
        provenance: str,
    ) -> WorkspaceRouteResult:
        workspace = self._service.get(workspace_id)
        patch = WorkspacePatch(
            base_workspace_revision=workspace.workspace_revision,
            operations=operations,
            provenance=provenance,
        )
        self._gate.validate(workspace_id, patch)
        if self._risk.classify(patch) is not RiskLevel.HITL:
            return WorkspaceRouteResult(projected=self._commit.commit(workspace_id, patch))

        # Validate the transition before persisting a decision. An illegal
        # state must fail loudly and must not leave an orphan DecisionPoint.
        transition_path = self._waiting_transition_path(workspace)
        decision = self._hitl.create_decision_point(
            workspace_id=workspace_id,
            run_id=run_id,
            kind=DecisionKind.PATCH_APPROVAL,
            prompt=prompt,
            proposed_patch=patch,
        )
        for status in transition_path:
            self._service.advance_status(workspace_id, status)
        return WorkspaceRouteResult(decision=decision)

    def ensure_initial_research(self, workspace_id: str) -> None:
        workspace = self._service.get(workspace_id)
        if workspace.status is WorkspaceStatus.CREATED:
            self._service.advance_status(workspace_id, WorkspaceStatus.INITIAL_RESEARCH)

    def begin_investigation(self, workspace_id: str) -> None:
        """Enter the investigation phase after the user chose a focus.

        Direction selection is deliberately a pause between runs.  Resolving
        the decision records the user's choice, but the next conversational
        turn is what starts the investigation.  Keeping this transition in
        the orchestrator prevents a UI-only status change and avoids bypassing
        an unrelated pending HITL decision.
        """
        workspace = self._service.get(workspace_id)
        if workspace.status is not WorkspaceStatus.WAITING_FOR_USER_ACTION:
            return
        if not workspace.active_focus_id:
            return
        if any(
            decision.status is DecisionStatus.PENDING
            for decision in self._hitl.list_for_workspace(workspace_id)
        ):
            return
        self._service.advance_status(workspace_id, WorkspaceStatus.INVESTIGATING)

    def report_direction_decision(
        self,
        workspace_id: str,
        run_id: str,
        *,
        allow_later: bool = True,
    ) -> DecisionPoint | None:
        """Create a direction checkpoint after the initial or a later round.

        The initial round asks the user to choose the first focus.  An explicit
        caller may also request a later checkpoint with ``allow_later=True``;
        normal runtime completion passes ``False`` so a new candidate never
        interrupts a completed answer.  Moving to ``WAITING_FOR_USER_ACTION``
        makes duplicate checkpoints impossible until the user resolves one.
        """
        workspace = self._service.get(workspace_id)
        # The first round needs an explicit focus choice.  Later rounds are
        # intentionally not interrupted by an automatically generated
        # subquestion: candidates remain visible in the overview and the user
        # can opt in by selecting one.  ``allow_later=True`` keeps the domain
        # seam backwards compatible for explicit callers/tests that want to
        # request a checkpoint.
        if workspace.status is WorkspaceStatus.INVESTIGATING and not allow_later:
            return None
        if workspace.status not in {
            WorkspaceStatus.INITIAL_RESEARCH,
            WorkspaceStatus.INVESTIGATING,
        }:
            return None
        if workspace.status is WorkspaceStatus.INVESTIGATING and not workspace.active_focus_id:
            return None
        if not self._service.list_research_map(workspace_id):
            return None
        candidates = tuple(
            question
            for question in self._service.list_subquestions(workspace_id)
            if question.researchability is Researchability.CANDIDATE
            and question.status is SubQuestionStatus.OPEN
            and (
                workspace.status is WorkspaceStatus.INITIAL_RESEARCH
                or question.question_id != workspace.active_focus_id
            )
        )
        if not candidates:
            return None
        transition_path = self._waiting_transition_path(workspace)
        if workspace.status is WorkspaceStatus.INITIAL_RESEARCH:
            prompt_prefix = "已完成论文初读。系统识别到可继续核查的研究问题："
            prompt_suffix = "请选择是否继续探索这些可验证问题。"
        else:
            prompt_prefix = "本轮研究已完成。基于当前焦点，系统发现了新的可验证方向："
            prompt_suffix = "请选择是否进入下一轮研究；当前焦点会保留在研究历史中。"
        decision = self._hitl.create_decision_point(
            workspace_id=workspace_id,
            run_id=run_id,
            kind=DecisionKind.RESEARCH_DIRECTION,
            prompt=(
                prompt_prefix
                + "（请始终以简体中文为主，必要时在括号中保留英文原文）："
                + "；".join(question.text for question in candidates[:5])
                + "。作者明确声明的局限或尚未判断的问题仅作为阅读边界保留，不会自动展开。"
                + prompt_suffix
            ),
            candidate_question_ids=tuple(question.question_id for question in candidates[:5]),
        )
        for status in transition_path:
            self._service.advance_status(workspace_id, status)
        return decision

    @staticmethod
    def _waiting_transition_path(workspace) -> tuple[WorkspaceStatus, ...]:
        if workspace.status is WorkspaceStatus.CREATED:
            preview = workspace.advance(WorkspaceStatus.INITIAL_RESEARCH)
            preview.advance(WorkspaceStatus.WAITING_FOR_USER_ACTION)
            return (
                WorkspaceStatus.INITIAL_RESEARCH,
                WorkspaceStatus.WAITING_FOR_USER_ACTION,
            )
        workspace.advance(WorkspaceStatus.WAITING_FOR_USER_ACTION)
        return (WorkspaceStatus.WAITING_FOR_USER_ACTION,)


def build_workspace_pipeline(
    repository: WorkspaceRepository,
    *,
    workspace_root: str | Path | None = None,
    decision_repository: DecisionPointRepository | None = None,
    clock: Callable[[], datetime] = _DEFAULT_CLOCK,
) -> WorkspacePipeline:
    """Assemble gate + risk + commit + hitl sharing one repository and clock.

    ``decision_repository`` overrides the default file-backed store; otherwise a
    ``DecisionPointJsonStore`` rooted at ``workspace_root`` is used (required for
    persistence). ``workspace_root`` may be omitted only when an explicit
    ``decision_repository`` is supplied.
    """
    gate = WorkspaceGate(repository, clock=clock)
    risk_classifier = WorkspaceRiskClassifier()
    commit_service = CommitService(repository, gate=gate)
    if decision_repository is None:
        if workspace_root is None:
            raise ValueError("workspace_root or decision_repository is required")
        decision_repository = DecisionPointJsonStore(workspace_root)
    hitl_service = HITLService(decision_repository, clock=clock)
    return WorkspacePipeline(
        gate=gate,
        risk_classifier=risk_classifier,
        commit_service=commit_service,
        hitl_service=hitl_service,
    )


def commit_resolved_decision(
    pipeline: WorkspacePipeline,
    decision_id: str,
) -> ProjectedWorkspaceState | None:
    """M3.6 — commit an already-approved patch_approval decision.

    The stored ``proposed_patch`` is re-validated by ``CommitService`` against the
    *current* workspace revision (the optimistic lock), so a decision that went
    stale while the user was away is rejected (rebase/reject) rather than
    silently overwriting newer state. Returns the projected state on a successful
    commit; ``None`` if the decision is not an approved patch_approval.
    """
    decision = pipeline.hitl_service.get(decision_id)
    if decision is None or decision.kind != DecisionKind.PATCH_APPROVAL:
        return None
    if decision.status != DecisionStatus.APPROVED or decision.proposed_patch is None:
        return None
    return pipeline.commit_service.commit(decision.workspace_id, decision.proposed_patch)
