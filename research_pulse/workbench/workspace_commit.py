"""Atomic commit for a validated research-workspace patch (M3).

``CommitService`` is the persistence side of propose → Gate → risk → commit: it
takes an already-Gate-validated patch, writes every canonical file it touches in
one atomic batch, and bumps the global ``workspace_revision`` exactly once. The
service performs no research judgement — risk routing (auto/review/hitl) happens
elsewhere and decides whether a patch reaches this commit at all.

The gate's optimistic-lock check means a stale patch (based on an old revision)
is rejected before anything is written, so a concurrent writer that advanced the
workspace cannot be silently overwritten.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
import hashlib
import json
from threading import RLock

from research_pulse.workbench.workspace import ProjectedWorkspaceState, WorkspaceRepository
from research_pulse.workbench.workspace_gate import WorkspaceGate, WorkspacePatch


class WorkspaceCommitError(ValueError):
    """Raised when a commit cannot be applied (e.g. revision conflict)."""


_DEFAULT_NOW: Callable[[], datetime] = lambda: datetime.now(UTC)


class CommitService:
    """Validate a patch and persist it atomically, bumping the global revision."""

    def __init__(self, repository: WorkspaceRepository, *, gate: WorkspaceGate | None = None, clock: Callable[[], datetime] = _DEFAULT_NOW) -> None:
        self._repository = repository
        self._gate = gate or WorkspaceGate(repository, clock=clock)
        self._committed: dict[str, ProjectedWorkspaceState] = {}
        self._commit_lock = RLock()

    @property
    def gate(self) -> WorkspaceGate:
        return self._gate

    def commit(self, workspace_id: str, patch: WorkspacePatch) -> ProjectedWorkspaceState:
        """Validate ``patch`` and atomically write the projected canonical state.

        Returns the projected state (including the new revision) on success. If
        the gate rejects the patch — a stale base revision, an integrity rule
        violation, or missing provenance — nothing is written and the
        ``WorkspaceGateError`` / ``WorkspacePatchError`` propagates.
        """
        # Gate validation and canonical commit must share one ownership boundary.
        # Otherwise two patches based on the same revision can both validate and
        # silently overwrite one another before either observes the new revision.
        with self._commit_lock:
            payload = {
                "workspace_id": workspace_id,
                "base_revision": patch.base_workspace_revision,
                "provenance": patch.provenance,
                "operations": [op.__dict__ for op in patch.operations],
            }
            operation_id = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            if operation_id in self._committed:
                return self._committed[operation_id]
            persisted = getattr(self._repository, "get_commit_receipt", lambda *_: None)(workspace_id, operation_id)
            if persisted is not None:
                self._committed[operation_id] = persisted
                return persisted
            projected = self._gate.validate(workspace_id, patch)
            self._repository.apply_commit(workspace_id, projected)
            saver = getattr(self._repository, "save_commit_receipt", None)
            if saver is not None:
                saver(workspace_id, operation_id, operation_id, projected)
            self._committed[operation_id] = projected
            return projected
