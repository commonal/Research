"""One-call fixed-context chat assembly and durable message history."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Protocol

from research_pulse.workbench.context_resolution import ContextResolver
from research_pulse.workbench.paper_access import PaperAccessService, PaperNotReadyError, PaperNotAttachedError
from research_pulse.workbench.prompt_budget import BudgetedBlock, BudgetedMessage, PromptBudgeter
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.citations import CitationRecord, extract_citations, project_citations


class ChatModel(Protocol):
    def complete(self, prompt: str) -> str: ...


class ChatRepository(Protocol):
    def insert_chat_message(self, message: "ChatMessageRecord") -> None: ...
    def save_chat_message(self, message: "ChatMessageRecord") -> None: ...
    def list_chat_messages(self, session_id: str) -> tuple["ChatMessageRecord", ...]: ...
    def replace_citations(self, message_id: str, citations: tuple[CitationRecord, ...]) -> None: ...


@dataclass(frozen=True)
class ChatMessageRecord:
    message_id: str
    session_id: str
    role: str
    text: str
    scope: str
    generation_status: str
    created_at: datetime
    metadata: dict[str, Any]
    safe_error: str | None = None
    citations: tuple[CitationRecord, ...] = ()


@dataclass(frozen=True)
class PreparedChatTurn:
    """Persisted pending messages plus the bounded prompt for one turn.

    Preparing a turn is deliberately separate from provider execution.  The
    API can persist the user message and a generating assistant message, then
    hand the provider call to a background worker without making the browser
    wait for model latency.
    """

    assistant: ChatMessageRecord
    prompt: str
    block_papers: dict[str, str]
    paper_id: str | None


class ChatGenerationError(RuntimeError):
    def __init__(self, message_id: str) -> None:
        super().__init__("模型生成失败，可重试")
        self.message_id = message_id


class ChatService:
    def __init__(self, repository: ChatRepository, session_service: SessionService,
                 paper_access: PaperAccessService, resolver: ContextResolver,
                 budgeter: PromptBudgeter, model: ChatModel, *,
                 message_id_factory: Callable[[], str], clock: Callable[[], datetime]) -> None:
        self.repository = repository
        self.session_service = session_service
        self.paper_access = paper_access
        self.resolver = resolver
        self.budgeter = budgeter
        self.model = model
        self.message_id_factory = message_id_factory
        self.clock = clock

    def list(self, session_id: str) -> tuple[ChatMessageRecord, ...]:
        self.session_service.get(session_id)
        return tuple(self._project(item) for item in self.repository.list_chat_messages(session_id))

    def _project(self, message: ChatMessageRecord) -> ChatMessageRecord:
        return replace(
            message,
            citations=project_citations(
                message.citations,
                allowlist=message.metadata.get("actual_block_ids", ()),
                session_id=message.session_id,
                paper_access=self.paper_access,
            ),
        )

    def send(self, session_id: str, *, query: str, scope: str,
             paper_id: str | None = None, block_id: str | None = None,
             block_ids: tuple[str, ...] = (), section_path: tuple[str, ...] = (),
             selected_text: str | None = None) -> ChatMessageRecord:
        return self.complete_prepared(self.prepare(
            session_id,
            query=query,
            scope=scope,
            paper_id=paper_id,
            block_id=block_id,
            block_ids=block_ids,
            section_path=section_path,
            selected_text=selected_text,
        ))

    def prepare(self, session_id: str, *, query: str, scope: str,
                paper_id: str | None = None, block_id: str | None = None,
                block_ids: tuple[str, ...] = (), section_path: tuple[str, ...] = (),
                selected_text: str | None = None) -> PreparedChatTurn:
        """Resolve context and persist queued messages without calling the model."""
        self.session_service.get(session_id)
        question = query.strip()
        if not question:
            raise ValueError("query must not be blank")
        block_papers: dict[str, str] = {}
        if scope == "none":
            blocks = ()
        elif scope == "full" and paper_id is None:
            collected = []
            for candidate_paper_id in self.paper_access.repository.list_paper_ids(session_id):
                try:
                    candidate_blocks = self.paper_access.load_resolvable_blocks(session_id, candidate_paper_id)
                except (PaperNotReadyError, PaperNotAttachedError):
                    continue
                for block in candidate_blocks:
                    collected.append(block)
                    block_papers[block.block_id] = candidate_paper_id
            blocks = tuple(collected)
        else:
            blocks = self.paper_access.load_resolvable_blocks(session_id, paper_id or "")
            if paper_id:
                block_papers = {block.block_id: paper_id for block in blocks}
        resolved = self.resolver.resolve(
            scope, blocks, block_id=block_id, block_ids=block_ids, section_path=section_path
        )
        exact_selection = selected_text.strip() if selected_text and selected_text.strip() else None
        old_messages = self.repository.list_chat_messages(session_id)
        history = tuple(
            BudgetedMessage(item.message_id, item.role, item.text)
            for item in old_messages if item.generation_status == "completed"
        )
        system_text = (
            "你是研究论文工作台助手。只根据明确提供的上下文起草回答；"
            + ("当前没有论文上下文。" if scope == "none" else "引用论文内容时保留完整 block_id。")
        )
        allocation = self.budgeter.allocate(
            system_text=system_text, question=question,
            context_blocks=tuple(BudgetedBlock(item.block_id, item.text) for item in resolved.blocks),
            history=history,
        )
        metadata = {
            "scope": scope, "paper_id": paper_id,
            "selected_text": exact_selection,
            "selection_block_ids": list(resolved.block_ids),
            "paper_context_used": bool(allocation.context_blocks),
            "actual_block_ids": [item.block_id for item in allocation.context_blocks],
            "paper_context_truncated": resolved.truncated or allocation.paper_context_truncated,
            "history_truncated": allocation.history_truncated,
            "context_chips": [
                {
                    "paper_id": block_papers.get(item.block_id, paper_id),
                    "block_id": item.block_id,
                    "section_path": list(next(
                        block.section_path for block in resolved.blocks
                        if block.block_id == item.block_id
                    )),
                    "page": next(
                        block.page for block in resolved.blocks
                        if block.block_id == item.block_id
                    ),
                    "truncated": item.truncated,
                }
                for item in allocation.context_blocks
            ],
        }
        user = ChatMessageRecord(self.message_id_factory(), session_id, "user", question,
                                 scope, "completed", self.clock(), metadata)
        assistant = ChatMessageRecord(self.message_id_factory(), session_id, "assistant", "",
                                      scope, "queued", self.clock(), metadata)
        self.repository.insert_chat_message(user)
        self.repository.insert_chat_message(assistant)
        assistant = replace(assistant, generation_status="generating")
        self.repository.save_chat_message(assistant)
        prompt_parts = [system_text]
        if exact_selection:
            prompt_parts.append(
                "\n用户精确选区（翻译或解释时必须优先处理这一段，不要擅自扩展到未选内容）：\n"
                + exact_selection
            )
        if allocation.context_blocks:
            prompt_parts.append("\n论文上下文：")
            prompt_parts.extend(f"[{item.block_id}]\n{item.text}" for item in allocation.context_blocks)
        if allocation.model_history:
            prompt_parts.append("\n最近对话：")
            prompt_parts.extend(f"{item.role}: {item.text}" for item in allocation.model_history)
        prompt_parts.append(f"\n当前问题：{question}")

        return PreparedChatTurn(
            assistant=assistant,
            prompt="\n".join(prompt_parts),
            block_papers=block_papers,
            paper_id=paper_id,
        )

    def complete_prepared(self, prepared: PreparedChatTurn) -> ChatMessageRecord:
        """Run the provider and finalize an already-persisted pending turn."""
        assistant = prepared.assistant
        try:
            answer = self.model.complete(prepared.prompt)
        except Exception:
            assistant = replace(assistant, generation_status="failed",
                                safe_error="模型生成失败，可重试")
            self.repository.save_chat_message(assistant)
            raise ChatGenerationError(assistant.message_id) from None
        assistant = replace(assistant, text=answer, generation_status="completed")
        self.repository.save_chat_message(assistant)
        citations = tuple(
            replace(citation, paper_id=prepared.block_papers.get(citation.block_id, prepared.paper_id))
            for citation in extract_citations(
            answer, message_id=assistant.message_id, paper_id=prepared.paper_id
            )
        )
        self.repository.replace_citations(assistant.message_id, citations)
        return self._project(replace(assistant, citations=citations))

    def fail_prepared(self, prepared: PreparedChatTurn, *, safe_error: str = "回答未能排队，可重试") -> None:
        """Close a prepared turn when the worker cannot be submitted."""
        self.repository.save_chat_message(
            replace(prepared.assistant, generation_status="failed", safe_error=safe_error)
        )
