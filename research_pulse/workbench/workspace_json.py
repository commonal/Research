"""Canonical-JSON persistence for the Research Assistant Workspace.

The workspace directory holds structured JSON as the single source of truth:

    data/workbench/workspaces/<workspace_id>/
        workspace.json          # research_intent, active_focus_id, anchor_paper_id, status
        research-map.json       # ResearchMapNode[]
        subquestions.json       # SubQuestion[]
        research-plan.json      # MethodMap / Hypothesis / Critique / ExperimentPlan / Iteration history
        evidence/<id>.json      # one file per Evidence (never the paper body)
        artifacts/*.md          # user-facing projections; never a write source

Markdown and the progress snapshot are derived projections built elsewhere; this
store never writes paper body text into the workspace. Reads/writes are small and
serialized so a single-process workbench stays consistent.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from research_pulse.workbench.workspace import (
    CandidateHypothesis,
    Critique,
    Evidence,
    ExperimentPlan,
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
    Researchability,
    SubQuestion,
    SubQuestionStatus,
    Workspace,
    WorkspaceNotFoundError,
    WorkspaceStatus,
)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class WorkspaceJsonStore:
    """Minimal file-backed repository implementing the WorkspaceRepository seam."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _workspace_dir(self, workspace_id: str) -> Path:
        return self.root / workspace_id

    def _workspace_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "workspace.json"

    def _subquestions_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "subquestions.json"

    def _evidence_dir(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "evidence"

    def _research_plan_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "research-plan.json"


    def _create_dir(self, workspace_id: str) -> Path:
        directory = self._workspace_dir(workspace_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # -- Workspace lifecycle --

    def create(self, workspace: Workspace) -> None:
        with self._lock:
            directory = self._create_dir(workspace.workspace_id)
            (directory / "workspace.json").write_text(
                _dumps(self._workspace_payload(workspace)), encoding="utf-8"
            )
            (directory / "research-map.json").write_text(
                _dumps({"nodes": []}), encoding="utf-8"
            )
            (directory / "subquestions.json").write_text(
                _dumps({"items": []}), encoding="utf-8"
            )
            (directory / "research-plan.json").write_text(
                _dumps(_research_plan_payload(workspace.research_plan)), encoding="utf-8"
            )
            (directory / "evidence").mkdir(exist_ok=True)

    def get(self, workspace_id: str) -> Workspace | None:
        with self._lock:
            workspace_file = self._workspace_file(workspace_id)
            if not workspace_file.exists():
                return None
            payload = json.loads(workspace_file.read_text(encoding="utf-8"))
            plan_file = self._research_plan_file(workspace_id)
            plan_payload = (
                json.loads(plan_file.read_text(encoding="utf-8"))
                if plan_file.exists()
                else None
            )
            return self._workspace_from_payload(workspace_id, payload, plan_payload)

    def list(self) -> tuple[Workspace, ...]:
        with self._lock:
            if not self.root.exists():
                return ()
            workspaces = []
            for child in self.root.iterdir():
                if not child.is_dir():
                    continue
                workspace = self.get(child.name)
                if workspace is not None:
                    workspaces.append(workspace)
            return tuple(
                sorted(
                    workspaces,
                    key=lambda w: (w.created_at is not None, w.created_at or w.workspace_id),
                    reverse=True,
                )
            )

    def save(self, workspace: Workspace) -> None:
        with self._lock:
            directory = self._create_dir(workspace.workspace_id)
            (directory / "workspace.json").write_text(
                _dumps(self._workspace_payload(workspace)), encoding="utf-8"
            )
            (directory / "research-plan.json").write_text(
                _dumps(_research_plan_payload(workspace.research_plan)), encoding="utf-8"
            )

    def apply_commit(self, workspace_id: str, projected: ProjectedWorkspaceState) -> None:
        """Atomically persist a post-patch canonical state under one lock.

        The global ``workspace_revision`` from the projected state is written to
        ``workspace.json`` last, inside the same lock acquisition as the data
        files. A concurrent reader that loads workspace.json sees either the old
        revision + old files (before any write), or the new revision + new files
        (after all writes) — never a half-applied torn mix across files, because
        the JSON store serializes read/write on one RLock and the data files are
        rewritten before the revision bump.
        """
        with self._lock:
            current = self.get(workspace_id)
            if current is None:
                raise WorkspaceNotFoundError(workspace_id)
            directory = self._create_dir(workspace_id)
            # Preserve the workspace meta; only the global revision moves.
            updated = Workspace(
                workspace_id=current.workspace_id,
                research_question=current.research_question,
                anchor_paper_id=current.anchor_paper_id,
                research_intent=current.research_intent,
                active_focus_id=current.active_focus_id,
                status=current.status,
                workspace_revision=projected.workspace_revision,
                created_at=current.created_at,
                research_map=tuple(projected.research_map),
                subquestions=tuple(projected.subquestions),
                evidence=tuple(projected.evidence),
                research_plan=(
                    projected.research_plan
                    if projected.research_plan is not None
                    else current.research_plan
                ),
            )
            (directory / "research-plan.json").write_text(
                _dumps(_research_plan_payload(updated.research_plan)), encoding="utf-8"
            )
            (directory / "subquestions.json").write_text(
                _dumps({"items": [_subquestion_payload(q) for q in projected.subquestions]}),
                encoding="utf-8",
            )
            (directory / "research-map.json").write_text(
                _dumps({"nodes": [_research_node_payload(n) for n in projected.research_map]}),
                encoding="utf-8",
            )
            evidence_dir = directory / "evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            # Rewrite the whole evidence set so removed evidence never lingers.
            for existing in evidence_dir.glob("*.json"):
                existing.unlink()
            for evidence in projected.evidence:
                safe_name = "".join(
                    ch for ch in evidence.evidence_id if ch.isalnum() or ch in "-_"
                ) or evidence.evidence_id
                (evidence_dir / f"{safe_name}.json").write_text(
                    _dumps(_evidence_payload(evidence)), encoding="utf-8"
                )
            # Publish the revision marker last: readers that observe the new
            # revision can therefore assume every projected file is present.
            (directory / "workspace.json").write_text(
                _dumps(self._workspace_payload(updated)), encoding="utf-8"
            )

    def get_commit_receipt(self, workspace_id: str, operation_id: str):
        with self._lock:
            path = self._workspace_dir(workspace_id) / "commit-receipts.json"
            if not path.exists(): return None
            item = json.loads(path.read_text(encoding="utf-8")).get(operation_id)
            if not item: return None
            state = item["projected_state"]
            return ProjectedWorkspaceState(
                workspace_revision=int(state["workspace_revision"]),
                subquestions=tuple(_subquestion_from_payload(x) for x in state.get("subquestions", [])),
                evidence=tuple(_evidence_from_payload(x) for x in state.get("evidence", [])),
                research_map=tuple(_research_node_from_payload(x) for x in state.get("research_map", [])),
                research_plan=(
                    _research_plan_from_payload(state["research_plan"])
                    if state.get("research_plan") is not None
                    else None
                ),
            )

    def save_commit_receipt(self, workspace_id: str, operation_id: str, patch_digest: str, projected: ProjectedWorkspaceState) -> None:
        with self._lock:
            path = self._workspace_dir(workspace_id) / "commit-receipts.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            data[operation_id] = {"patch_digest": patch_digest, "projected_state": {
                "workspace_revision": projected.workspace_revision,
                "subquestions": [_subquestion_payload(x) for x in projected.subquestions],
                "evidence": [_evidence_payload(x) for x in projected.evidence],
                "research_map": [_research_node_payload(x) for x in projected.research_map],
                "research_plan": (
                    _research_plan_payload(projected.research_plan)
                    if projected.research_plan is not None
                    else None
                ),
            }}
            path.write_text(_dumps(data), encoding="utf-8")


    def _workspace_payload(self, workspace: Workspace) -> dict[str, Any]:
        return {
            "workspace_id": workspace.workspace_id,
            "research_question": workspace.research_question,
            "research_intent": workspace.research_intent,
            "active_focus_id": workspace.active_focus_id,
            "anchor_paper_id": workspace.anchor_paper_id,
            "status": workspace.status.value,
            "workspace_revision": workspace.workspace_revision,
            "created_at": (
                workspace.created_at.isoformat() if workspace.created_at else None
            ),
        }

    def _workspace_from_payload(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        plan_payload: dict[str, Any] | None = None,
    ) -> Workspace:
        created_at = payload.get("created_at")
        return Workspace(
            workspace_id=workspace_id,
            research_question=payload.get("research_question", ""),
            anchor_paper_id=payload.get("anchor_paper_id", ""),
            research_intent=payload.get("research_intent") or payload.get("research_question") or None,
            active_focus_id=payload.get("active_focus_id"),
            status=WorkspaceStatus(payload.get("status", "created")),
            workspace_revision=int(payload.get("workspace_revision", 1)),
            created_at=_parse_datetime(created_at),
            research_plan=(
                _research_plan_from_payload(plan_payload)
                if plan_payload is not None
                else ResearchPlan()
            ),
        )

    # -- SubQuestions --

    def list_subquestions(self, workspace_id: str) -> tuple[SubQuestion, ...]:
        with self._lock:
            path = self._subquestions_file(workspace_id)
            if not path.exists():
                return ()
            payload = json.loads(path.read_text(encoding="utf-8"))
            return tuple(_subquestion_from_payload(item) for item in payload.get("items", []))

    def get_subquestion(self, workspace_id: str, question_id: str) -> SubQuestion | None:
        for question in self.list_subquestions(workspace_id):
            if question.question_id == question_id:
                return question
        return None

    def upsert_subquestion(self, workspace_id: str, question: SubQuestion) -> None:
        with self._lock:
            path = self._subquestions_file(workspace_id)
            items: list[dict[str, Any]] = []
            if path.exists():
                items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
            replaced = False
            for index, existing in enumerate(items):
                if existing.get("question_id") == question.question_id:
                    items[index] = _subquestion_payload(question)
                    replaced = True
                    break
            if not replaced:
                items.append(_subquestion_payload(question))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dumps({"items": items}), encoding="utf-8")

    # -- Evidence --

    def list_evidence(self, workspace_id: str) -> tuple[Evidence, ...]:
        with self._lock:
            evidence_dir = self._evidence_dir(workspace_id)
            if not evidence_dir.exists():
                return ()
            results = []
            for path in sorted(evidence_dir.glob("*.json")):
                payload = json.loads(path.read_text(encoding="utf-8"))
                results.append(_evidence_from_payload(payload))
            return tuple(results)

    def upsert_evidence(self, workspace_id: str, evidence: Evidence) -> None:
        with self._lock:
            evidence_dir = self._evidence_dir(workspace_id)
            evidence_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(
                ch for ch in evidence.evidence_id if ch.isalnum() or ch in "-_"
            ) or evidence.evidence_id
            (evidence_dir / f"{safe_name}.json").write_text(
                _dumps(_evidence_payload(evidence)), encoding="utf-8"
            )

    # -- Research Map --

    def _research_map_file(self, workspace_id: str) -> Path:
        return self._workspace_dir(workspace_id) / "research-map.json"

    def list_research_map(self, workspace_id: str) -> tuple[ResearchMapNode, ...]:
        with self._lock:
            path = self._research_map_file(workspace_id)
            if not path.exists():
                return ()
            payload = json.loads(path.read_text(encoding="utf-8"))
            return tuple(_research_node_from_payload(item) for item in payload.get("nodes", []))

    def get_research_map_node(self, workspace_id: str, node_id: str) -> ResearchMapNode | None:
        for node in self.list_research_map(workspace_id):
            if node.node_id == node_id:
                return node
        return None

    def upsert_research_map_node(self, workspace_id: str, node: ResearchMapNode) -> None:
        with self._lock:
            path = self._research_map_file(workspace_id)
            nodes: list[dict[str, Any]] = []
            if path.exists():
                nodes = json.loads(path.read_text(encoding="utf-8")).get("nodes", [])
            replaced = False
            for index, existing in enumerate(nodes):
                if existing.get("node_id") == node.node_id:
                    nodes[index] = _research_node_payload(node)
                    replaced = True
                    break
            if not replaced:
                nodes.append(_research_node_payload(node))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dumps({"nodes": nodes}), encoding="utf-8")


def _subquestion_payload(question: SubQuestion) -> dict[str, Any]:
    return {
        "question_id": question.question_id,
        "text": question.text,
        "status": question.status.value,
        "answer": question.answer,
        "researchability": question.researchability.value,
    }


def _subquestion_from_payload(payload: dict[str, Any]) -> SubQuestion:
    return SubQuestion(
        question_id=payload.get("question_id", ""),
        text=payload.get("text", ""),
        status=SubQuestionStatus(payload.get("status", "open")),
        answer=payload.get("answer"),
        researchability=Researchability(payload.get("researchability") or "candidate"),
    )


def _evidence_payload(evidence: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "source_id": evidence.source_id,
        "block_ids": list(evidence.block_ids),
        "supports_question_ids": list(evidence.supports_question_ids),
        "evidence_role": evidence.evidence_role,
        "claim": evidence.claim,
        "research_interpretation": evidence.research_interpretation,
        "confidence": evidence.confidence,
        "added_at": evidence.added_at.isoformat() if evidence.added_at else None,
    }


def _research_node_payload(node: ResearchMapNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "related_question_ids": list(node.related_question_ids),
        "evidence_ids": list(node.evidence_ids),
        "label": node.label,
    }


def _research_node_from_payload(payload: dict[str, Any]) -> ResearchMapNode:
    return ResearchMapNode(
        node_id=payload.get("node_id", ""),
        related_question_ids=tuple(payload.get("related_question_ids", ())),
        evidence_ids=tuple(payload.get("evidence_ids", ())),
        label=payload.get("label", ""),
    )


def _research_plan_payload(plan: ResearchPlan) -> dict[str, Any]:
    method_map = plan.method_map
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
                "map_id": method_map.map_id,
                "status": method_map.status.value,
                "entries": [
                    {
                        "method_id": entry.method_id,
                        "name": entry.name,
                        "mechanism": entry.mechanism,
                        "assumptions": entry.assumptions,
                        "evidence_ids": list(entry.evidence_ids),
                        "limitations": entry.limitations,
                    }
                    for entry in method_map.entries
                ],
            }
            if method_map is not None
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


def _plan_status(value: Any) -> PlanArtifactStatus:
    try:
        return PlanArtifactStatus(value or PlanArtifactStatus.DRAFT.value)
    except ValueError:
        return PlanArtifactStatus.DRAFT


def _hypothesis_status(value: Any) -> HypothesisStatus:
    try:
        return HypothesisStatus(value or HypothesisStatus.CANDIDATE.value)
    except ValueError:
        return HypothesisStatus.CANDIDATE


def _research_plan_from_payload(payload: dict[str, Any]) -> ResearchPlan:
    if not isinstance(payload, dict):
        return ResearchPlan()
    try:
        stage = ResearchPlanStage(payload.get("stage", ResearchPlanStage.NOT_STARTED.value))
    except ValueError:
        stage = ResearchPlanStage.NOT_STARTED
    try:
        artifact_stage = ResearchArtifactStage(
            payload.get("artifact_stage", ResearchArtifactStage.OVERVIEW.value)
        )
    except ValueError:
        # Older workspaces have no artifact maturity field.  They are still
        # valid and start at the conservative overview stage.
        artifact_stage = ResearchArtifactStage.OVERVIEW
    method_payload = payload.get("method_map")
    method_map = None
    if isinstance(method_payload, dict):
        method_map = MethodMap(
            map_id=method_payload.get("map_id", "METHOD-MAP"),
            status=_plan_status(method_payload.get("status")),
            entries=tuple(
                MethodMapEntry(
                    method_id=item.get("method_id", ""),
                    name=item.get("name", ""),
                    mechanism=item.get("mechanism", ""),
                    assumptions=item.get("assumptions", ""),
                    evidence_ids=tuple(item.get("evidence_ids", ())),
                    limitations=item.get("limitations", ""),
                )
                for item in method_payload.get("entries", [])
                if isinstance(item, dict)
            ),
        )
    iterations = tuple(
        ResearchIteration(
            iteration_id=item.get("iteration_id", ""),
            sequence=int(item.get("sequence", index + 1)),
            title=item.get("title", ""),
            status=_iteration_status(item.get("status")),
            focus_question_id=item.get("focus_question_id"),
            run_id=item.get("run_id"),
            summary=item.get("summary", ""),
            evidence_ids=tuple(item.get("evidence_ids", ())),
            candidate_question_ids=tuple(item.get("candidate_question_ids", ())),
            decision=item.get("decision", ""),
            next_step=item.get("next_step", ""),
        )
        for index, item in enumerate(payload.get("iterations", []))
        if isinstance(item, dict)
    )
    hypotheses = tuple(
        CandidateHypothesis(
            hypothesis_id=item.get("hypothesis_id", ""),
            text=item.get("text", ""),
            question_id=item.get("question_id"),
            evidence_ids=tuple(item.get("evidence_ids", ())),
            rationale=item.get("rationale", ""),
            falsifiers=tuple(item.get("falsifiers", ())),
            status=_hypothesis_status(item.get("status")),
        )
        for item in payload.get("hypotheses", [])
        if isinstance(item, dict)
    )
    critiques = tuple(
        Critique(
            critique_id=item.get("critique_id", ""),
            hypothesis_id=item.get("hypothesis_id", ""),
            strengths=tuple(item.get("strengths", ())),
            risks=tuple(item.get("risks", ())),
            alternatives=tuple(item.get("alternatives", ())),
            evidence_ids=tuple(item.get("evidence_ids", ())),
            confidence=item.get("confidence", "medium"),
            status=_plan_status(item.get("status")),
        )
        for item in payload.get("critiques", [])
        if isinstance(item, dict)
    )
    experiment_plans = tuple(
        ExperimentPlan(
            plan_id=item.get("plan_id", ""),
            hypothesis_id=item.get("hypothesis_id"),
            status=_plan_status(item.get("status")),
            intervention=item.get("intervention", ""),
            baselines=tuple(item.get("baselines", ())),
            datasets=tuple(item.get("datasets", ())),
            metrics=tuple(item.get("metrics", ())),
            ablations=tuple(item.get("ablations", ())),
            expected_outcomes=item.get("expected_outcomes", ""),
            decision_criteria=item.get("decision_criteria", ""),
            resource_estimate=item.get("resource_estimate", ""),
            risks=tuple(item.get("risks", ())),
        )
        for item in payload.get("experiment_plans", [])
        if isinstance(item, dict)
    )
    return ResearchPlan(
        stage=stage,
        artifact_stage=artifact_stage,
        method_map=method_map,
        hypotheses=hypotheses,
        critiques=critiques,
        experiment_plans=experiment_plans,
        iterations=iterations,
    )


def _iteration_status(value: Any) -> ResearchIterationStatus:
    try:
        return ResearchIterationStatus(value or ResearchIterationStatus.IN_PROGRESS.value)
    except ValueError:
        return ResearchIterationStatus.IN_PROGRESS


def _evidence_from_payload(payload: dict[str, Any]) -> Evidence:
    return Evidence(
        evidence_id=payload.get("evidence_id", ""),
        source_id=payload.get("source_id", ""),
        block_ids=tuple(payload.get("block_ids", ())),
        supports_question_ids=tuple(payload.get("supports_question_ids", ())),
        evidence_role=payload.get("evidence_role", "supporting"),
        claim=payload.get("claim", ""),
        research_interpretation=payload.get("research_interpretation", ""),
        confidence=payload.get("confidence", "medium"),
        added_at=_parse_datetime(payload.get("added_at")),
    )


def _parse_datetime(value: str | None):
    if not value:
        return None
    from datetime import datetime

    return datetime.fromisoformat(value)
