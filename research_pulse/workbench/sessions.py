"""Public service seam for research-session lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable, Protocol
from uuid import uuid4


class SessionRepository(Protocol):
    def create(self, session: "ResearchSession") -> None: ...

    def get(self, session_id: str) -> "ResearchSession | None": ...

    def list(self, lifecycle: "SessionLifecycle | None" = None) -> tuple["ResearchSession", ...]: ...

    def link_paper(self, session_id: str, paper_id: str) -> None: ...

    def list_paper_ids(self, session_id: str) -> tuple[str, ...]: ...

    def save(self, session: "ResearchSession") -> None: ...

    def delete(self, session_id: str) -> None: ...


class SessionLifecycle(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class ResearchSession:
    session_id: str
    research_question: str | None
    title: str
    created_at: datetime
    lifecycle: SessionLifecycle = SessionLifecycle.ACTIVE
    paper_panel_open: bool = False
    active_paper_id: str | None = None
    workspace_id: str | None = None


@dataclass(frozen=True)
class ResearchWorkspace:
    """Resource-scope workspace: the long-lived folder that owns shared papers,
    notes and knowledge. In V1 a Workspace is the container for multiple Session
    threads; M3 research state (research_map/subquestions/evidence) stays at the
    future ResearchRun level and is NOT lifted here (avoid cross-question bleed)."""
    workspace_id: str
    title: str
    created_at: datetime


class SessionNotFoundError(LookupError):
    """Raised when a session does not exist at the public service seam."""


class PaperLimitError(ValueError):
    """Raised when V1 would attach a second active paper to a session."""


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        *,
        session_id_factory: Callable[[], str] = lambda: str(uuid4()),
        workspace_id_factory: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.session_id_factory = session_id_factory
        self.workspace_id_factory = workspace_id_factory
        self.clock = clock

    def _ensure_session_workspace(self, session: ResearchSession) -> None:
        ensure = getattr(self.repository, "ensure_workspace", None)
        if ensure and session.workspace_id:
            ensure(session.workspace_id)

    def create_empty(self, workspace_id: str | None = None) -> ResearchSession:
        session = ResearchSession(
            session_id=self.session_id_factory(),
            research_question=None,
            title="新会话",
            created_at=self.clock(),
            workspace_id=workspace_id or self.workspace_id_factory(),
        )
        self.repository.create(session)
        self._ensure_session_workspace(session)
        return session

    def create_from_question(self, research_question: str, workspace_id: str | None = None) -> ResearchSession:
        question = " ".join(research_question.split())
        session = ResearchSession(
            session_id=self.session_id_factory(),
            research_question=question,
            title=question,
            created_at=self.clock(),
            workspace_id=workspace_id or self.workspace_id_factory(),
        )
        self.repository.create(session)
        self._ensure_session_workspace(session)
        return session

    def create_from_paper(self, paper_id: str, workspace_id: str | None = None) -> ResearchSession:
        session = ResearchSession(
            session_id=self.session_id_factory(),
            research_question=None,
            title="新会话",
            created_at=self.clock(),
            workspace_id=workspace_id or self.workspace_id_factory(),
        )
        self.repository.create(session)
        self._ensure_session_workspace(session)
        self.attach_paper(session.session_id, paper_id)
        # A paper-first entry point is an explicit hand-off from a knowledge
        # asset into the reading workspace.  Return the persisted layout with
        # the paper already open so the caller never lands on a blank session
        # and has to discover the context manually.
        return self.update_layout(
            session.session_id,
            paper_panel_open=True,
            active_paper_id=paper_id,
            update_active_paper=True,
        )

    def workspace_id_for(self, session: ResearchSession) -> str:
        """Return the session's real resource workspace id. Business code must
        read the stored ``workspace_id`` — never derive it from ``session_id``."""
        if not session.workspace_id:
            raise ValueError(f"session {session.session_id} has no workspace binding")
        return session.workspace_id

    def attach_paper(self, session_id: str, paper_id: str) -> None:
        self.get(session_id)
        if paper_id in self.repository.list_paper_ids(session_id):
            return
        self.repository.link_paper(session_id, paper_id)

    def get(self, session_id: str) -> ResearchSession:
        session = self.repository.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    def list(self, lifecycle: SessionLifecycle | None = None) -> tuple[ResearchSession, ...]:
        return self.repository.list(lifecycle)

    def find_active_for_paper(
        self,
        paper_id: str,
        workspace_id: str | None = None,
    ) -> ResearchSession | None:
        """Find the most recently created active session that already owns a paper.

        Paper-note hand-offs are an idempotent *open* action, not a request to
        create a new research project every time.  The repository interface is
        intentionally kept small, so the lookup uses the existing session and
        paper-link seams and works for both SQLite and in-memory repositories.
        A supplied workspace id scopes the lookup; otherwise the newest active
        session across workspaces wins.
        """
        matches = [
            session
            for session in self.list(SessionLifecycle.ACTIVE)
            if (workspace_id is None or session.workspace_id == workspace_id)
            and paper_id in self.repository.list_paper_ids(session.session_id)
        ]
        return max(matches, key=lambda session: session.created_at, default=None)

    def update_question(self, session_id: str, research_question: str) -> ResearchSession:
        question = " ".join(research_question.split())
        updated = replace(self.get(session_id), research_question=question)
        self.repository.save(updated)
        return updated

    def update_layout(
        self,
        session_id: str,
        *,
        paper_panel_open: bool | None = None,
        active_paper_id: str | None = None,
        update_active_paper: bool = False,
    ) -> ResearchSession:
        current = self.get(session_id)
        attached_paper_ids = self.repository.list_paper_ids(session_id)
        next_paper_panel_open = (
            current.paper_panel_open if paper_panel_open is None else paper_panel_open
        )
        active_paper = (
            current.active_paper_id
            if current.active_paper_id in attached_paper_ids
            else (
                attached_paper_ids[0]
                if next_paper_panel_open and attached_paper_ids
                else None
            )
        )
        updated = replace(
            current,
            paper_panel_open=next_paper_panel_open,
            active_paper_id=(
                active_paper_id
                if update_active_paper
                else active_paper
            ),
        )
        self.repository.save(updated)
        return updated

    def rename(self, session_id: str, title: str) -> ResearchSession:
        updated = replace(self.get(session_id), title=" ".join(title.split()))
        self.repository.save(updated)
        return updated

    def archive(self, session_id: str) -> ResearchSession:
        updated = replace(self.get(session_id), lifecycle=SessionLifecycle.ARCHIVED)
        self.repository.save(updated)
        return updated

    def restore(self, session_id: str) -> ResearchSession:
        updated = replace(self.get(session_id), lifecycle=SessionLifecycle.ACTIVE)
        self.repository.save(updated)
        return updated

    def delete(self, session_id: str) -> None:
        self.get(session_id)
        self.repository.delete(session_id)
