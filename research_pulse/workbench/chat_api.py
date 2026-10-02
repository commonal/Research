"""HTTP boundary for durable workbench chat messages."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from research_pulse.workbench.chat import ChatGenerationError, ChatMessageRecord, ChatService
from research_pulse.workbench.context_resolution import ContextResolutionError
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.paper_access import (
    PaperNotAttachedError, PaperNotFoundError, PaperNotReadyError, UnmanagedPaperPathError,
)
from research_pulse.workbench.scope_resolver import InteractionContext, ScopeResolver
from research_pulse.workbench.sessions import SessionNotFoundError
from research_pulse.workbench.turn_runtime import (
    assistant_research_budget,
    TurnRequest,
    TurnValidationError,
)
from research_pulse.workbench.turn_service import TurnRuntime


class SendMessageRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    scope: Literal["none", "selection", "section", "full", "explore"] = "none"
    paper_id: str | None = None
    block_id: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    selected_text: str | None = Field(default=None, max_length=12000)
    selection_rects: list[dict[str, float]] = Field(default_factory=list)
    selection_page_size: dict[str, float] | None = None
    section_path: list[str] = Field(default_factory=list)
    profile: Literal["literature", "assistant"] = "literature"
    interaction_context: dict[str, object] | None = None
    action: Literal[
        "translate", "explain", "ask_selection", "paper", "web", "web_search",
        "research", "research_run", "basic",
    ] | None = None
    client_request_id: str | None = Field(default=None, max_length=200)


def _payload(message: ChatMessageRecord) -> dict[str, object]:
    return {
        "message_id": message.message_id, "session_id": message.session_id,
        "role": message.role, "text": message.text, "scope": message.scope,
        "generation_status": message.generation_status,
        "created_at": message.created_at.isoformat(), "metadata": message.metadata,
        "safe_error": message.safe_error,
        "citations": [
            {
                "citation_id": item.citation_id,
                "paper_id": item.paper_id,
                "block_id": item.block_id,
                "status": item.status,
                "locator": item.locator,
            }
            for item in message.citations
        ],
    }


_EXPLORATION_CONFIG = {
    "run_profile": "literature_exploration",
    "adapter": "deep_agents",
    "adapter_version": "0.7.11",
}
_EXPLORATION_BUDGETS = {
    "model_rounds": 8, "tool_calls": 16, "block_reads": 24,
    "wall_seconds": 300, "input_tokens": 40000, "output_tokens": 8000,
}
# Web lookup is a bounded fact-finding turn, not a workspace research run.
# Keep its budget separate even when the user entered through the assistant
# profile; otherwise a few empty web responses can replay a 200k-token context.
_WEB_LOOKUP_BUDGETS = {
    "model_rounds": 4, "tool_calls": 8, "block_reads": 0,
    "wall_seconds": 180, "input_tokens": 30000, "output_tokens": 6000,
}
# Research-assistant profile: deeper multi-step runs (plan → retrieve → read →
# write workspace artifacts). Product-level budgets, independent of the locked
# V0 literature budgets above; to be calibrated by assistant-run evidence.
def _assistant_budgets() -> dict[str, int | bool]:
    """Build assistant budgets at request time so .env test mode is honored."""
    enabled = os.getenv("RESEARCH_PULSE_EXPERIMENTAL_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return assistant_research_budget(experimental_mode=enabled)


def build_workbench_chat_router(
    service: ChatService,
    *,
    exploration_service: ExplorationService | None = None,
    assistant_exploration_service: ExplorationService | None = None,
    turn_runtime: TurnRuntime | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/workbench/sessions/{session_id}/messages")

    @router.get("")
    def list_messages(session_id: str) -> dict[str, object]:
        try:
            return {"items": [_payload(item) for item in service.list(session_id)]}
        except SessionNotFoundError as error:
            raise HTTPException(404, "session not found") from error

    @router.post("", status_code=200)
    def send_message(session_id: str, request: SendMessageRequest) -> dict[str, object]:
        try:
            if turn_runtime is not None:
                context_data = dict(request.interaction_context or {})
                # Legacy fixed-scope fields are translated into the normalized
                # interaction context for clients that have not adopted the
                # new request shape yet.
                if request.paper_id and not context_data.get("canonical_paper_id"):
                    context_data["canonical_paper_id"] = request.paper_id
                if request.scope in {"selection", "section", "full"} and request.paper_id:
                    context_data.setdefault("surface", "paper_reader")
                if request.scope == "selection" and request.paper_id and request.block_id:
                    context_data["selection"] = {
                        "paper_id": request.paper_id,
                        "block_id": request.block_id,
                        "block_ids": list(request.block_ids or [request.block_id]),
                        "text": request.selected_text or "",
                        "section_path": list(request.section_path),
                    }
                try:
                    turn_request = TurnRequest(
                        session_id=session_id,
                        message=request.query,
                        interaction_context=InteractionContext.from_dict(context_data),
                        # The unified runtime owns capability selection.  Keep
                        # legacy ``scope=explore`` for the old branch only; it
                        # must not silently upgrade an ordinary prompt into a
                        # durable research run.
                        explicit_action=request.action,
                        client_request_id=request.client_request_id,
                        metadata={"legacy_scope": request.scope, "profile": request.profile},
                    )
                except (KeyError, TypeError, ValueError, TurnValidationError) as error:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "invalid_turn_request",
                            "message": "请求上下文无效，请重新打开论文或重新选择内容。",
                        },
                    ) from error
                result = turn_runtime.start(turn_request)
                from fastapi.responses import JSONResponse
                status_code = 202 if result.status in {"queued", "running"} else 200
                return JSONResponse(
                    status_code=status_code,
                    content={"kind": "turn", "result": result.to_dict()},
                )
            # Keep explicit capability actions meaningful for older embedders
            # that have not wired the unified TurnRuntime yet.  A research_run
            # must never silently fall through to ChatService (which would
            # produce a paper/basic answer instead of a durable run).
            explicit_research = request.action in {"research", "research_run"}
            if request.scope == "explore" or explicit_research:
                assistant = request.profile == "assistant" or explicit_research
                config = {**_EXPLORATION_CONFIG, "profile": "assistant" if assistant else request.profile}
                # Resolve every exploration turn, including global chat. An
                # absent interaction context is still meaningful: it should
                # default to a low-cost direct answer rather than silently
                # entering the full paper-search harness.
                resolved = ScopeResolver().resolve(
                    request.query,
                    InteractionContext.from_dict(request.interaction_context),
                )
                resolved_payload = resolved.to_dict()
                if explicit_research:
                    resolved_payload.update({
                        "mode": "research_synthesis",
                        "retrieval_plan": "research_exploration",
                    })
                config["resolved"] = resolved_payload
                lightweight_web = resolved.retrieval_plan == "web_lookup"
                if lightweight_web:
                    # Web lookup has no workspace writes or paper-block reads;
                    # use the read-only runtime and its bounded budget even if
                    # the UI currently labels the turn as "assistant". The
                    # profile is part of the durable Attempt snapshot, so it
                    # must also be rewritten here; otherwise the worker would
                    # select the assistant runtime, expose read_workspace_state
                    # for paper-bound sessions, and spend the tiny web budget
                    # on planning before search_web is called.
                    config["requested_profile"] = request.profile
                    config["profile"] = "literature"
                    active_service = exploration_service or assistant_exploration_service
                    budgets = _WEB_LOOKUP_BUDGETS
                else:
                    active_service = (assistant_exploration_service if assistant else exploration_service)
                    budgets = _assistant_budgets() if assistant else _EXPLORATION_BUDGETS
                if active_service is None:
                    raise HTTPException(503, "exploration is unavailable")
                run = active_service.create(
                    session_id,
                    request.query,
                    config_snapshot=config,
                    budgets=budgets,
                )
                # An exploration is a durable run, never a chat message. 202 is
                # set on the response below by raising the explicit response.
                from fastapi.responses import JSONResponse
                run_payload = asdict(run)
                if run.created_at is not None:
                    run_payload["created_at"] = run.created_at.isoformat()
                return JSONResponse(
                    status_code=202,
                    content={"kind": "exploration_run", "run": run_payload},
                )
            return _payload(service.send(
                session_id, query=request.query, scope=request.scope,
                paper_id=request.paper_id, block_id=request.block_id,
                block_ids=tuple(request.block_ids), selected_text=request.selected_text,
                section_path=tuple(request.section_path),
            ))
        except SessionNotFoundError as error:
            raise HTTPException(404, "session not found") from error
        except (PaperNotFoundError, PaperNotAttachedError) as error:
            raise HTTPException(404, "paper not attached") from error
        except (PaperNotReadyError, UnmanagedPaperPathError) as error:
            raise HTTPException(409, "paper context is not ready") from error
        except (ContextResolutionError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        except ChatGenerationError as error:
            raise HTTPException(502, {"code": "generation_failed", "message_id": error.message_id}) from error

    return router
