"""M3.6 — human-in-the-loop decisions: DecisionPoint + HITLService.

A HITL decision records a point where the Harness stops for the user and the
action it would take; the user's resolution (approve / reject / choose a
direction) drives a follow-up Run that re-validates ``workspace_revision``.

Two ``DecisionKind`` values unify under the single workspace pause state
``WAITING_FOR_USER_ACTION`` (see the state machine in the workspace model):
- ``research_direction`` — the agent reached an initial understanding, or a
  later bounded round produced new candidate questions, and must ask which
  direction to pursue next.
- ``patch_approval`` — a canonical patch classified ``hitl`` (semantic reversal /
  direction change) that must be approved before commit.

``HITLService`` persists decision points; it never gates, classifies or commits
(those are ``WorkspaceGate`` / ``WorkspaceRiskClassifier`` / ``CommitService``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from json import dumps, loads
from pathlib import Path
from threading import RLock
from typing import Callable, Protocol
from uuid import uuid4

from research_pulse.workbench.workspace_gate import WorkspacePatch


class DecisionKind(StrEnum):
    RESEARCH_DIRECTION = "research_direction"
    PATCH_APPROVAL = "patch_approval"


class DecisionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DecisionPointError(ValueError):
    """Raised for an illegal decision-point mutation (e.g. resolving twice)."""


@dataclass(frozen=True)
class DecisionPoint:
    decision_id: str
    workspace_id: str
    run_id: str
    kind: DecisionKind
    prompt: str
    created_at: datetime | None = None
    status: DecisionStatus = DecisionStatus.PENDING
    proposed_patch: WorkspacePatch | None = None
    resolved_at: datetime | None = None
    resolved_run_id: str | None = None
    decision: str | None = None
    operation_id: str | None = None
    candidate_question_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.decision_id.strip() or not self.workspace_id.strip() or not self.run_id.strip():
            raise DecisionPointError("decision point requires a decision_id, workspace_id and run_id")
        if not self.prompt.strip():
            raise DecisionPointError("decision point requires a non-blank prompt")

    def resolve(
        self,
        *,
        decision: str,
        status: DecisionStatus,
        resolved_run_id: str,
        resolved_at: datetime,
    ) -> "DecisionPoint":
        if self.status != DecisionStatus.PENDING:
            raise DecisionPointError("decision point is already resolved")
        if status == DecisionStatus.PENDING:
            raise DecisionPointError("a resolution cannot keep the decision pending")
        return replace(
            self,
            status=status,
            decision=decision,
            resolved_run_id=resolved_run_id,
            resolved_at=resolved_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "kind": self.kind.value,
            "prompt": self.prompt,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_run_id": self.resolved_run_id,
            "decision": self.decision,
            "operation_id": self.operation_id,
            "candidate_question_ids": list(self.candidate_question_ids),
            "proposed_patch": (
                {
                    "base_workspace_revision": self.proposed_patch.base_workspace_revision,
                    "provenance": self.proposed_patch.provenance,
                    "operations": [_operation_dict(op) for op in self.proposed_patch.operations],
                }
                if self.proposed_patch is not None
                else None
            ),
        }


class DecisionPointRepository(Protocol):
    def put(self, decision: DecisionPoint) -> None: ...
    def get(self, decision_id: str) -> DecisionPoint | None: ...
    def list_for_workspace(self, workspace_id: str) -> tuple[DecisionPoint, ...]: ...


class DecisionPointJsonStore:
    """File-backed decision-point repository under a workspace directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = RLock()

    def _dir(self, workspace_id: str) -> Path:
        return self.root / workspace_id / "decision_points"

    def put(self, decision: DecisionPoint) -> None:
        with self._lock:
            directory = self._dir(decision.workspace_id)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{decision.decision_id}.json").write_text(
                dumps(decision.to_dict(), ensure_ascii=False), encoding="utf-8"
            )

    def get(self, decision_id: str) -> DecisionPoint | None:
        with self._lock:
            if not self.root.exists():
                return None
            for workspace_dir in self.root.iterdir():
                candidate = workspace_dir / "decision_points" / f"{decision_id}.json"
                if candidate.exists():
                    return _decision_from_dict(loads(candidate.read_text(encoding="utf-8")))
            return None

    def list_for_workspace(self, workspace_id: str) -> tuple[DecisionPoint, ...]:
        with self._lock:
            directory = self._dir(workspace_id)
            if not directory.exists():
                return ()
            decisions = []
            for filename in sorted(directory.glob("*.json")):
                decisions.append(_decision_from_dict(loads(filename.read_text(encoding="utf-8"))))
            return tuple(decisions)


def _operation_dict(operation) -> dict[str, object]:
    payload = {
        "kind": operation.kind,
        "object_id": operation.object_id,
    }
    for field_name in (
        "text", "answer", "source_id", "evidence_role", "claim",
        "research_interpretation", "confidence", "label",
        "researchability",
    ):
        value = getattr(operation, field_name)
        if value is not None:
            payload[field_name] = value
    for field_name in ("block_ids", "supports_question_ids", "related_question_ids", "evidence_ids"):
        value = getattr(operation, field_name)
        if value:
            payload[field_name] = list(value)
    if operation.research_plan is not None:
        from research_pulse.workbench.workspace_json import _research_plan_payload

        payload["research_plan"] = _research_plan_payload(operation.research_plan)
    if operation.research_iteration is not None:
        item = operation.research_iteration
        payload["research_iteration"] = {
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
    return payload


def _decision_from_dict(data: dict[str, object]) -> DecisionPoint:
    patch_data = data.get("proposed_patch")
    from research_pulse.workbench.workspace_gate import WorkspacePatch, WorkspacePatchOperation
    from research_pulse.workbench.workspace_json import _research_plan_from_payload
    from research_pulse.workbench.workspace import ResearchIteration, ResearchIterationStatus

    proposed_patch = None
    if patch_data:
        proposed_patch = WorkspacePatch(
            base_workspace_revision=int(patch_data["base_workspace_revision"]),
            provenance=str(patch_data.get("provenance", "")),
            operations=tuple(
                WorkspacePatchOperation(
                    kind=str(op["kind"]),
                    object_id=str(op["object_id"]),
                    text=op.get("text"),
                    answer=op.get("answer"),
                    source_id=op.get("source_id"),
                    block_ids=tuple(op.get("block_ids", ())),
                    supports_question_ids=tuple(op.get("supports_question_ids", ())),
                    evidence_role=op.get("evidence_role"),
                    claim=op.get("claim"),
                    research_interpretation=op.get("research_interpretation"),
                    confidence=op.get("confidence"),
                    label=op.get("label"),
                    researchability=op.get("researchability"),
                    related_question_ids=tuple(op.get("related_question_ids", ())),
                    evidence_ids=tuple(op.get("evidence_ids", ())),
                    research_plan=(
                        _research_plan_from_payload(op["research_plan"])
                        if op.get("research_plan") is not None
                        else None
                    ),
                    research_iteration=(
                        _research_iteration_from_dict(op["research_iteration"])
                        if op.get("research_iteration") is not None
                        else None
                    ),
                )
                for op in patch_data["operations"]
            ),
        )
    return DecisionPoint(
        decision_id=str(data["decision_id"]),
        workspace_id=str(data["workspace_id"]),
        run_id=str(data["run_id"]),
        kind=DecisionKind(data["kind"]),
        prompt=str(data["prompt"]),
        status=DecisionStatus(data["status"]),
        created_at=_parse_datetime(data.get("created_at")),
        resolved_at=_parse_datetime(data.get("resolved_at")),
        resolved_run_id=data.get("resolved_run_id"),
        decision=data.get("decision"),
        operation_id=data.get("operation_id"),
        candidate_question_ids=tuple(str(item) for item in data.get("candidate_question_ids", ())),
        proposed_patch=proposed_patch,
    )


def _research_iteration_from_dict(data: dict[str, object]) -> "ResearchIteration":
    from research_pulse.workbench.workspace import ResearchIteration, ResearchIterationStatus

    status_value = str(data.get("status") or ResearchIterationStatus.IN_PROGRESS.value)
    try:
        status = ResearchIterationStatus(status_value)
    except ValueError:
        status = ResearchIterationStatus.IN_PROGRESS
    return ResearchIteration(
        iteration_id=str(data.get("iteration_id") or ""),
        sequence=int(data.get("sequence") or 1),
        title=str(data.get("title") or ""),
        status=status,
        focus_question_id=(str(data["focus_question_id"]) if data.get("focus_question_id") is not None else None),
        run_id=(str(data["run_id"]) if data.get("run_id") is not None else None),
        summary=str(data.get("summary") or ""),
        evidence_ids=tuple(str(value) for value in data.get("evidence_ids", ()) or ()),
        candidate_question_ids=tuple(str(value) for value in data.get("candidate_question_ids", ()) or ()),
        decision=str(data.get("decision") or ""),
        next_step=str(data.get("next_step") or ""),
    )


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


class HITLService:
    def __init__(
        self,
        repository: DecisionPointRepository,
        *,
        decision_id_factory: Callable[[], str] = lambda: f"D-{_next_id()}",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.decision_id_factory = decision_id_factory
        self.clock = clock
        self._resolved_operations: dict[str, DecisionPoint] = {}

    def create_decision_point(
        self,
        *,
        workspace_id: str,
        run_id: str,
        kind: DecisionKind,
        prompt: str,
        proposed_patch: WorkspacePatch | None = None,
        candidate_question_ids: tuple[str, ...] = (),
    ) -> DecisionPoint:
        decision = DecisionPoint(
            decision_id=self._next_available_decision_id(),
            workspace_id=workspace_id,
            run_id=run_id,
            kind=kind,
            prompt=prompt,
            proposed_patch=proposed_patch,
            candidate_question_ids=candidate_question_ids,
            created_at=self.clock(),
        )
        self.repository.put(decision)
        return decision

    def _next_available_decision_id(self) -> str:
        """Return an id that is unique in the durable decision store.

        The default counter is intentionally process-local, while the
        decision repository survives worker restarts and development hot
        reloads.  Without a collision check, a reloaded process can create
        ``D-101`` again and overwrite an earlier decision file, breaking the
        direction history and making an old run appear to have a new pending
        decision.  Preserve deterministic custom factories used by tests, but
        advance numeric ids (or add a short suffix for opaque ids) whenever
        the proposed id is already persisted.
        """
        candidate = str(self.decision_id_factory()).strip()
        if not candidate:
            candidate = f"D-{uuid4().hex[:12]}"
        if self.repository.get(candidate) is None:
            return candidate

        prefix, separator, suffix = candidate.rpartition("-")
        if separator and suffix.isdigit():
            next_number = int(suffix) + 1
            while True:
                candidate = f"{prefix}-{next_number}"
                if self.repository.get(candidate) is None:
                    return candidate
                next_number += 1

        while True:
            candidate = f"{candidate}-{uuid4().hex[:8]}"
            if self.repository.get(candidate) is None:
                return candidate

    def get(self, decision_id: str) -> DecisionPoint | None:
        return self.repository.get(decision_id)

    def list_for_workspace(self, workspace_id: str) -> tuple[DecisionPoint, ...]:
        return self.repository.list_for_workspace(workspace_id)

    def resolve(
        self,
        decision_id: str,
        *,
        decision: str,
        approved: bool,
        resolved_run_id: str,
        operation_id: str | None = None,
    ) -> DecisionPoint:
        if operation_id and operation_id in self._resolved_operations:
            return self._resolved_operations[operation_id]
        existing = self.get(decision_id)
        if existing is None:
            raise DecisionPointError(f"decision point {decision_id} does not exist")
        if operation_id and existing.operation_id == operation_id:
            return existing
        status = DecisionStatus.APPROVED if approved else DecisionStatus.REJECTED
        resolved = existing.resolve(
            decision=decision,
            status=status,
            resolved_run_id=resolved_run_id,
            resolved_at=self.clock(),
        )
        if operation_id:
            resolved = replace(resolved, operation_id=operation_id)
        self.repository.put(resolved)
        if operation_id:
            self._resolved_operations[operation_id] = resolved
        return resolved


_counter = 100


def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter
