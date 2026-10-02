"""Thin FastAPI transport for workbench session lifecycle operations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from research_pulse.workbench.sessions import (
    SessionLifecycle,
    SessionNotFoundError,
    SessionService,
)


class CreateSessionRequest(BaseModel):
    research_question: str | None = Field(default=None, max_length=2_000)
    paper_id: str | None = Field(default=None, max_length=240)
    workspace_id: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def one_entry_mode(self) -> "CreateSessionRequest":
        if self.research_question is not None and self.paper_id is not None:
            raise ValueError("research_question and paper_id are mutually exclusive")
        return self


class UpdateSessionRequest(BaseModel):
    research_question: str | None = Field(default=None, max_length=2_000)
    title: str | None = Field(default=None, max_length=200)
    paper_panel_open: bool | None = None
    active_paper_id: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateSessionRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


def session_payload(service: SessionService, session) -> dict[str, Any]:
    """Project one session for both the normal API and cross-surface hand-offs.

    Keeping this projection at the workbench boundary prevents the knowledge
    reader from inventing a second session DTO when a note opens the workbench.
    """
    result = asdict(session)
    result["created_at"] = session.created_at.isoformat().replace("+00:00", "Z")
    result["lifecycle"] = session.lifecycle.value
    result["paper_ids"] = list(service.repository.list_paper_ids(session.session_id))
    title_lookup = getattr(service.repository, "get_workspace_title", None)
    result["workspace_title"] = (
        title_lookup(session.workspace_id) if (title_lookup and session.workspace_id) else None
    )
    return result


def build_workbench_router(
    service: SessionService,
    *,
    session_workspace_cleanup: Callable[[str, str | None], None] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/workbench/sessions", tags=["workbench"])

    def payload(session) -> dict[str, Any]:
        return session_payload(service, session)

    def get_or_404(session_id: str):
        try:
            return service.get(session_id)
        except SessionNotFoundError as error:
            raise HTTPException(status_code=404, detail="workbench session not found") from error

    @router.post("", status_code=201)
    def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        if request.research_question is not None:
            session = service.create_from_question(request.research_question, request.workspace_id)
        elif request.paper_id is not None:
            session = service.create_from_paper(request.paper_id, request.workspace_id)
        else:
            session = service.create_empty(request.workspace_id)
        return payload(session)

    @router.get("")
    def list_sessions(
        lifecycle: Literal["active", "archived"] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        selected = SessionLifecycle(lifecycle) if lifecycle else None
        return {"items": [payload(item) for item in service.list(selected)]}

    @router.get("/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        return payload(get_or_404(session_id))

    @router.patch("/{session_id}")
    def update_session(session_id: str, request: UpdateSessionRequest) -> dict[str, Any]:
        get_or_404(session_id)
        session = service.get(session_id)
        if request.research_question is not None:
            session = service.update_question(session_id, request.research_question)
        if request.title is not None:
            session = service.rename(session_id, request.title)
        if "paper_panel_open" in request.model_fields_set or "active_paper_id" in request.model_fields_set:
            session = service.update_layout(
                session_id,
                paper_panel_open=request.paper_panel_open,
                active_paper_id=request.active_paper_id,
                update_active_paper="active_paper_id" in request.model_fields_set,
            )
        return payload(session)

    @router.post("/{session_id}/archive")
    def archive_session(session_id: str) -> dict[str, Any]:
        get_or_404(session_id)
        return payload(service.archive(session_id))

    @router.post("/{session_id}/restore")
    def restore_session(session_id: str) -> dict[str, Any]:
        get_or_404(session_id)
        return payload(service.restore(session_id))

    @router.delete("/{session_id}", status_code=204)
    def delete_session(session_id: str) -> Response:
        session = get_or_404(session_id)
        service.delete(session_id)
        if session_workspace_cleanup is not None:
            session_workspace_cleanup(session_id, session.workspace_id)
        return Response(status_code=204)

    return router


class RenameWorkspaceRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def build_resource_workspace_router(
    repository: Any,
    *,
    workspace_cleanup: Callable[[str], None] | None = None,
) -> APIRouter:
    """CRUD for the resource workspace entity (the research-project folder).

    Mounted at ``/api/workbench/workspace`` (singular) to stay unambiguous
    against the M4 ``/api/workbench/workspaces`` state router.
    """

    router = APIRouter(prefix="/api/workbench/workspace", tags=["workbench"])

    def get_title_or_404(workspace_id: str) -> str:
        lookup = getattr(repository, "get_workspace_title", None)
        title = lookup(workspace_id) if lookup else None
        if title is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        return title

    @router.put("/{workspace_id}")
    def rename_workspace(workspace_id: str, request: RenameWorkspaceRequest) -> dict[str, Any]:
        get_title_or_404(workspace_id)
        repository.rename_workspace(workspace_id, request.title)
        return {"workspace_id": workspace_id, "title": request.title}

    @router.delete("/{workspace_id}", status_code=204)
    def delete_workspace(workspace_id: str) -> Response:
        get_title_or_404(workspace_id)
        for session in repository.list_sessions_by_workspace(workspace_id):
            repository.delete(session.session_id)
        repository.delete_workspace(workspace_id)
        if workspace_cleanup is not None:
            workspace_cleanup(workspace_id)
        return Response(status_code=204)

    return router
