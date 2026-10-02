"""Adapters that let the unified turn runtime reuse current executors."""

from __future__ import annotations

from concurrent.futures import Executor
from typing import Callable, Protocol
from uuid import uuid4

from research_pulse.workbench.chat import ChatGenerationError, ChatMessageRecord, ChatService
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    CapabilityProfileRegistry,
    TurnRequest,
    TurnResult,
)


class TurnAdapter(Protocol):
    def execute(self, request: TurnRequest, decision: CapabilityDecision) -> TurnResult: ...


def _citation_payload(message: ChatMessageRecord) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "citation_id": citation.citation_id,
            "paper_id": citation.paper_id,
            "block_id": citation.block_id,
            "status": citation.status,
            "locator": citation.locator,
        }
        for citation in message.citations
    )


def _chat_kwargs(request: TurnRequest, decision: CapabilityDecision) -> dict[str, object]:
    """Map the normalized interaction context to the fixed-scope chat seam."""
    context = request.interaction_context
    selection = context.selection
    if decision.capability == "basic":
        return {
            "query": request.message,
            "scope": "none",
            "paper_id": None,
            "block_id": None,
            "block_ids": (),
            "section_path": (),
            "selected_text": None,
        }
    if selection is not None:
        return {
            "query": request.message,
            "scope": "selection",
            "paper_id": selection.paper_id,
            "block_id": selection.block_id,
            "block_ids": selection.block_ids or (selection.block_id,),
            "section_path": selection.section_path,
            "selected_text": selection.text,
        }
    if context.canonical_paper_id:
        return {
            "query": request.message,
            "scope": "full",
            "paper_id": context.canonical_paper_id,
            "block_id": None,
            "block_ids": (),
            "section_path": (),
            "selected_text": None,
        }
    return {
        "query": request.message,
        "scope": "none",
        "paper_id": None,
        "block_id": None,
        "block_ids": (),
        "section_path": (),
        "selected_text": None,
    }


class SynchronousTurnAdapter:
    """Wrap the existing one-call ChatService without changing its contract."""

    def __init__(self, chat_service: ChatService, *, turn_id_factory: Callable[[], str] = lambda: str(uuid4())) -> None:
        self.chat_service = chat_service
        self.turn_id_factory = turn_id_factory

    def execute(self, request: TurnRequest, decision: CapabilityDecision) -> TurnResult:
        message = self.chat_service.send(
            request.session_id, **_chat_kwargs(request, decision)
        )
        return TurnResult(
            turn_id=self.turn_id_factory(),
            assistant_message=message.text,
            capability_decision=decision,
            citations=_citation_payload(message),
            event_summary=({"event_type": "turn_completed", "summary": "回答已生成"},),
            status="completed",
        )


class BackgroundTurnAdapter:
    """Persist a paper turn, then run provider work outside the request path."""

    def __init__(
        self,
        chat_service: ChatService,
        *,
        executor: Executor,
        turn_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.chat_service = chat_service
        self.executor = executor
        self.turn_id_factory = turn_id_factory

    def execute(self, request: TurnRequest, decision: CapabilityDecision) -> TurnResult:
        prepared = self.chat_service.prepare(
            request.session_id,
            **_chat_kwargs(request, decision),
        )
        try:
            self.executor.submit(self._complete, prepared)
        except Exception:
            self.chat_service.fail_prepared(prepared)
            raise
        return TurnResult(
            turn_id=self.turn_id_factory(),
            assistant_message=None,
            capability_decision=decision,
            event_summary=({"event_type": "turn_started", "summary": "回答已排队"},),
            status="queued",
            durable_handle=prepared.assistant.message_id,
        )

    def _complete(self, prepared) -> None:
        try:
            self.chat_service.complete_prepared(prepared)
        except ChatGenerationError:
            # The message row is already marked failed by ChatService.  The
            # worker must not surface provider details to the request thread.
            return


class DurableTurnAdapter:
    """Create a durable Run/Attempt through the existing ExplorationService."""

    def __init__(
        self,
        exploration_service: ExplorationService,
        registry: CapabilityProfileRegistry,
        *,
        turn_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.exploration_service = exploration_service
        self.registry = registry
        self.turn_id_factory = turn_id_factory

    def execute(self, request: TurnRequest, decision: CapabilityDecision) -> TurnResult:
        profile = self.registry.get(decision.capability)
        context_block = request.metadata.get("context_block")
        config = {
            "profile": "assistant" if decision.capability == "research" else "literature",
            "capability": decision.capability,
            "retrieval_plan": decision.retrieval_plan,
            "allowed_tools": list(decision.allowed_tools),
            "resolved": {
                "retrieval_plan": decision.retrieval_plan,
                "mode": "research_synthesis" if decision.capability == "research" else "",
                "context_block": str(context_block) if context_block else None,
            },
        }
        run = self.exploration_service.create(
            request.session_id,
            request.message,
            config_snapshot=config,
            budgets=dict(profile.budget_profile),
        )
        return TurnResult(
            turn_id=self.turn_id_factory(),
            assistant_message=None,
            capability_decision=decision,
            event_summary=({"event_type": "turn_started", "summary": "研究任务已排队"},),
            status="queued",
            durable_handle=run.run_id,
        )
