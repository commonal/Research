"""Workspace directory lifecycle for the research-assistant profile.

Two scopes (the V1 decoupling):
  * ``resolve(workspace_id)``  — the RESOURCE workspace folder, keyed by the
    real ``ResearchWorkspace.workspace_id`` (shared papers / notes / knowledge).
  * ``session_state_dir(...)`` — a per-session subfolder inside the workspace
    where the M3 research state (research_map / subquestions / evidence) and the
    assistant's freeform notes live. Sessions never share this (Session ≈
    ResearchRun), so one workspace can hold several isolated threads.

Directories are created lazily and cleaned up with their owner.
"""

from __future__ import annotations

from pathlib import Path
import shutil


WORKSPACE_SUBDIRS = ("sessions", "materials")
SESSION_STATE_SUBDIRS = ("evidence", "findings", "notes")


def workspace_root(data_root: Path) -> Path:
    return Path(data_root) / "workspaces"


class SessionWorkspaceManager:
    def __init__(self, data_root: str | Path) -> None:
        self._root = workspace_root(data_root)

    def resolve(self, workspace_id: str) -> str:
        """Return the resource workspace folder for a workspace, creating it lazily."""
        if not workspace_id.strip():
            raise ValueError("workspace id must not be blank")
        path = self._root / workspace_id
        path.mkdir(parents=True, exist_ok=True)
        for sub in WORKSPACE_SUBDIRS:
            (path / sub).mkdir(exist_ok=True)
        return str(path)

    def session_state_dir(self, workspace_id: str, session_id: str) -> str:
        """Return the per-session M3 state subfolder (isolated within the workspace)."""
        if not workspace_id.strip() or not session_id.strip():
            raise ValueError("workspace id and session id must not be blank")
        path = Path(self.resolve(workspace_id)) / "sessions" / session_id
        path.mkdir(parents=True, exist_ok=True)
        for sub in SESSION_STATE_SUBDIRS:
            (path / sub).mkdir(exist_ok=True)
        return str(path)

    def remove_workspace(self, workspace_id: str) -> None:
        if not workspace_id.strip():
            raise ValueError("workspace id must not be blank")
        path = self._root / workspace_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def remove_session(self, workspace_id: str, session_id: str) -> None:
        if not workspace_id.strip() or not session_id.strip():
            raise ValueError("workspace id and session id must not be blank")
        path = self._root / workspace_id / "sessions" / session_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
