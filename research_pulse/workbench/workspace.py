"""Framework-free domain contracts + service seam for the Research Assistant Workspace.

A Workspace is the top-level persistent research object: one Research Question with
one Anchor Paper (both REQUIRED in V1), carrying canonical JSON state (research map,
subquestions, evidence, and a research plan). A Session is a single opened dialog context and may hold many
Agent Runs; both share one Workspace state so the assistant is "present" across rounds.

Canonical JSON is the single source of truth. Markdown and the progress snapshot are
derived artifacts. Durable objects use workspace-scoped stable IDs and reference each
other only by ID. The Workspace ``status`` is controlled by the Orchestrator through
``advance_status`` only — there is no free "set status" mutation for the agent.

The user-facing research model distinguishes a stable ``research_intent`` from an
optional, user-confirmed ``active_focus_id``. ``research_question`` remains as a
backwards-compatible alias for older workspaces and API clients; it must not be
treated as an automatically promoted root question.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable, Protocol
from uuid import uuid4


class WorkspaceStatus(StrEnum):
    CREATED = "created"
    INITIAL_RESEARCH = "initial_research"
    WAITING_FOR_USER_ACTION = "waiting_for_user_action"
    INVESTIGATING = "investigating"
    ARCHIVED = "archived"


# Orchestrator-controlled transition table. ARCHIVED is a terminal short-circuit
# reachable from any non-terminal state; everything else follows the strict loop
# CREATED -> INITIAL_RESEARCH -> WAITING_FOR_USER_ACTION -> INVESTIGATING -> WAITING...
_WORKSPACE_TRANSITIONS: dict[WorkspaceStatus, frozenset[WorkspaceStatus]] = {
    WorkspaceStatus.CREATED: frozenset(
        {WorkspaceStatus.INITIAL_RESEARCH, WorkspaceStatus.ARCHIVED}
    ),
    WorkspaceStatus.INITIAL_RESEARCH: frozenset(
        {WorkspaceStatus.WAITING_FOR_USER_ACTION, WorkspaceStatus.ARCHIVED}
    ),
    WorkspaceStatus.WAITING_FOR_USER_ACTION: frozenset(
        {WorkspaceStatus.INVESTIGATING, WorkspaceStatus.ARCHIVED}
    ),
    WorkspaceStatus.INVESTIGATING: frozenset(
        {WorkspaceStatus.WAITING_FOR_USER_ACTION, WorkspaceStatus.ARCHIVED}
    ),
    WorkspaceStatus.ARCHIVED: frozenset(),
}

# CREATED stays terminal on archive; restoring returns to CREATED, not to the
# pre-archive state (V1 keeps restore semantics simple for the workspace).
_RESTORE_TO = {
    WorkspaceStatus.ARCHIVED: WorkspaceStatus.CREATED,
}


class SubQuestionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DEPRIORITIZED = "deprioritized"


class Researchability(StrEnum):
    """Whether a subquestion is suitable for a follow-up investigation.

    ``BOUNDARY`` is used for an author-stated limitation or an explicitly
    out-of-scope item that should remain useful reading context without
    automatically opening another research run. ``UNKNOWN`` is fail-closed:
    it is visible to the user but cannot trigger the continuation prompt.
    ``CANDIDATE`` is reserved for a question that can be checked with further
    literature, data, or an experiment.
    """

    CANDIDATE = "candidate"
    BOUNDARY = "boundary"
    UNKNOWN = "unknown"


class WorkspaceNotFoundError(LookupError):
    """Raised when a workspace does not exist at the public service seam."""


class EvidenceError(ValueError):
    """Raised when an evidence record is missing required provenance."""


class MissingRequiredFieldError(ValueError):
    """Raised when a workspace is created without research_intent or anchor_paper_id."""


def _normalize_required(text: str | None, *, field_name: str) -> str:
    if text is None:
        raise MissingRequiredFieldError(f"{field_name} is required")
    normalized = " ".join(text.split())
    if not normalized:
        raise MissingRequiredFieldError(f"{field_name} must not be blank")
    return normalized


def _ensure_status_transition(current: WorkspaceStatus, target: WorkspaceStatus) -> None:
    if target not in _WORKSPACE_TRANSITIONS[current]:
        raise ValueError(
            f"illegal workspace transition: {current.value} -> {target.value}"
        )


@dataclass(frozen=True)
class SubQuestion:
    question_id: str
    text: str
    status: SubQuestionStatus = SubQuestionStatus.OPEN
    answer: str | None = None
    researchability: Researchability = Researchability.CANDIDATE

    def with_text(self, text: str) -> "SubQuestion":
        return replace(self, text=" ".join(text.split()))

    def resolve(self, answer: str) -> "SubQuestion":
        return replace(self, status=SubQuestionStatus.RESOLVED, answer=answer)

    def with_researchability(self, value: Researchability) -> "SubQuestion":
        return replace(self, researchability=value)

    def deprioritize(self) -> "SubQuestion":
        return replace(self, status=SubQuestionStatus.DEPRIORITIZED)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_id: str
    block_ids: tuple[str, ...]
    supports_question_ids: tuple[str, ...] = ()
    evidence_role: str = "supporting"
    claim: str = ""
    research_interpretation: str = ""
    confidence: str = "medium"
    added_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.source_id or not self.block_ids:
            raise EvidenceError("evidence requires evidence_id, source_id and block_ids")


@dataclass(frozen=True)
class ResearchMapNode:
    node_id: str
    related_question_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    label: str = ""

    def with_label(self, label: str) -> "ResearchMapNode":
        return replace(self, label=" ".join(label.split()))

    def with_question_ids(self, related_question_ids: tuple[str, ...]) -> "ResearchMapNode":
        return replace(self, related_question_ids=tuple(dict.fromkeys(related_question_ids)))

    def with_evidence_ids(self, evidence_ids: tuple[str, ...]) -> "ResearchMapNode":
        return replace(self, evidence_ids=tuple(dict.fromkeys(evidence_ids)))


class ResearchPlanStage(StrEnum):
    """Fine-grained progress inside a workspace research plan.

    Workspace.status deliberately stays small and lifecycle-oriented. This
    stage belongs to the persisted research artifact and can therefore evolve
    without adding more pause states to the outer workspace state machine.
    """

    NOT_STARTED = "not_started"
    METHOD_MAPPING = "method_mapping"
    HYPOTHESIS_REVIEW = "hypothesis_review"
    EXPERIMENT_PLANNING = "experiment_planning"
    READY = "ready"


class ResearchArtifactStage(StrEnum):
    """Maturity of the user-facing research artifact.

    ``ResearchPlanStage`` describes the internal planning workflow (method map,
    hypothesis review and experiment planning).  This enum is deliberately
    separate: a workspace may have a rich plan while still only being ready to
    show an overview, not a publication-style report.  Promotion beyond
    ``STAGE_NOTE`` must be an explicit product action in a later slice.
    """

    OVERVIEW = "overview"
    STAGE_NOTE = "stage_note"
    REPORT_DRAFT = "report_draft"
    REPORT = "report"


class ResearchIterationStatus(StrEnum):
    """Lifecycle of one bounded research round.

    An iteration is deliberately separate from an AgentRun: one round may use
    several runs (for example, a direction decision followed by a continuation)
    while the iteration records the research meaning and decision made in that
    round.
    """

    IN_PROGRESS = "in_progress"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PlanArtifactStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class HypothesisStatus(StrEnum):
    CANDIDATE = "candidate"
    SELECTED = "selected"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MethodMapEntry:
    """One comparable method or mechanism in the research landscape."""

    method_id: str
    name: str
    mechanism: str = ""
    assumptions: str = ""
    evidence_ids: tuple[str, ...] = ()
    limitations: str = ""


@dataclass(frozen=True)
class MethodMap:
    map_id: str = "METHOD-MAP"
    status: PlanArtifactStatus = PlanArtifactStatus.DRAFT
    entries: tuple[MethodMapEntry, ...] = ()


@dataclass(frozen=True)
class CandidateHypothesis:
    """A falsifiable candidate, not an automatically promoted question."""

    hypothesis_id: str
    text: str
    question_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    rationale: str = ""
    falsifiers: tuple[str, ...] = ()
    status: HypothesisStatus = HypothesisStatus.CANDIDATE


@dataclass(frozen=True)
class Critique:
    """A structured challenge to one candidate hypothesis."""

    critique_id: str
    hypothesis_id: str
    strengths: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    confidence: str = "medium"
    status: PlanArtifactStatus = PlanArtifactStatus.DRAFT


@dataclass(frozen=True)
class ExperimentPlan:
    """A proposed intervention and its falsification/evaluation protocol."""

    plan_id: str
    hypothesis_id: str | None = None
    status: PlanArtifactStatus = PlanArtifactStatus.DRAFT
    intervention: str = ""
    baselines: tuple[str, ...] = ()
    datasets: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    ablations: tuple[str, ...] = ()
    expected_outcomes: str = ""
    decision_criteria: str = ""
    resource_estimate: str = ""
    risks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchIteration:
    """A durable, bounded round in the evolution of a research question."""

    iteration_id: str
    sequence: int
    title: str
    status: ResearchIterationStatus = ResearchIterationStatus.IN_PROGRESS
    focus_question_id: str | None = None
    run_id: str | None = None
    summary: str = ""
    evidence_ids: tuple[str, ...] = ()
    candidate_question_ids: tuple[str, ...] = ()
    decision: str = ""
    next_step: str = ""


@dataclass(frozen=True)
class ResearchPlan:
    """Canonical plan artifacts associated with one Workspace."""

    stage: ResearchPlanStage = ResearchPlanStage.NOT_STARTED
    # Do not call the first synthesis a "report".  The artifact starts as an
    # overview and is promoted to a stage note once a durable research round
    # exists.  Report draft/final promotion is explicit and is not performed by
    # the exploration runtime.
    artifact_stage: ResearchArtifactStage = ResearchArtifactStage.OVERVIEW
    method_map: MethodMap | None = None
    hypotheses: tuple[CandidateHypothesis, ...] = ()
    critiques: tuple[Critique, ...] = ()
    experiment_plans: tuple[ExperimentPlan, ...] = ()
    iterations: tuple[ResearchIteration, ...] = ()


@dataclass(frozen=True)
class WorkspaceSession:
    """A single opened dialog context for a workspace. May hold many Agent Runs.

    This is DIFFERENT from the workspace aggregate: a Session is runtime history,
    while the Workspace is the durable research state. Multiple Sessions share and
    advance one Workspace state so the assistant stays present across rounds.
    """

    session_id: str
    workspace_id: str
    created_at: datetime


@dataclass(frozen=True)
class AgentRun:
    """One execution triggered by a single user message inside a Session."""

    run_id: str
    session_id: str
    workspace_id: str
    message: str
    status: str = "queued"
    created_at: datetime | None = None


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    research_question: str
    anchor_paper_id: str
    research_intent: str | None = None
    active_focus_id: str | None = None
    status: WorkspaceStatus = WorkspaceStatus.CREATED
    workspace_revision: int = 1
    created_at: datetime | None = None
    research_map: tuple[ResearchMapNode, ...] = ()
    subquestions: tuple[SubQuestion, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    research_plan: ResearchPlan = field(default_factory=ResearchPlan)

    def __post_init__(self) -> None:
        if self.workspace_revision < 1:
            raise ValueError("workspace_revision must be a positive integer")
        if self.research_intent is None:
            # Legacy workspaces only have research_question. Keep the old value
            # as the stable intent while callers migrate to the new field.
            object.__setattr__(self, "research_intent", self.research_question or None)

    def advance(self, status: WorkspaceStatus) -> "Workspace":
        _ensure_status_transition(self.status, status)
        return replace(self, status=status)

    def with_revision(self, revision: int) -> "Workspace":
        """Return a copy carrying the new global optimistic-lock revision."""
        if revision < 1:
            raise ValueError("workspace_revision must be a positive integer")
        return replace(self, workspace_revision=revision)

    def restore(self) -> "Workspace":
        target = _RESTORE_TO.get(self.status, WorkspaceStatus.CREATED)
        return replace(self, status=target)

    def with_active_focus(self, question_id: str | None) -> "Workspace":
        """Return a copy with the user-selected current research focus."""
        return replace(self, active_focus_id=question_id)


@dataclass(frozen=True)
class ProjectedWorkspaceState:
    """The full post-patch canonical state, persisted atomically by CommitService.

    A valid WorkspacePatch projects onto one of these: the new global
    ``workspace_revision`` plus the complete post-patch subquestions / evidence /
    research map and, when changed, the typed research plan. Gate produces it;
    CommitService writes it atomically; the
    repository persists it under one lock so a reader never sees half-applied state.
    """

    workspace_revision: int
    subquestions: tuple[SubQuestion, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    research_map: tuple[ResearchMapNode, ...] = ()
    # ``None`` means preserve the current plan. This keeps existing patch
    # operations backwards compatible while allowing a future plan operation
    # to participate in the same atomic commit.
    research_plan: ResearchPlan | None = None

    def __post_init__(self) -> None:
        if self.workspace_revision < 1:
            raise ValueError("workspace_revision must be a positive integer")


class WorkspaceRepository(Protocol):
    def create(self, workspace: Workspace) -> None: ...
    def get(self, workspace_id: str) -> Workspace | None: ...
    def list(self) -> tuple[Workspace, ...]: ...
    def save(self, workspace: Workspace) -> None: ...
    def apply_commit(self, workspace_id: str, projected: ProjectedWorkspaceState) -> None: ...
    def list_subquestions(self, workspace_id: str) -> tuple[SubQuestion, ...]: ...
    def get_subquestion(self, workspace_id: str, question_id: str) -> SubQuestion | None: ...
    def upsert_subquestion(self, workspace_id: str, question: SubQuestion) -> None: ...
    def list_evidence(self, workspace_id: str) -> tuple[Evidence, ...]: ...
    def upsert_evidence(self, workspace_id: str, evidence: Evidence) -> None: ...
    def list_research_map(self, workspace_id: str) -> tuple[ResearchMapNode, ...]: ...
    def get_research_map_node(self, workspace_id: str, node_id: str) -> ResearchMapNode | None: ...
    def upsert_research_map_node(self, workspace_id: str, node: ResearchMapNode) -> None: ...


class WorkspaceService:
    """Public seam for workspace lifecycle + durable research state operations.

    The service is the ONLY path to mutate workspace state. Agent-facing tools must
    use these methods; ``status`` advances only through ``advance_status`` (Orchestrator
    gate), so an agent cannot claim ``WAITING_FOR_USER_ACTION`` on its own.
    """

    def __init__(
        self,
        repository: WorkspaceRepository,
        *,
        workspace_id_factory: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.workspace_id_factory = workspace_id_factory
        self.clock = clock

    def create(
        self,
        *,
        research_question: str | None = None,
        anchor_paper_id: str | None,
        research_intent: str | None = None,
    ) -> Workspace:
        # ``research_question`` remains accepted for old callers. New callers
        # should provide the broader research_intent instead.
        intent = _normalize_required(
            research_intent if research_intent is not None else research_question,
            field_name="research_intent",
        )
        question = " ".join((research_question or "").split())
        anchor = _normalize_required(anchor_paper_id, field_name="anchor_paper_id")
        workspace = Workspace(
            workspace_id=self.workspace_id_factory(),
            research_question=question,
            anchor_paper_id=anchor,
            research_intent=intent,
            status=WorkspaceStatus.CREATED,
            created_at=self.clock(),
        )
        self.repository.create(workspace)
        return workspace

    def get(self, workspace_id: str) -> Workspace:
        workspace = self.repository.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    def list(self) -> tuple[Workspace, ...]:
        return self.repository.list()

    def archive(self, workspace_id: str) -> Workspace:
        workspace = self.advance_status(workspace_id, WorkspaceStatus.ARCHIVED)
        return workspace

    def restore(self, workspace_id: str) -> Workspace:
        workspace = self.get(workspace_id)
        if workspace.status != WorkspaceStatus.ARCHIVED:
            raise ValueError("only archived workspaces can be restored")
        updated = workspace.restore()
        self.repository.save(updated)
        return updated

    def advance_status(self, workspace_id: str, status: WorkspaceStatus) -> Workspace:
        """Orchestrator-only status transition. The agent cannot call this freely."""
        workspace = self.get(workspace_id)
        updated = workspace.advance(status)
        self.repository.save(updated)
        return updated

    def set_active_focus(self, workspace_id: str, question_id: str) -> Workspace:
        """Persist one explicit user-selected candidate as the current focus.

        This is intentionally separate from subquestion updates: selecting a
        focus is a user action, not an automatic Agent promotion.
        """
        workspace = self.get(workspace_id)
        if workspace.status is WorkspaceStatus.ARCHIVED:
            raise ValueError("archived workspaces cannot change the active focus")
        question = self.repository.get_subquestion(workspace_id, question_id)
        if question is None:
            raise WorkspaceNotFoundError(question_id)
        if question.researchability is not Researchability.CANDIDATE:
            raise ValueError("only candidate questions can become the active focus")
        if question.status is SubQuestionStatus.DEPRIORITIZED:
            raise ValueError("deprioritized questions cannot become the active focus")
        updated = workspace.with_active_focus(question_id)
        self.repository.save(updated)
        return updated

    def clear_active_focus(self, workspace_id: str) -> Workspace:
        workspace = self.get(workspace_id)
        if workspace.status is WorkspaceStatus.ARCHIVED:
            raise ValueError("archived workspaces cannot change the active focus")
        updated = workspace.with_active_focus(None)
        self.repository.save(updated)
        return updated

    # -- SubQuestions (structured operations; never overwrite the whole canonical file) --

    def add_subquestion(
        self,
        workspace_id: str,
        question_id: str,
        text: str,
        *,
        researchability: Researchability = Researchability.CANDIDATE,
    ) -> SubQuestion:
        self.get(workspace_id)
        normalized = " ".join(text.split())
        if not normalized:
            raise ValueError("subquestion text must not be blank")
        question = SubQuestion(
            question_id=question_id,
            text=normalized,
            researchability=researchability,
        )
        self.repository.upsert_subquestion(workspace_id, question)
        return question

    def update_subquestion_text(self, workspace_id: str, question_id: str, text: str) -> SubQuestion:
        self.get(workspace_id)
        current = self.repository.get_subquestion(workspace_id, question_id)
        if current is None:
            raise WorkspaceNotFoundError(question_id)
        updated = current.with_text(text)
        self.repository.upsert_subquestion(workspace_id, updated)
        return updated

    def update_subquestion_researchability(
        self,
        workspace_id: str,
        question_id: str,
        researchability: Researchability,
    ) -> SubQuestion:
        self.get(workspace_id)
        current = self.repository.get_subquestion(workspace_id, question_id)
        if current is None:
            raise WorkspaceNotFoundError(question_id)
        updated = current.with_researchability(researchability)
        self.repository.upsert_subquestion(workspace_id, updated)
        return updated

    def resolve_subquestion(self, workspace_id: str, question_id: str, answer: str) -> SubQuestion:
        self.get(workspace_id)
        current = self.repository.get_subquestion(workspace_id, question_id)
        if current is None:
            raise WorkspaceNotFoundError(question_id)
        updated = current.resolve(answer)
        self.repository.upsert_subquestion(workspace_id, updated)
        return updated

    def deprioritize_subquestion(self, workspace_id: str, question_id: str) -> SubQuestion:
        self.get(workspace_id)
        current = self.repository.get_subquestion(workspace_id, question_id)
        if current is None:
            raise WorkspaceNotFoundError(question_id)
        updated = current.deprioritize()
        self.repository.upsert_subquestion(workspace_id, updated)
        return updated

    # -- Evidence (never copies the paper body; only claim + interpretation) --

    def add_evidence(
        self,
        workspace_id: str,
        *,
        evidence_id: str,
        source_id: str,
        block_ids: tuple[str, ...],
        supports_question_ids: tuple[str, ...] = (),
        evidence_role: str = "supporting",
        claim: str = "",
        research_interpretation: str = "",
        confidence: str = "medium",
    ) -> Evidence:
        self.get(workspace_id)
        evidence = Evidence(
            evidence_id=evidence_id,
            source_id=source_id,
            block_ids=block_ids,
            supports_question_ids=supports_question_ids,
            evidence_role=evidence_role,
            claim=claim,
            research_interpretation=research_interpretation,
            confidence=confidence,
            added_at=self.clock(),
        )
        self.repository.upsert_evidence(workspace_id, evidence)
        return evidence

    def list_subquestions(self, workspace_id: str) -> tuple[SubQuestion, ...]:
        self.get(workspace_id)
        return self.repository.list_subquestions(workspace_id)

    def list_evidence(self, workspace_id: str) -> tuple[Evidence, ...]:
        self.get(workspace_id)
        return self.repository.list_evidence(workspace_id)

    # -- Research Map (structured nodes; reference subjections/evidence by stable id) --

    def add_research_map_node(
        self,
        workspace_id: str,
        node_id: str,
        *,
        label: str = "",
        related_question_ids: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
    ) -> ResearchMapNode:
        self.get(workspace_id)
        node = ResearchMapNode(
            node_id=node_id,
            related_question_ids=tuple(dict.fromkeys(related_question_ids)),
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            label=" ".join(label.split()),
        )
        self.repository.upsert_research_map_node(workspace_id, node)
        return node

    def update_research_map_node(
        self,
        workspace_id: str,
        node_id: str,
        *,
        label: str | None = None,
        related_question_ids: tuple[str, ...] | None = None,
        evidence_ids: tuple[str, ...] | None = None,
    ) -> ResearchMapNode:
        self.get(workspace_id)
        current = self.repository.get_research_map_node(workspace_id, node_id)
        if current is None:
            raise WorkspaceNotFoundError(node_id)
        updated = current
        if label is not None:
            updated = updated.with_label(label)
        if related_question_ids is not None:
            updated = updated.with_question_ids(related_question_ids)
        if evidence_ids is not None:
            updated = updated.with_evidence_ids(evidence_ids)
        self.repository.upsert_research_map_node(workspace_id, updated)
        return updated

    def list_research_map(self, workspace_id: str) -> tuple[ResearchMapNode, ...]:
        self.get(workspace_id)
        return self.repository.list_research_map(workspace_id)
