"""Integrity gate for research-workspace mutations (M3).

The agent never mutates canonical state directly. It produces a
``WorkspacePatch`` (what it wants to change) that a ``WorkspaceGate`` validates
before it is committed. The gate is the programmatic guard for data integrity —
schema legality, stable-ID immutability, evidence reference completeness,
source_id/block_ids presence, optimistic-lock version against a global
``workspace_revision``, and provenance. Human judgement (should this change be
accepted?) is a *different* concern handled by WorkspaceRiskClassifier.

A valid patch projects onto a ``ProjectedWorkspaceState`` — the full post-patch
canonical snapshot plus the next global revision. ``WorkspaceGate.validate``
returns that projection so a ``CommitService`` can persist it atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Callable

from research_pulse.workbench.errors import RevisionConflictError
from research_pulse.workbench.workspace import (
    CandidateHypothesis,
    Evidence,
    ExperimentPlan,
    Critique,
    HypothesisStatus,
    MethodMap,
    MethodMapEntry,
    PlanArtifactStatus,
    ProjectedWorkspaceState,
    ResearchMapNode,
    ResearchPlan,
    ResearchPlanStage,
    ResearchArtifactStage,
    ResearchIteration,
    ResearchIterationStatus,
    SubQuestion,
    Researchability,
    WorkspaceNotFoundError,
    WorkspaceRepository,
)


class WorkspacePatchError(ValueError):
    """Raised when an Operation / Patch is structurally malformed."""


class WorkspaceGateError(ValueError):
    """Raised when a patch violates a data-integrity rule."""


class WorkspaceRevisionConflictError(WorkspaceGateError, RevisionConflictError):
    """Raised only when a patch's optimistic-lock base revision is stale."""


# Operation kinds. Each is keyed to one controlled domain-write tool.
SUBQUESTION_ADD = "subquestion_add"
SUBQUESTION_UPDATE = "subquestion_update"
SUBQUESTION_RESOLVE = "subquestion_resolve"
SUBQUESTION_DEPRIORITIZE = "subquestion_deprioritize"
EVIDENCE_ADD = "evidence_add"
RESEARCH_MAP_ADD = "research_map_add"
RESEARCH_MAP_UPDATE = "research_map_update"
RESEARCH_PLAN_REPLACE = "research_plan_replace"
RESEARCH_ITERATION_APPEND = "research_iteration_append"
RESEARCH_ITERATION_UPDATE = "research_iteration_update"

_OP_KINDS = frozenset({
    SUBQUESTION_ADD,
    SUBQUESTION_UPDATE,
    SUBQUESTION_RESOLVE,
    SUBQUESTION_DEPRIORITIZE,
    EVIDENCE_ADD,
    RESEARCH_MAP_ADD,
    RESEARCH_MAP_UPDATE,
    RESEARCH_PLAN_REPLACE,
    RESEARCH_ITERATION_APPEND,
    RESEARCH_ITERATION_UPDATE,
})


@dataclass(frozen=True)
class WorkspacePatchOperation:
    """One structured mutation, keyed to a stable ``object_id``.

    ``kind`` is the discriminator; ``object_id`` is the stable workspace-scoped
    id being created (add ops) or referenced (update/resolve/deprioritize). An
    operation never renames an existing id — the id is fixed, so the agent cannot
    silently replace one research object with another by editing its key.
    """

    kind: str
    object_id: str
    text: str | None = None
    answer: str | None = None
    source_id: str | None = None
    block_ids: tuple[str, ...] = ()
    supports_question_ids: tuple[str, ...] = ()
    evidence_role: str | None = None
    claim: str = ""
    research_interpretation: str = ""
    confidence: str | None = None
    label: str | None = None
    related_question_ids: tuple[str, ...] | None = None
    evidence_ids: tuple[str, ...] | None = None
    researchability: str | None = None
    # Full replacement is intentional: a plan is one atomic, typed aggregate,
    # never a collection of raw-file edits that can leave half a plan visible.
    research_plan: ResearchPlan | None = None
    # A runtime-owned round receipt.  It is deliberately a separate operation
    # so recording a completed round cannot replace the rest of the plan.
    research_iteration: ResearchIteration | None = None

    def __post_init__(self) -> None:
        if self.kind not in _OP_KINDS:
            raise WorkspacePatchError(f"unknown operation kind: {self.kind}")
        if not self.object_id.strip():
            raise WorkspacePatchError("object_id is required")


@dataclass(frozen=True)
class WorkspacePatch:
    """A proposed, optimistic-locked batch of structured operations.

    ``base_workspace_revision`` is the global workspace revision the patch was
    built against; it must equal the current revision at commit time or the patch
    is stale (rejected as a conflict). ``provenance`` records who/what proposed
    the change so the gate can confirm the patch is attributable.
    """

    base_workspace_revision: int
    operations: tuple[WorkspacePatchOperation, ...]
    provenance: str = ""

    def __post_init__(self) -> None:
        if self.base_workspace_revision < 1:
            raise WorkspacePatchError("base_workspace_revision must be a positive integer")
        if not self.operations:
            raise WorkspacePatchError("a patch must contain at least one operation")
        if not self.provenance.strip():
            raise WorkspacePatchError("provenance is required")


_DEFAULT_NOW: Callable[[], datetime] = lambda: datetime.now(UTC)


class WorkspaceGate:
    """Programmatic data-integrity guard for a WorkspacePatch.

    Reads the current canonical state through the repository, checks the
    optimistic-lock revision, projects the operations onto the post-patch state,
    and verifies every data-integrity rule. Returns ``ProjectedWorkspaceState``
    on success; raises ``WorkspaceGateError`` / ``WorkspacePatchError`` otherwise.
    """

    def __init__(self, repository: WorkspaceRepository, *, clock: Callable[[], datetime] = _DEFAULT_NOW) -> None:
        self._repository = repository
        self._clock = clock

    def validate(self, workspace_id: str, patch: WorkspacePatch) -> ProjectedWorkspaceState:
        workspace = self._repository.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)

        if patch.base_workspace_revision != workspace.workspace_revision:
            raise WorkspaceRevisionConflictError(
                f"optimistic-lock conflict: patch based on revision "
                f"{patch.base_workspace_revision} but workspace is at "
                f"{workspace.workspace_revision}"
            )

        subquestions = {q.question_id: q for q in self._repository.list_subquestions(workspace_id)}
        evidence = {e.evidence_id: e for e in self._repository.list_evidence(workspace_id)}
        research_map = {n.node_id: n for n in self._repository.list_research_map(workspace_id)}

        research_plan: ResearchPlan | None = None
        plan_changed = False
        for op in patch.operations:
            if op.kind == RESEARCH_PLAN_REPLACE:
                if plan_changed:
                    raise WorkspaceGateError("a patch may contain only one research plan replacement")
                if op.object_id != "RESEARCH-PLAN":
                    raise WorkspaceGateError("research plan operation must use object_id RESEARCH-PLAN")
                if op.research_plan is None:
                    raise WorkspaceGateError("research plan replacement requires a plan")
                research_plan = op.research_plan
                plan_changed = True
                continue
            if op.kind == RESEARCH_ITERATION_APPEND:
                if plan_changed:
                    raise WorkspaceGateError(
                        "a patch cannot mix a full research plan replacement with an iteration append"
                    )
                if op.object_id != (op.research_iteration.iteration_id if op.research_iteration else ""):
                    raise WorkspaceGateError("iteration append object_id must match iteration_id")
                if op.research_iteration is None:
                    raise WorkspaceGateError("iteration append requires a research iteration")
                if any(item.iteration_id == op.research_iteration.iteration_id for item in workspace.research_plan.iterations):
                    raise WorkspaceGateError(
                        f"research iteration {op.research_iteration.iteration_id} already exists"
                    )
                research_plan = replace(
                    workspace.research_plan,
                    artifact_stage=(
                        ResearchArtifactStage.STAGE_NOTE
                        if workspace.research_plan.artifact_stage is ResearchArtifactStage.OVERVIEW
                        else workspace.research_plan.artifact_stage
                    ),
                    iterations=(*workspace.research_plan.iterations, op.research_iteration),
                )
                plan_changed = True
                continue
            if op.kind == RESEARCH_ITERATION_UPDATE:
                if plan_changed:
                    raise WorkspaceGateError(
                        "a patch cannot mix a full research plan replacement with an iteration update"
                    )
                if op.research_iteration is None:
                    raise WorkspaceGateError("iteration update requires a research iteration")
                if op.object_id != op.research_iteration.iteration_id:
                    raise WorkspaceGateError("iteration update object_id must match iteration_id")
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(workspace.research_plan.iterations)
                        if item.iteration_id == op.research_iteration.iteration_id
                    ),
                    None,
                )
                if existing_index is None:
                    raise WorkspaceGateError(
                        f"research iteration {op.research_iteration.iteration_id} does not exist"
                    )
                existing_item = workspace.research_plan.iterations[existing_index]
                if (
                    existing_item.sequence != op.research_iteration.sequence
                    or existing_item.run_id != op.research_iteration.run_id
                ):
                    raise WorkspaceGateError(
                        "iteration identity fields cannot change during an update"
                    )
                if (
                    existing_item.status is ResearchIterationStatus.COMPLETED
                    and op.research_iteration.status is not ResearchIterationStatus.COMPLETED
                ):
                    raise WorkspaceGateError(
                        "completed research iterations cannot regress"
                    )
                updated_iterations = list(workspace.research_plan.iterations)
                updated_iterations[existing_index] = op.research_iteration
                research_plan = replace(
                    workspace.research_plan,
                    artifact_stage=(
                        ResearchArtifactStage.STAGE_NOTE
                        if workspace.research_plan.artifact_stage is ResearchArtifactStage.OVERVIEW
                        else workspace.research_plan.artifact_stage
                    ),
                    iterations=tuple(updated_iterations),
                )
                plan_changed = True
                continue
            self._apply_operation(op, subquestions, evidence, research_map)

        # A workspace with durable evidence or an iteration has moved beyond a
        # bare overview.  Promote the user-facing artifact atomically with the
        # same patch so all readers (including legacy JSON readers) see one
        # consistent maturity value.  Agents cannot request this promotion
        # themselves; it is a deterministic Harness lifecycle transition.
        current_plan = research_plan if plan_changed else workspace.research_plan
        if (
            current_plan.artifact_stage is ResearchArtifactStage.OVERVIEW
            and (evidence or current_plan.iterations)
        ):
            research_plan = replace(
                current_plan,
                artifact_stage=ResearchArtifactStage.STAGE_NOTE,
            )
            plan_changed = True

        self._check_references(subquestions, evidence, research_map)
        if plan_changed:
            self._check_research_plan_references(research_plan, subquestions, evidence)

        return ProjectedWorkspaceState(
            workspace_revision=workspace.workspace_revision + 1,
            subquestions=tuple(subquestions.values()),
            evidence=tuple(evidence.values()),
            research_map=tuple(research_map.values()),
            research_plan=research_plan if plan_changed else None,
        )

    def _apply_operation(
        self,
        op: WorkspacePatchOperation,
        subquestions: dict[str, SubQuestion],
        evidence: dict[str, Evidence],
        research_map: dict[str, ResearchMapNode],
    ) -> None:
        if op.kind == SUBQUESTION_ADD:
            if op.object_id in subquestions:
                raise WorkspaceGateError(f"stable id {op.object_id} already exists")
            if not op.text or not op.text.strip():
                raise WorkspaceGateError("subquestion add requires non-blank text")
            subquestions[op.object_id] = SubQuestion(
                question_id=op.object_id,
                text=" ".join(op.text.split()),
                researchability=_parse_researchability(op.researchability),
            )
        elif op.kind == SUBQUESTION_UPDATE:
            current = subquestions.get(op.object_id)
            if current is None:
                raise WorkspaceGateError(f"subquestion {op.object_id} does not exist")
            if not op.text or not op.text.strip():
                raise WorkspaceGateError("subquestion update requires non-blank text")
            updated = current.with_text(op.text)
            if op.researchability is not None:
                updated = updated.with_researchability(_parse_researchability(op.researchability))
            subquestions[op.object_id] = updated
        elif op.kind == SUBQUESTION_RESOLVE:
            current = subquestions.get(op.object_id)
            if current is None:
                raise WorkspaceGateError(f"subquestion {op.object_id} does not exist")
            if op.answer is None or not op.answer.strip():
                raise WorkspaceGateError("subquestion resolve requires an answer")
            subquestions[op.object_id] = current.resolve(op.answer)
        elif op.kind == SUBQUESTION_DEPRIORITIZE:
            current = subquestions.get(op.object_id)
            if current is None:
                raise WorkspaceGateError(f"subquestion {op.object_id} does not exist")
            subquestions[op.object_id] = current.deprioritize()
        elif op.kind == EVIDENCE_ADD:
            if op.object_id in evidence:
                raise WorkspaceGateError(f"stable id {op.object_id} already exists")
            if not op.source_id or not op.source_id.strip():
                raise WorkspaceGateError("evidence requires source_id")
            if not op.block_ids:
                raise WorkspaceGateError("evidence requires block_ids")
            if len(set(op.block_ids)) != len(op.block_ids):
                raise WorkspaceGateError("evidence block_ids must be unique")
            evidence[op.object_id] = Evidence(
                evidence_id=op.object_id,
                source_id=op.source_id,
                block_ids=op.block_ids,
                supports_question_ids=op.supports_question_ids,
                evidence_role=op.evidence_role or "supporting",
                claim=op.claim,
                research_interpretation=op.research_interpretation,
                confidence=op.confidence or "medium",
                added_at=self._clock(),
            )
        elif op.kind == RESEARCH_MAP_ADD:
            if op.object_id in research_map:
                raise WorkspaceGateError(f"stable id {op.object_id} already exists")
            research_map[op.object_id] = ResearchMapNode(
                node_id=op.object_id,
                related_question_ids=tuple(dict.fromkeys(op.related_question_ids or ())),
                evidence_ids=tuple(dict.fromkeys(op.evidence_ids or ())),
                label=" ".join((op.label or "").split()),
            )
        elif op.kind == RESEARCH_MAP_UPDATE:
            current = research_map.get(op.object_id)
            if current is None:
                raise WorkspaceGateError(f"research map node {op.object_id} does not exist")
            if op.label is not None:
                current = current.with_label(op.label)
            if op.related_question_ids is not None:
                current = current.with_question_ids(tuple(dict.fromkeys(op.related_question_ids)))
            if op.evidence_ids is not None:
                current = current.with_evidence_ids(tuple(dict.fromkeys(op.evidence_ids)))
            research_map[op.object_id] = current
        else:  # pragma: no cover - guarded by __post_init__
            raise WorkspacePatchError(f"unknown operation kind: {op.kind}")

    @staticmethod
    def _check_references(
        subquestions: dict[str, SubQuestion],
        evidence: dict[str, Evidence],
        research_map: dict[str, ResearchMapNode],
    ) -> None:
        for ev in evidence.values():
            for qid in ev.supports_question_ids:
                if qid not in subquestions:
                    raise WorkspaceGateError(
                        f"evidence {ev.evidence_id} references unknown subquestion {qid}"
                    )
        for node in research_map.values():
            for qid in node.related_question_ids:
                if qid not in subquestions:
                    raise WorkspaceGateError(
                        f"research map node {node.node_id} references unknown subquestion {qid}"
                    )
            for eid in node.evidence_ids:
                if eid not in evidence:
                    raise WorkspaceGateError(
                        f"research map node {node.node_id} references unknown evidence {eid}"
                    )

    @staticmethod
    def _check_research_plan_references(
        plan: ResearchPlan | None,
        subquestions: dict[str, SubQuestion],
        evidence: dict[str, Evidence],
    ) -> None:
        if plan is None:  # pragma: no cover - guarded by validate
            raise WorkspaceGateError("research plan replacement requires a plan")
        if not isinstance(plan.stage, ResearchPlanStage):
            raise WorkspaceGateError("research plan stage is invalid")

        def unique_nonblank(values: tuple[str, ...], label: str) -> None:
            if any(not value or not value.strip() for value in values):
                raise WorkspaceGateError(f"{label} ids must not be blank")
            if len(set(values)) != len(values):
                raise WorkspaceGateError(f"{label} ids must be unique")

        if not all(isinstance(item, ResearchIteration) for item in plan.iterations):
            raise WorkspaceGateError("iterations must contain ResearchIteration values")
        unique_nonblank(tuple(item.iteration_id for item in plan.iterations), "iteration")
        sequences = tuple(item.sequence for item in plan.iterations)
        if any(sequence < 1 for sequence in sequences):
            raise WorkspaceGateError("iteration sequence must be a positive integer")
        if len(set(sequences)) != len(sequences):
            raise WorkspaceGateError("iteration sequence values must be unique")
        for item in plan.iterations:
            if not item.title.strip():
                raise WorkspaceGateError(f"iteration {item.iteration_id} requires a title")
            if not isinstance(item.status, ResearchIterationStatus):
                raise WorkspaceGateError(f"iteration {item.iteration_id} status is invalid")
            if item.focus_question_id is not None and item.focus_question_id not in subquestions:
                raise WorkspaceGateError(
                    f"iteration {item.iteration_id} references unknown focus question {item.focus_question_id}"
                )
            _check_known_ids(item.evidence_ids, evidence, f"iteration {item.iteration_id}")
            for question_id in item.candidate_question_ids:
                if question_id not in subquestions:
                    raise WorkspaceGateError(
                        f"iteration {item.iteration_id} references unknown candidate question {question_id}"
                    )

        method_map = plan.method_map
        if method_map is not None:
            if not isinstance(method_map, MethodMap):
                raise WorkspaceGateError("method_map must be a MethodMap")
            if not all(isinstance(entry, MethodMapEntry) for entry in method_map.entries):
                raise WorkspaceGateError("method_map.entries must contain MethodMapEntry values")
            if not method_map.map_id.strip():
                raise WorkspaceGateError("method_map.map_id must not be blank")
            unique_nonblank(tuple(entry.method_id for entry in method_map.entries), "method")
            if not isinstance(method_map.status, PlanArtifactStatus):
                raise WorkspaceGateError("method_map status is invalid")
            for entry in method_map.entries:
                if not entry.name.strip():
                    raise WorkspaceGateError(f"method {entry.method_id} requires a name")
                _check_known_ids(
                    entry.evidence_ids,
                    evidence,
                    f"method {entry.method_id}",
                )

        if not all(isinstance(item, CandidateHypothesis) for item in plan.hypotheses):
            raise WorkspaceGateError("hypotheses must contain CandidateHypothesis values")
        unique_nonblank(tuple(item.hypothesis_id for item in plan.hypotheses), "hypothesis")
        hypothesis_ids = {item.hypothesis_id for item in plan.hypotheses}
        for item in plan.hypotheses:
            if not item.text.strip():
                raise WorkspaceGateError(f"hypothesis {item.hypothesis_id} requires text")
            if not isinstance(item.status, HypothesisStatus):
                raise WorkspaceGateError(f"hypothesis {item.hypothesis_id} status is invalid")
            if item.question_id is not None and item.question_id not in subquestions:
                raise WorkspaceGateError(
                    f"hypothesis {item.hypothesis_id} references unknown subquestion {item.question_id}"
                )
            _check_known_ids(item.evidence_ids, evidence, f"hypothesis {item.hypothesis_id}")

        if not all(isinstance(item, Critique) for item in plan.critiques):
            raise WorkspaceGateError("critiques must contain Critique values")
        unique_nonblank(tuple(item.critique_id for item in plan.critiques), "critique")
        for item in plan.critiques:
            if item.hypothesis_id not in hypothesis_ids:
                raise WorkspaceGateError(
                    f"critique {item.critique_id} references unknown hypothesis {item.hypothesis_id}"
                )
            if not isinstance(item.status, PlanArtifactStatus):
                raise WorkspaceGateError(f"critique {item.critique_id} status is invalid")
            _check_known_ids(item.evidence_ids, evidence, f"critique {item.critique_id}")

        if not all(isinstance(item, ExperimentPlan) for item in plan.experiment_plans):
            raise WorkspaceGateError("experiment_plans must contain ExperimentPlan values")
        unique_nonblank(tuple(item.plan_id for item in plan.experiment_plans), "experiment plan")
        for item in plan.experiment_plans:
            if not isinstance(item.status, PlanArtifactStatus):
                raise WorkspaceGateError(f"experiment plan {item.plan_id} status is invalid")
            if item.hypothesis_id is not None and item.hypothesis_id not in hypothesis_ids:
                raise WorkspaceGateError(
                    f"experiment plan {item.plan_id} references unknown hypothesis {item.hypothesis_id}"
                )


def _check_known_ids(values: tuple[str, ...], known: dict[str, object], owner: str) -> None:
    if len(set(values)) != len(values):
        raise WorkspaceGateError(f"{owner} evidence ids must be unique")
    for value in values:
        if value not in known:
            raise WorkspaceGateError(f"{owner} references unknown evidence {value}")


def _parse_researchability(value: str | None) -> Researchability:
    if value is None or not str(value).strip():
        # Backward-compatible direct callers retain the historical candidate
        # behavior; the model-facing adapter supplies ``unknown`` explicitly.
        return Researchability.CANDIDATE
    try:
        return Researchability(str(value).strip().lower())
    except ValueError as exc:
        raise WorkspaceGateError(
            "researchability must be one of candidate, boundary, unknown"
        ) from exc
