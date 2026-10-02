"""Controlled workspace write tools for the research-assistant profile.

These are the ONLY way the agent may change canonical workspace state. Unlike
the raw file tools that path-scoping blocks from ``workspace.json`` /
``research-map.json`` / ``subquestions.json`` / ``research-plan.json`` /
``evidence/*.json``, these tools
express *what the agent wants to change* (a ``WorkspacePatch``) and then route it
through the domain pipeline:

    propose → WorkspaceGate (integrity) → WorkspaceRiskClassifier (grading)
             → CommitService (atomic commit)   [auto / review]
             → HITLService (DecisionPoint)     [hitl]

The tool itself never decides grading or whether to commit — the domain layer
encapsulates that (so an agent cannot self-certify a risky change as "low risk").
The agent never sees ``propose_patch``; it only sees the three controlled write
tools, whose internal behavior changed from "direct submit" to "propose + route".

Read-only research tools live in ``research_tools.ReadOnlyResearchTools``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from research_pulse.workbench.hitl import DecisionKind, HITLService
from research_pulse.workbench.supporting_paper import (
    SupportingPaperImporter,
    SupportingPaperImportError,
)
from research_pulse.workbench.workspace import (
    CandidateHypothesis,
    Critique,
    ExperimentPlan,
    HypothesisStatus,
    MethodMap,
    MethodMapEntry,
    PlanArtifactStatus,
    ResearchPlan,
    ResearchPlanStage,
    ResearchIteration,
    ResearchIterationStatus,
    Researchability,
    SubQuestionStatus,
    WorkspaceService,
)
from research_pulse.workbench.workspace_commit import CommitService
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
    WorkspaceGate,
    WorkspaceGateError,
    WorkspacePatchOperation,
)
from research_pulse.workbench.workspace_risk import WorkspaceRiskClassifier
from research_pulse.workbench.workspace_pipeline import WorkspaceOrchestrator


class WorkspaceToolError(ValueError):
    """Safe public error for a rejected controlled workspace write."""


@dataclass(frozen=True)
class ManagedWorkspaceState:
    """A snapshot of the per-run workspace binding passed to the tool facade."""
    workspace_id: str


def _research_plan_projection(plan: ResearchPlan) -> dict[str, object]:
    """Expose plan artifacts to the agent without exposing raw JSON files."""
    return {
        "stage": plan.stage.value,
        "artifact_stage": plan.artifact_stage.value,
        "iterations": [
            {
                "iteration_id": item.iteration_id,
                "sequence": item.sequence,
                "title": item.title,
                "status": item.status.value,
                "focus_question_id": item.focus_question_id,
                "run_id": item.run_id,
                "summary": item.summary,
                "evidence_ids": list(item.evidence_ids),
                "candidate_question_ids": list(item.candidate_question_ids),
                "decision": item.decision,
                "next_step": item.next_step,
            }
            for item in plan.iterations
        ],
        "method_map": (
            {
                "map_id": plan.method_map.map_id,
                "status": plan.method_map.status.value,
                "entries": [
                    {
                        "method_id": entry.method_id,
                        "name": entry.name,
                        "mechanism": entry.mechanism,
                        "assumptions": entry.assumptions,
                        "evidence_ids": list(entry.evidence_ids),
                        "limitations": entry.limitations,
                    }
                    for entry in plan.method_map.entries
                ],
            }
            if plan.method_map is not None
            else None
        ),
        "hypotheses": [
            {
                "hypothesis_id": item.hypothesis_id,
                "text": item.text,
                "question_id": item.question_id,
                "evidence_ids": list(item.evidence_ids),
                "rationale": item.rationale,
                "falsifiers": list(item.falsifiers),
                "status": item.status.value,
            }
            for item in plan.hypotheses
        ],
        "critiques": [
            {
                "critique_id": item.critique_id,
                "hypothesis_id": item.hypothesis_id,
                "strengths": list(item.strengths),
                "risks": list(item.risks),
                "alternatives": list(item.alternatives),
                "evidence_ids": list(item.evidence_ids),
                "confidence": item.confidence,
                "status": item.status.value,
            }
            for item in plan.critiques
        ],
        "experiment_plans": [
            {
                "plan_id": item.plan_id,
                "hypothesis_id": item.hypothesis_id,
                "status": item.status.value,
                "intervention": item.intervention,
                "baselines": list(item.baselines),
                "datasets": list(item.datasets),
                "metrics": list(item.metrics),
                "ablations": list(item.ablations),
                "expected_outcomes": item.expected_outcomes,
                "decision_criteria": item.decision_criteria,
                "resource_estimate": item.resource_estimate,
                "risks": list(item.risks),
            }
            for item in plan.experiment_plans
        ],
    }


class WorkspaceResearchTools:
    """Controlled read + write facade over one workspace's canonical state.

    The facade is bound to a single workspace_id per run, so the agent can only
    mutate the workspace it is currently working in. Every write is expressed as
    a ``WorkspacePatch`` and routed through the integrity gate, the risk
    classifier and either the commit service (auto/review) or the HITL service
    (hitl) by this facade — the agent cannot bypass the gate or self-grade.
    """

    def __init__(
        self,
        service: WorkspaceService,
        workspace_id: str,
        *,
        gate: WorkspaceGate,
        risk_classifier: WorkspaceRiskClassifier,
        commit_service: CommitService,
        hitl_service: HITLService,
        run_id: str = "",
        importer: SupportingPaperImporter | None = None,
        # Optional: link an imported paper into the importing session's
        # workspace scope so it shows up in the session's paper list (right
        # context) and becomes readable by later runs. Without it an imported
        # source would be registered but invisible/unreadable.
        paper_linker: Callable[[str], None] | None = None,
    ) -> None:
        if not workspace_id.strip():
            raise ValueError("workspace id must not be blank")
        self._service = service
        self._workspace_id = workspace_id
        self._orchestrator = WorkspaceOrchestrator(
            service,
            gate=gate,
            risk_classifier=risk_classifier,
            commit_service=commit_service,
            hitl_service=hitl_service,
        )
        self._run_id = run_id
        self._importer = importer
        self._paper_linker = paper_linker
        self._pending_decision_id: str | None = None
        # Probe the binding once so a missing workspace fails fast and closed.
        self._service.get(workspace_id)

    @property
    def capability_names(self) -> tuple[str, ...]:
        # import_supporting_paper only exists when an importer is wired in
        # (fail-closed: without it the tool is absent from the surface).
        base = (
            "read_workspace_state",
            "update_subquestions",
            "add_evidence",
            "update_research_map",
            "update_research_plan",
        )
        return base + ("import_supporting_paper",) if self._importer is not None else base

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def pending_decision_id(self) -> str | None:
        """The decision point created by a hitl write this run, if any."""
        return self._pending_decision_id

    # -- read ----------------------------------------------------------------

    def read_workspace_state(self) -> dict[str, object]:
        workspace = self._service.get(self._workspace_id)
        return {
            "workspace_id": workspace.workspace_id,
            "research_question": workspace.research_question,
            "research_intent": workspace.research_intent,
            "active_focus_id": workspace.active_focus_id,
            "anchor_paper_id": workspace.anchor_paper_id,
            "status": workspace.status.value,
            "research_map": [
                {
                    "node_id": node.node_id,
                    "label": node.label,
                    "related_question_ids": list(node.related_question_ids),
                    "evidence_ids": list(node.evidence_ids),
                }
                for node in self._service.list_research_map(self._workspace_id)
            ],
            "subquestions": [
                {
                    "question_id": question.question_id,
                    "text": question.text,
                    "status": question.status.value,
                    "answer": question.answer,
                    "researchability": question.researchability.value,
                }
                for question in self._service.list_subquestions(self._workspace_id)
            ],
            "evidence": [
                {
                    "evidence_id": evidence.evidence_id,
                    "source_id": evidence.source_id,
                    "block_ids": list(evidence.block_ids),
                    "supports_question_ids": list(evidence.supports_question_ids),
                    "evidence_role": evidence.evidence_role,
                    "claim": evidence.claim,
                    "research_interpretation": evidence.research_interpretation,
                    "confidence": evidence.confidence,
                }
                for evidence in self._service.list_evidence(self._workspace_id)
            ],
            "research_plan": _research_plan_projection(workspace.research_plan),
        }

    # -- controlled writes (propose → gate → risk → commit / hitl) ------------

    def update_subquestions(
        self,
        operation: str,
        question_id: str,
        *,
        text: str | None = None,
        answer: str | None = None,
        researchability: str | None = None,
    ) -> dict[str, object]:
        """Apply ONE structured subquestion operation, never a bulk overwrite.

        operation is one of add | update | resolve | deprioritize. For add/update,
        researchability classifies whether the item is a testable follow-up
        candidate, an author-stated boundary, or not yet judged. The change is
        proposed as a patch, integrity-gated, risk-classified and either committed
        (auto/review) or suspended as a DecisionPoint (hitl: resolve).
        """
        normalized = _normalize_operation(operation)
        if not question_id.strip():
            raise WorkspaceToolError("question_id is required")
        if normalized == "add":
            if text is None or not text.strip():
                raise WorkspaceToolError("add requires non-blank text")
            op = WorkspacePatchOperation(
                kind=SUBQUESTION_ADD,
                object_id=question_id,
                text=text,
                researchability=researchability,
            )
        elif normalized == "update":
            if text is None or not text.strip():
                raise WorkspaceToolError("update requires non-blank text")
            op = WorkspacePatchOperation(
                kind=SUBQUESTION_UPDATE,
                object_id=question_id,
                text=text,
                researchability=researchability,
            )
        elif normalized == "resolve":
            if answer is None:
                raise WorkspaceToolError("resolve requires an answer")
            op = WorkspacePatchOperation(kind=SUBQUESTION_RESOLVE, object_id=question_id, answer=answer)
        else:  # deprioritize
            op = WorkspacePatchOperation(kind=SUBQUESTION_DEPRIORITIZE, object_id=question_id)

        outcome = self._apply_patch((op,), prompt=_describe_patch_operations((op,)))
        if outcome is None:
            return {
                "question_id": question_id,
                "status": "awaiting_decision",
                "decision_point_id": self._pending_decision_id,
                "decision_kind": DecisionKind.PATCH_APPROVAL.value,
            }
        produced = _first(lambda q: q.question_id == question_id, self._service.list_subquestions(self._workspace_id))
        if produced is None:
            raise WorkspaceToolError("subquestion write did not persist")
        return {
            "question_id": produced.question_id,
            "text": produced.text,
            "status": produced.status.value,
            "answer": produced.answer,
            "researchability": produced.researchability.value,
        }

    def add_evidence(
        self,
        evidence_id: str,
        source_id: str,
        block_ids: list[str],
        *,
        supports_question_ids: list[str] | None = None,
        evidence_role: str = "supporting",
        claim: str = "",
        research_interpretation: str = "",
        confidence: str = "medium",
    ) -> dict[str, object]:
        """Record evidence that a source supports a research question.

        source_id + block_ids are REQUIRED (otherwise the evidence is untethered
        and rejected); the paper body is never copied here — only the claim and
        the interpretation for this research question. Expresses an evidence_add
        patch; additive evidence commits (auto), conflicting evidence suspends.
        """
        if not evidence_id.strip() or not source_id.strip():
            raise WorkspaceToolError("evidence_id and source_id are required")
        if not block_ids or len(set(block_ids)) != len(block_ids):
            raise WorkspaceToolError("block_ids must be a non-empty list of unique full ids")
        op = WorkspacePatchOperation(
            kind=EVIDENCE_ADD,
            object_id=evidence_id,
            source_id=source_id,
            block_ids=tuple(block_ids),
            supports_question_ids=tuple(supports_question_ids or ()),
            evidence_role=evidence_role,
            claim=claim,
            research_interpretation=research_interpretation,
            confidence=confidence,
        )
        outcome = self._apply_patch((op,), prompt=_describe_patch_operations((op,)))
        if outcome is None:
            return {
                "evidence_id": evidence_id,
                "status": "awaiting_decision",
                "decision_point_id": self._pending_decision_id,
                "decision_kind": DecisionKind.PATCH_APPROVAL.value,
            }
        produced = _first(lambda e: e.evidence_id == evidence_id, self._service.list_evidence(self._workspace_id))
        if produced is None:
            raise WorkspaceToolError("evidence write did not persist")
        return {
            "evidence_id": produced.evidence_id,
            "source_id": produced.source_id,
            "block_ids": list(produced.block_ids),
            "supports_question_ids": list(produced.supports_question_ids),
            "evidence_role": produced.evidence_role,
        }

    def update_research_map(
        self,
        node_id: str,
        *,
        label: str | None = None,
        related_question_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """Add or update a Research Map node referenced by stable ids."""
        if not node_id.strip():
            raise WorkspaceToolError("node_id is required")
        existing = self._service.repository.get_research_map_node(self._workspace_id, node_id)
        kind = RESEARCH_MAP_UPDATE if existing is not None else RESEARCH_MAP_ADD
        op = WorkspacePatchOperation(
            kind=kind,
            object_id=node_id,
            label=label,
            related_question_ids=tuple(related_question_ids) if related_question_ids is not None else None,
            evidence_ids=tuple(evidence_ids) if evidence_ids is not None else None,
        )
        outcome = self._apply_patch((op,), prompt=_describe_patch_operations((op,)))
        if outcome is None:
            return {
                "node_id": node_id,
                "status": "awaiting_decision",
                "decision_point_id": self._pending_decision_id,
                "decision_kind": DecisionKind.PATCH_APPROVAL.value,
            }
        produced = self._service.repository.get_research_map_node(self._workspace_id, node_id)
        if produced is None:
            raise WorkspaceToolError("research map write did not persist")
        return {
            "node_id": produced.node_id,
            "label": produced.label,
            "related_question_ids": list(produced.related_question_ids),
            "evidence_ids": list(produced.evidence_ids),
        }

    def update_research_plan(self, plan: Mapping[str, Any]) -> dict[str, object]:
        """Replace the typed research plan through the controlled write pipeline.

        The public tool accepts one nested mapping so the whole plan is gated and
        committed atomically. Omitted top-level sections preserve the current
        plan, while a supplied section replaces that section; raw
        ``research-plan.json`` writes are never accepted as an alternative.
        Draft artifacts route to review, whereas selected/approved/ready plans
        are held for an explicit user decision by the risk classifier.
        """
        current = self._service.get(self._workspace_id).research_plan
        try:
            proposed = _research_plan_from_input(plan, current)
        except (TypeError, ValueError) as error:
            raise WorkspaceToolError(str(error)) from error
        op = WorkspacePatchOperation(
            kind=RESEARCH_PLAN_REPLACE,
            object_id="RESEARCH-PLAN",
            research_plan=proposed,
        )
        outcome = self._apply_patch((op,), prompt=_describe_patch_operations((op,)))
        if outcome is None:
            return {
                "status": "awaiting_decision",
                "decision_point_id": self._pending_decision_id,
                "decision_kind": DecisionKind.PATCH_APPROVAL.value,
                "research_plan": _research_plan_projection(proposed),
            }
        persisted = self._service.get(self._workspace_id).research_plan
        return {
            "status": "committed",
            "research_plan": _research_plan_projection(persisted),
        }

    def append_research_iteration(self, iteration: ResearchIteration) -> dict[str, object]:
        """Record one completed/abandoned round without replacing the plan.

        This is a Harness-owned receipt rather than an agent-facing tool.  It
        uses its own append operation so a runtime completion hook cannot erase
        hypotheses or prior iteration history through a full-plan replacement.
        Repeating the same ``iteration_id`` is idempotent for recovery paths.
        """
        current = self._service.get(self._workspace_id).research_plan
        existing = next(
            (
                item
                for item in current.iterations
                if item.iteration_id == iteration.iteration_id
                or (iteration.run_id is not None and item.run_id == iteration.run_id)
            ),
            None,
        )
        if existing is not None:
            return {
                "status": "already_recorded",
                "iteration_id": existing.iteration_id,
                "research_plan": _research_plan_projection(current),
            }
        op = WorkspacePatchOperation(
            kind=RESEARCH_ITERATION_APPEND,
            object_id=iteration.iteration_id,
            research_iteration=iteration,
        )
        outcome = self._apply_patch((op,), prompt=_describe_patch_operations((op,)))
        if outcome is None:
            return {
                "status": "awaiting_decision",
                "decision_point_id": self._pending_decision_id,
                "decision_kind": DecisionKind.PATCH_APPROVAL.value,
            }
        persisted = self._service.get(self._workspace_id).research_plan
        return {
            "status": "committed",
            "iteration_id": iteration.iteration_id,
            "research_plan": _research_plan_projection(persisted),
        }

    def update_research_iteration(self, iteration: ResearchIteration) -> dict[str, object]:
        """Refresh runtime metadata for an existing round receipt.

        This is Harness-owned and intentionally not exposed to the Agent.  It
        is used when a budget continuation completes a round that was first
        recorded as abandoned; identity fields stay stable while the summary,
        evidence and terminal status are refreshed.
        """
        current = self._service.get(self._workspace_id).research_plan
        existing = next(
            (item for item in current.iterations if item.iteration_id == iteration.iteration_id),
            None,
        )
        if existing is None:
            raise WorkspaceToolError("research iteration does not exist")
        if existing == iteration:
            return {
                "status": "already_recorded",
                "iteration_id": iteration.iteration_id,
                "research_plan": _research_plan_projection(current),
            }
        op = WorkspacePatchOperation(
            kind=RESEARCH_ITERATION_UPDATE,
            object_id=iteration.iteration_id,
            research_iteration=iteration,
        )
        outcome = self._apply_patch((op,), prompt=_describe_patch_operations((op,)))
        if outcome is None:
            return {
                "status": "awaiting_decision",
                "decision_point_id": self._pending_decision_id,
                "decision_kind": DecisionKind.PATCH_APPROVAL.value,
            }
        persisted = self._service.get(self._workspace_id).research_plan
        return {
            "status": "committed",
            "iteration_id": iteration.iteration_id,
            "research_plan": _research_plan_projection(persisted),
        }

    def import_supporting_paper(self, candidate: Mapping[str, Any]) -> dict[str, object]:
        """Turn a search_arxiv candidate into a managed source (thin, reuses pipeline).

        Fail-closed: requires an importer to be wired in; otherwise the tool is
        absent from the surface, so the model cannot reach it. Never reads the
        paper body here — the managed material is read later via read_managed_blocks.
        """
        if self._importer is None:
            raise WorkspaceToolError("supporting paper import is not configured")
        if "arxiv_id" not in candidate and "url" not in candidate and "source_id" not in candidate:
            raise WorkspaceToolError("candidate must provide arxiv_id or url")
        result = self._importer.import_paper(dict(candidate))
        if self._paper_linker is not None and result.source_id:
            self._paper_linker(result.source_id)
        return {
            "source_id": result.source_id,
            "source_identity": result.source_identity or "",
            "source_url": result.source_url or "",
            "pdf_status": result.pdf_status,
            "parse_status": result.parse_status,
            "sample_block_id": result.sample_block_id,
            "safe_error": result.safe_error,
        }

    # -- domain pipeline ------------------------------------------------------

    def _apply_patch(self, operations: tuple[WorkspacePatchOperation, ...], *, prompt: str) -> object | None:
        """Gate → risk → commit/HITL. Returns None when a HITL decision suspends."""
        try:
            result = self._orchestrator.route_patch(
                self._workspace_id,
                self._run_id,
                operations,
                prompt=prompt,
                provenance=self._provenance(),
            )
        except WorkspaceGateError as error:
            raise WorkspaceToolError(str(error)) from error
        if result.decision is not None:
            self._pending_decision_id = result.decision.decision_id
            return None
        return result.projected

    def _provenance(self) -> str:
        return f"agent:run:{self._run_id}" if self._run_id else "agent:tool"

    def report_direction_decision(self, *, allow_later: bool = True) -> str | None:
        """Ask for the first focus or the next focus after a bounded round.

        The orchestrator only creates a checkpoint when it has a complete map
        and at least one open candidate question.  Later rounds can be
        explicitly requested with ``allow_later=True``; the normal runtime
        passes ``False`` so a completed answer is not interrupted by a new
        suggested question.
        """
        decision = self._orchestrator.report_direction_decision(
            self._workspace_id, self._run_id, allow_later=allow_later
        )
        if decision is None:
            return None
        self._pending_decision_id = decision.decision_id
        return decision.decision_id

    def ensure_initial_map(self) -> bool:
        """Materialize a minimal initial map when the agent omitted one.

        Initial exploration may end because of a token/round limit or a model
        response that contains candidate questions but no explicit map call.
        Requiring one more model call makes the direction checkpoint disappear
        even though the useful candidates are already durable. This deterministic
        map links only open candidate questions and known evidence, and still
        passes through the normal gate/risk/commit pipeline.
        """
        if self._service.list_research_map(self._workspace_id):
            return False
        candidates = tuple(
            question.question_id
            for question in self._service.list_subquestions(self._workspace_id)
            if question.researchability is Researchability.CANDIDATE
            and question.status is SubQuestionStatus.OPEN
        )
        if not candidates:
            return False
        evidence_ids = tuple(
            evidence.evidence_id
            for evidence in self._service.list_evidence(self._workspace_id)
        )
        node = self.update_research_map(
            "node-initial-research",
            label="初步研究方向（待用户确认）",
            related_question_ids=list(candidates[:8]),
            evidence_ids=list(evidence_ids[:8]),
        )
        return bool(node and node.get("node_id"))

    def ensure_initial_research(self) -> None:
        """Advance a fresh CREATED workspace to INITIAL_RESEARCH at run start."""
        self._orchestrator.ensure_initial_research(self._workspace_id)

    def begin_investigation(self) -> None:
        """Start the next research turn after an explicit focus selection."""
        self._orchestrator.begin_investigation(self._workspace_id)

    def invoke(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        if tool_name not in self.capability_names:
            raise WorkspaceToolError("workspace tool is not allowed")
        if tool_name == "read_workspace_state":
            return self.read_workspace_state()
        if tool_name == "update_subquestions":
            return self.update_subquestions(
                str(arguments["operation"]), str(arguments["question_id"]),
                text=_as_str(arguments.get("text")), answer=_as_str(arguments.get("answer")),
                researchability=_as_str(arguments.get("researchability")),
            )
        if tool_name == "add_evidence":
            block_ids = arguments["block_ids"]
            if not isinstance(block_ids, list) or not all(isinstance(x, str) for x in block_ids):
                raise WorkspaceToolError("block_ids must be a list of full ids")
            return self.add_evidence(
                str(arguments["evidence_id"]), str(arguments["source_id"]), block_ids,
                supports_question_ids=_as_str_list(arguments.get("supports_question_ids")),
                evidence_role=str(arguments.get("evidence_role", "supporting")),
                claim=str(arguments.get("claim", "")), research_interpretation=str(arguments.get("research_interpretation", "")),
                confidence=str(arguments.get("confidence", "medium")),
            )
        if tool_name == "update_research_map":
            return self.update_research_map(
                str(arguments["node_id"]),
                label=_as_str(arguments.get("label")),
                related_question_ids=_as_str_list(arguments.get("related_question_ids")),
                evidence_ids=_as_str_list(arguments.get("evidence_ids")),
            )
        if tool_name == "update_research_plan":
            plan = arguments.get("plan")
            if not isinstance(plan, Mapping):
                raise WorkspaceToolError("plan must be an object")
            return self.update_research_plan(plan)
        if tool_name == "import_supporting_paper":
            return self.import_supporting_paper(arguments)
        raise WorkspaceToolError("unknown workspace tool")


def _normalize_operation(operation: str) -> str:
    normalized = operation.strip().lower()
    allowed = {"add", "update", "resolve", "deprioritize"}
    if normalized not in allowed:
        raise WorkspaceToolError(f"operation must be one of {sorted(allowed)}")
    return normalized


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkspaceToolError("expected a list of ids")
    return value


def _research_plan_from_input(payload: Mapping[str, Any], current: ResearchPlan) -> ResearchPlan:
    """Parse an agent-facing mapping into the typed plan aggregate.

    Parsing is deliberately strict about container shapes and enum values; the
    gate remains the final authority for cross-object references and uniqueness.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("plan must be an object")
    # Artifact maturity is controlled by the Harness/product lifecycle, not by
    # a model write.  Keeping it out of this allow-list prevents an agent from
    # prematurely presenting a stage note as a final research report.
    allowed_keys = {"stage", "iterations", "method_map", "hypotheses", "critiques", "experiment_plans"}
    unknown_keys = set(payload) - allowed_keys
    if unknown_keys:
        raise ValueError(f"plan contains unsupported fields: {sorted(str(key) for key in unknown_keys)}")
    if not payload:
        raise ValueError("plan must include at least one section")

    stage = current.stage
    if "stage" in payload:
        stage = _plan_enum(ResearchPlanStage, payload.get("stage"), "stage")

    iterations = current.iterations
    if "iterations" in payload:
        raw_items = _list_value(payload.get("iterations"), "iterations")
        iterations = tuple(_research_iteration(item, index + 1) for index, item in enumerate(raw_items))
        # Iteration history is append-only from the agent's perspective.  A
        # full plan replacement is still atomic, but an update that silently
        # drops a previous round would make the research lineage disappear
        # from the workspace.  Force callers to include the existing rounds
        # and append the new one explicitly instead.
        existing_ids = {item.iteration_id for item in current.iterations}
        provided_ids = {item.iteration_id for item in iterations}
        missing_ids = sorted(existing_ids - provided_ids)
        if missing_ids:
            raise ValueError(
                "iterations must preserve existing history; missing iteration ids: "
                + ", ".join(missing_ids)
            )

    method_map = current.method_map
    if "method_map" in payload:
        raw_map = payload.get("method_map")
        if raw_map is None:
            method_map = None
        elif isinstance(raw_map, Mapping):
            raw_entries = raw_map.get("entries", ())
            if not isinstance(raw_entries, (list, tuple)):
                raise ValueError("method_map.entries must be a list")
            entries = tuple(_method_map_entry(item) for item in raw_entries)
            method_map = MethodMap(
                map_id=_text_value(raw_map.get("map_id"), "method_map.map_id", "METHOD-MAP"),
                status=_plan_enum(
                    PlanArtifactStatus,
                    raw_map.get("status", PlanArtifactStatus.DRAFT.value),
                    "method_map.status",
                ),
                entries=entries,
            )
        else:
            raise ValueError("method_map must be an object or null")

    hypotheses = current.hypotheses
    if "hypotheses" in payload:
        raw_items = _list_value(payload.get("hypotheses"), "hypotheses")
        hypotheses = tuple(_candidate_hypothesis(item) for item in raw_items)

    critiques = current.critiques
    if "critiques" in payload:
        raw_items = _list_value(payload.get("critiques"), "critiques")
        critiques = tuple(_critique(item) for item in raw_items)

    experiment_plans = current.experiment_plans
    if "experiment_plans" in payload:
        raw_items = _list_value(payload.get("experiment_plans"), "experiment_plans")
        experiment_plans = tuple(_experiment_plan(item) for item in raw_items)

    return ResearchPlan(
        stage=stage,
        artifact_stage=current.artifact_stage,
        method_map=method_map,
        hypotheses=hypotheses,
        critiques=critiques,
        experiment_plans=experiment_plans,
        iterations=iterations,
    )


def _list_value(value: Any, label: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list")
    return list(value)


def _object_value(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = _list_value(value, label)
    if not all(isinstance(item, str) for item in values):
        raise ValueError(f"{label} must contain strings")
    return tuple(item.strip() for item in values)


def _text_value(value: Any, label: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _plan_enum(enum_type, value: Any, label: str):
    try:
        return enum_type(str(value).strip().lower())
    except (TypeError, ValueError) as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{label} must be one of {allowed}") from error


def _method_map_entry(value: Any) -> MethodMapEntry:
    item = _object_value(value, "method_map entry")
    return MethodMapEntry(
        method_id=_text_value(item.get("method_id"), "method_map entry.method_id"),
        name=_text_value(item.get("name"), "method_map entry.name"),
        mechanism=_text_value(item.get("mechanism"), "method_map entry.mechanism"),
        assumptions=_text_value(item.get("assumptions"), "method_map entry.assumptions"),
        evidence_ids=_string_tuple(item.get("evidence_ids"), "method_map entry.evidence_ids"),
        limitations=_text_value(item.get("limitations"), "method_map entry.limitations"),
    )


def _candidate_hypothesis(value: Any) -> CandidateHypothesis:
    item = _object_value(value, "hypothesis")
    question_id = item.get("question_id")
    return CandidateHypothesis(
        hypothesis_id=_text_value(item.get("hypothesis_id"), "hypothesis.hypothesis_id"),
        text=_text_value(item.get("text"), "hypothesis.text"),
        question_id=None if question_id is None else _text_value(question_id, "hypothesis.question_id"),
        evidence_ids=_string_tuple(item.get("evidence_ids"), "hypothesis.evidence_ids"),
        rationale=_text_value(item.get("rationale"), "hypothesis.rationale"),
        falsifiers=_string_tuple(item.get("falsifiers"), "hypothesis.falsifiers"),
        status=_plan_enum(
            HypothesisStatus,
            item.get("status", HypothesisStatus.CANDIDATE.value),
            "hypothesis.status",
        ),
    )


def _critique(value: Any) -> Critique:
    item = _object_value(value, "critique")
    return Critique(
        critique_id=_text_value(item.get("critique_id"), "critique.critique_id"),
        hypothesis_id=_text_value(item.get("hypothesis_id"), "critique.hypothesis_id"),
        strengths=_string_tuple(item.get("strengths"), "critique.strengths"),
        risks=_string_tuple(item.get("risks"), "critique.risks"),
        alternatives=_string_tuple(item.get("alternatives"), "critique.alternatives"),
        evidence_ids=_string_tuple(item.get("evidence_ids"), "critique.evidence_ids"),
        confidence=_text_value(item.get("confidence"), "critique.confidence", "medium"),
        status=_plan_enum(
            PlanArtifactStatus,
            item.get("status", PlanArtifactStatus.DRAFT.value),
            "critique.status",
        ),
    )


def _experiment_plan(value: Any) -> ExperimentPlan:
    item = _object_value(value, "experiment plan")
    hypothesis_id = item.get("hypothesis_id")
    return ExperimentPlan(
        plan_id=_text_value(item.get("plan_id"), "experiment_plan.plan_id"),
        hypothesis_id=None if hypothesis_id is None else _text_value(hypothesis_id, "experiment_plan.hypothesis_id"),
        status=_plan_enum(
            PlanArtifactStatus,
            item.get("status", PlanArtifactStatus.DRAFT.value),
            "experiment_plan.status",
        ),
        intervention=_text_value(item.get("intervention"), "experiment_plan.intervention"),
        baselines=_string_tuple(item.get("baselines"), "experiment_plan.baselines"),
        datasets=_string_tuple(item.get("datasets"), "experiment_plan.datasets"),
        metrics=_string_tuple(item.get("metrics"), "experiment_plan.metrics"),
        ablations=_string_tuple(item.get("ablations"), "experiment_plan.ablations"),
        expected_outcomes=_text_value(item.get("expected_outcomes"), "experiment_plan.expected_outcomes"),
        decision_criteria=_text_value(item.get("decision_criteria"), "experiment_plan.decision_criteria"),
        resource_estimate=_text_value(item.get("resource_estimate"), "experiment_plan.resource_estimate"),
        risks=_string_tuple(item.get("risks"), "experiment_plan.risks"),
    )


def _research_iteration(value: Any, fallback_sequence: int) -> ResearchIteration:
    item = _object_value(value, "research iteration")
    sequence_value = item.get("sequence", fallback_sequence)
    try:
        sequence = int(sequence_value)
    except (TypeError, ValueError) as error:
        raise ValueError("research iteration.sequence must be an integer") from error
    return ResearchIteration(
        iteration_id=_text_value(item.get("iteration_id"), "research iteration.iteration_id"),
        sequence=sequence,
        title=_text_value(item.get("title"), "research iteration.title"),
        status=_plan_enum(
            ResearchIterationStatus,
            item.get("status", ResearchIterationStatus.IN_PROGRESS.value),
            "research iteration.status",
        ),
        focus_question_id=(
            None
            if item.get("focus_question_id") is None
            else _text_value(item.get("focus_question_id"), "research iteration.focus_question_id")
        ),
        run_id=(None if item.get("run_id") is None else _text_value(item.get("run_id"), "research iteration.run_id")),
        summary=_text_value(item.get("summary"), "research iteration.summary"),
        evidence_ids=_string_tuple(item.get("evidence_ids"), "research iteration.evidence_ids"),
        candidate_question_ids=_string_tuple(item.get("candidate_question_ids"), "research iteration.candidate_question_ids"),
        decision=_text_value(item.get("decision"), "research iteration.decision"),
        next_step=_text_value(item.get("next_step"), "research iteration.next_step"),
    )


def _first(predicate, items):
    for item in items:
        if predicate(item):
            return item
    return None


_OPERATION_LABELS = {
    SUBQUESTION_ADD: "新增子问题",
    SUBQUESTION_UPDATE: "更新子问题文本",
    SUBQUESTION_RESOLVE: "把子问题标记为已解决",
    SUBQUESTION_DEPRIORITIZE: "把子问题降级",
    EVIDENCE_ADD: "新增证据引用",
    RESEARCH_MAP_ADD: "新增研究地图节点",
    RESEARCH_MAP_UPDATE: "更新研究地图节点",
    RESEARCH_PLAN_REPLACE: "更新研究计划",
    RESEARCH_ITERATION_APPEND: "记录研究迭代",
    RESEARCH_ITERATION_UPDATE: "更新研究迭代回执",
}


def _describe_patch_operations(operations: tuple[WorkspacePatchOperation, ...]) -> str:
    parts = []
    for op in operations:
        label = _OPERATION_LABELS.get(op.kind, op.kind)
        parts.append(f"{label}（{op.object_id}）")
    return "；".join(parts)
