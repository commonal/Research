"""Pinned Deep Agents V0 adapter for the read-only workbench experiment."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import nullcontext
import hashlib
import json
from threading import RLock
from typing import Any
from uuid import uuid4

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import StateBackend
from deepagents.backends.protocol import SandboxBackendProtocol, WriteResult
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, Field

from research_pulse.workbench.agent_runtime import (
    AgentRunInput,
    AgentRunResult,
    RunBudgets,
    AgentEvent,
    ToolCapability,
)
from research_pulse.workbench.budget_enforcer import BudgetEnforcer, RunTerminated
from research_pulse.workbench.capability_preflight import (
    CapabilityPreflightError,
    require_exact_readonly_capabilities,
)
from research_pulse.workbench.capability_policy import (
    assistant_capability_policy,
    capability_policy_for_mode,
    direct_capability_policy,
    paper_evidence_capability_policy,
    synthesis_capability_policy,
    web_lookup_capability_policy,
    verify_capability_policy,
)
from research_pulse.workbench.research_tools import ReadOnlyResearchTools, ToolPolicyError
from research_pulse.workbench.supporting_paper import SupportingPaperImportError
from research_pulse.workbench.scope_resolver import ResolveMode
from research_pulse.workbench.tool_dispatcher import ToolDispatcher
from research_pulse.workbench.tool_execution import ToolOutcomeStatus
from research_pulse.workbench.tool_policy_adapters import build_workbench_tool_registry
from research_pulse.workbench.workspace import ResearchIteration, ResearchIterationStatus
from research_pulse.workbench.workspace_tools import WorkspaceResearchTools, WorkspaceToolError
from research_pulse.workbench.turn_runtime import research_provider_output_limit


# Network-touching tools: a stalled external call must never freeze the run.
# Local file/catalog tools stay on the caller thread (no thread churn).
_NETWORK_TOOLS = frozenset({"search_arxiv", "import_supporting_paper"})
_TOOL_CALL_POLL_SECONDS = 2.0
_TOOL_CALL_MAX_SECONDS = 150.0
_NETWORK_TOOL_TIMEOUTS = {
    # Discovery is optional.  A stalled arXiv provider must not consume most
    # of the research turn's wall budget; the agent can still synthesize from
    # the anchor paper and any managed evidence.
    "search_arxiv": 25.0,
    "import_supporting_paper": 120.0,
}
# arXiv discovery is for finding candidates, not for hoarding context: cap the
# number of search_arxiv calls per run so the input-token budget is spent on
# reading and synthesis, not on repeat searches.
_SEARCH_CALL_LIMIT = 4
_SEARCH_FAILURE_LIMIT = 1
_WEB_SEARCH_ATTEMPT_LIMIT = 3
_WEB_SEARCH_FAILURE_LIMIT = 2
_MAX_BLOCKS_PER_READ = 20
# A model round replays the previous tool messages.  Letting the agent read
# the whole paper in many batches therefore grows the next prompt
# quadratically.  Keep a smaller, explicit per-attempt evidence cap so a
# research run reaches synthesis before the input-token budget is consumed.
_MAX_BLOCKS_PER_ATTEMPT = 32
# Workspace writes are durable side effects, not a second research loop.  In
# production a runaway model may emit the same write family many times in one
# turn; after a bounded number we return a recoverable instruction to synthesize
# instead of growing the transcript indefinitely.  Explicit local-test mode
# disables this application cap and relies on the wall/provider safeguards.
_MAX_WORKSPACE_WRITES_PER_ATTEMPT = 20
_WORKSPACE_WRITE_TOOLS = frozenset({
    "update_subquestions", "add_evidence", "update_research_map",
    "update_research_plan", "import_supporting_paper",
})
# The graph keeps tool messages for the whole attempt.  Before each provider
# call we send a bounded view of that history; otherwise every new round
# replays all previously read blocks and the cumulative input budget is spent
# even when the agent has already reached a sufficient evidence boundary.
_MAX_PROVIDER_CONTEXT_CHARS = 30000
_MAX_PROVIDER_MESSAGE_CHARS = 10000
_MAX_HISTORICAL_TOOL_CHARS = 1200

ALLOWED_TOOLS = (
    "search_sources",
    "read_paper_metadata",
    "read_managed_blocks",
    "read_run_status",
    "search_arxiv",
)
# Research-assistant profile: adds the workspace file tools and planning.
# `execute`, `task`, `delete` and raw shell stay forbidden — the filesystem
# backend is a non-sandbox FilesystemBackend, so `execute` never materializes.
ALLOWED_TOOLS_ASSISTANT = (
    "search_sources",
    "read_paper_metadata",
    "read_managed_blocks",
    "read_run_status",
    "search_arxiv",
    "ls", "read_file", "write_file", "edit_file", "glob", "grep", "write_todos",
    "read_workspace_state", "update_subquestions", "add_evidence", "update_research_map", "update_research_plan",
    "import_supporting_paper",
)
_DEEPAGENTS_BUILTINS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "task"}
)
_PROFILE_KEY = "openai:deepseek-v4-flash"
# Assistant profile resolves under its own model name: the runtime's model
# instances report this ls_model_name in assistant mode, so deepagents picks
# this profile (execute/task/delete excluded) instead of the read-only one.
_ASSISTANT_PROFILE_KEY = "openai:research-assistant"
_ASSISTANT_EXCLUDED_TOOLS = frozenset({"execute", "task", "delete"})


def register_readonly_profile() -> None:
    register_harness_profile(
        _PROFILE_KEY,
        HarnessProfile(
            excluded_tools=_DEEPAGENTS_BUILTINS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )


def register_assistant_profile() -> None:
    register_harness_profile(
        _ASSISTANT_PROFILE_KEY,
        HarnessProfile(
            excluded_tools=_ASSISTANT_EXCLUDED_TOOLS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )


def _compact_candidate(item: dict[str, object]) -> dict[str, object]:
    """Persistable slice of one arXiv search result (metadata + abstract)."""
    return {
        "arxiv_id": str(item.get("arxiv_id") or ""),
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or item.get("source_url") or ""),
        "source_id": str(item.get("source_id") or ""),
        "abstract_preview": str(item.get("abstract_preview") or "")[:400],
        "categories": [str(c) for c in (item.get("categories") or [])],
        "published": str(item.get("published") or ""),
        "relevance": float(item.get("relevance") or 0.0),
    }


def _short_text(value: object, limit: int = 600) -> str:
    """Keep agent-facing workspace echoes bounded without changing persistence."""
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _compact_workspace_state(value: object) -> dict[str, object]:
    """Project durable state into a small planning snapshot for the model.

    The complete state is still available through the repository/API.  The
    model only needs stable ids and short labels to decide its next action;
    replaying every claim and evidence paragraph in every round is the main
    source of input-token blow-up in long research attempts.
    """
    if not isinstance(value, dict):
        return {"ok": True, "state": _short_text(value)}
    result: dict[str, object] = {
        key: value.get(key)
        for key in (
            "workspace_id", "research_question", "research_intent",
            "active_focus_id", "anchor_paper_id", "status",
        )
        if key in value
    }
    result["research_map"] = [
        {
            "node_id": item.get("node_id"),
            "label": _short_text(item.get("label"), 240),
            "related_question_ids": list(item.get("related_question_ids") or [])[:12],
            "evidence_ids": list(item.get("evidence_ids") or [])[:12],
        }
        for item in (value.get("research_map") or [])[:12]
        if isinstance(item, dict)
    ]
    result["subquestions"] = [
        {
            "question_id": item.get("question_id"),
            "text": _short_text(item.get("text"), 400),
            "status": item.get("status"),
            "researchability": item.get("researchability"),
            "answer": _short_text(item.get("answer"), 400),
        }
        for item in (value.get("subquestions") or [])[:12]
        if isinstance(item, dict)
    ]
    result["evidence"] = [
        {
            "evidence_id": item.get("evidence_id"),
            "source_id": item.get("source_id"),
            "block_ids": list(item.get("block_ids") or [])[:8],
            "supports_question_ids": list(item.get("supports_question_ids") or [])[:8],
            "evidence_role": item.get("evidence_role"),
            "claim": _short_text(item.get("claim"), 500),
            "confidence": item.get("confidence"),
        }
        for item in (value.get("evidence") or [])[:16]
        if isinstance(item, dict)
    ]
    plan = value.get("research_plan")
    if isinstance(plan, dict):
        result["research_plan"] = {
            "stage": plan.get("stage"),
            "iterations": [
                {
                    "iteration_id": item.get("iteration_id"),
                    "sequence": item.get("sequence"),
                    "title": _short_text(item.get("title"), 240),
                    "status": item.get("status"),
                    "focus_question_id": item.get("focus_question_id"),
                    "summary": _short_text(item.get("summary"), 500),
                    "candidate_question_ids": list(item.get("candidate_question_ids") or [])[:12],
                    "next_step": _short_text(item.get("next_step"), 300),
                }
                for item in (plan.get("iterations") or [])[-8:]
                if isinstance(item, dict)
            ],
            "title": _short_text(plan.get("title"), 240),
            "method_map": [
                _short_text(item, 240)
                for item in (plan.get("method_map") or [])[:8]
            ],
            "hypotheses": [
                _short_text(item, 300)
                for item in (plan.get("hypotheses") or [])[:8]
            ],
        }
    return result


def _compact_workspace_result(tool_name: str, value: object) -> dict[str, object]:
    """Return an acknowledgement rather than echoing a full write payload."""
    if not isinstance(value, dict):
        return {"ok": True, "tool": tool_name}
    allowed = {
        key: value[key]
        for key in (
            "status", "question_id", "evidence_id", "node_id", "decision_point_id",
            "decision_kind", "source_id", "source_identity", "pdf_status", "parse_status",
        )
        if key in value
    }
    allowed["ok"] = True
    allowed["tool"] = tool_name
    return allowed


def _compact_provider_messages(
    messages: Sequence[BaseMessage], *, max_context_chars: int = _MAX_PROVIDER_CONTEXT_CHARS,
) -> list[BaseMessage]:
    """Bound the replayed graph history passed to the model provider.

    LangGraph must retain the full message sequence internally so tool-call
    ids stay valid.  We only compact the provider-facing copies: recent tool
    results remain useful for synthesis, while old block payloads are reduced
    to a short marker.  This keeps the cumulative input budget predictable
    without deleting durable evidence or altering event traces.
    """
    if not messages:
        return []
    compacted: list[BaseMessage] = []
    total_chars = 0
    # Keep the system/user framing and the newest tool results preferentially.
    preserve_prefix = min(3, len(messages))
    preserve_suffix_start = max(preserve_prefix, len(messages) - 8)
    for index, message in enumerate(messages):
        content = message.content
        if not isinstance(content, str):
            content = str(content)
        if index < preserve_prefix or index >= preserve_suffix_start:
            limit = _MAX_PROVIDER_MESSAGE_CHARS
        elif getattr(message, "type", "") == "tool":
            limit = _MAX_HISTORICAL_TOOL_CHARS
        else:
            limit = 3000
        if len(content) > limit:
            content = content[:limit] + "\n[历史上下文已压缩]"
        # Once the overall cap is reached, retain message structure but avoid
        # replaying more payload.  Tool ids and names remain on the message.
        if total_chars + len(content) > max_context_chars:
            content = "[历史工具输出已压缩，请依据当前保留证据总结]"
        total_chars += len(content)
        try:
            compacted.append(message.model_copy(update={"content": content}))
        except AttributeError:
            # Older LangChain message implementations are mutable enough for a
            # shallow copy; this branch keeps the adapter version-tolerant.
            import copy
            clone = copy.copy(message)
            clone.content = content
            compacted.append(clone)
    return compacted


def _call_network_tool(
    facade: ReadOnlyResearchTools,
    name: str,
    args: dict[str, Any],
    guard: BudgetEnforcer,
) -> Any:
    """Run one network-touching tool call under a deadline.

    Budget checks only happen between calls, so a stalled external service
    (arXiv MCP upstream fetch, PDF download) would freeze the whole run. The
    watchdog enforces a per-call ceiling (bounded by the remaining wall budget)
    and aborts the moment the user cancels. The worker thread is abandoned,
    never killed — the facade is read-only, so a lingering fetch is harmless.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    remaining_wall = guard.limits.wall_seconds - (guard.clock() - guard.started_at)
    call_limit = _NETWORK_TOOL_TIMEOUTS.get(name, _TOOL_CALL_MAX_SECONDS)
    ceiling = max(15.0, min(call_limit, _TOOL_CALL_MAX_SECONDS, remaining_wall))
    deadline = guard.clock() + ceiling
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
    future = pool.submit(facade.invoke, name, args)
    try:
        while True:
            if guard.status != "running":
                future.cancel()
                raise RunTerminated(guard.status, guard.stop_dimension)
            try:
                return future.result(timeout=_TOOL_CALL_POLL_SECONDS)
            except FutureTimeout:
                if guard.clock() >= deadline:
                    raise TimeoutError(
                        f"{name} did not respond within {ceiling:.0f}s "
                        "(network stall or service hang)"
                    ) from None
    finally:
        pool.shutdown(wait=False)


class BudgetedChatModel(BaseChatModel):
    """Reserve every model call before delegating it to the provider."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    delegate: Any
    enforcer: BudgetEnforcer
    max_output_tokens: int = 1000
    model_name: str = "deepseek-v4-flash"
    bound_kwargs: dict[str, Any] = Field(default_factory=dict)
    @property
    def _llm_type(self) -> str:
        return "budgeted-deepseek-v0"

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "BudgetedChatModel":
        return self.model_copy(update={"delegate": self.delegate.bind_tools(tools, **kwargs)})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        provider_messages = _compact_provider_messages(messages)
        estimated_input = max(1, sum(len(str(message.content).encode("utf-8")) for message in provider_messages) // 3)
        # Near the end of an attempt, fall back to an even smaller provider
        # view so a final synthesis call can still fit.  This is deliberately
        # local to the adapter: the durable graph history and evidence remain
        # unchanged, while the model receives the newest tool results and the
        # explicit stop instructions rather than a hard budget exception.
        remaining_input = self.enforcer.remaining("input_tokens")
        if estimated_input > remaining_input:
            compact_chars = max(6000, remaining_input * 3 - 600)
            provider_messages = _compact_provider_messages(
                messages, max_context_chars=compact_chars,
            )
            estimated_input = max(1, sum(len(str(message.content).encode("utf-8")) for message in provider_messages) // 3)
        reservation = self.enforcer.authorize_model(estimated_input, self.max_output_tokens)
        result = self.delegate.invoke(provider_messages, stop=stop, **kwargs)
        output_tokens = self.max_output_tokens
        if isinstance(result, AIMessage) and result.usage_metadata:
            output_tokens = min(
                self.max_output_tokens,
                int(result.usage_metadata.get("output_tokens", self.max_output_tokens)),
            )
        self.enforcer.settle_model(reservation, actual_output_tokens=output_tokens)
        return ChatResult(generations=[ChatGeneration(message=result)])


class EffectiveToolProbeModel(BaseChatModel):
    """Records the post-middleware tool surface without calling a provider."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    recorded_tool_names: list[str] = Field(default_factory=list)
    model_name: str = "deepseek-v4-flash"

    def __init__(self, *, model_name: str = "deepseek-v4-flash", **kwargs: Any) -> None:
        super().__init__(model_name=model_name, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "effective-tool-probe"

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "openai", "ls_model_name": self.model_name}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "EffectiveToolProbeModel":
        # The tool list may mix StructuredTool objects with plain tool-name
        # strings (the assistant profile passes some tools as bare names); record
        # the effective name in either case, and don't crash on foreign entries.
        names = []
        for tool in tools:
            name = getattr(tool, "name", None)
            if name is None and isinstance(tool, str):
                name = tool
            if name:
                names.append(str(name))
        self.recorded_tool_names[:] = names
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="probe complete"))])


class _EventEmittingBackend:
    """Delegating backend wrapper that emits workspace file events.

    Only used by the assistant profile. Read operations delegate unchanged;
    write/edit/delete additionally emit a sanitized `file_written` event with
    the workspace-relative path (safe event gate rejects absolute paths).
    """

    def __init__(self, inner: Any, on_file_event: Callable[[str, str], None]) -> None:
        self._inner = inner
        self._on_file_event = on_file_event

    def _note(self, action: str, result: Any, path: str | None = None) -> Any:
        error = getattr(result, "error", None)
        if not error:
            self._on_file_event(action, str(path if path is not None else getattr(result, "path", "")))
        return result

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        def _call(*args: Any, **kwargs: Any) -> Any:
            return attr(*args, **kwargs)
        _call.__name__ = getattr(attr, "__name__", name)
        return _call

    def write(self, file_path: str, content: str) -> Any:
        return self._note("write_file", self._inner.write(file_path, content), file_path)

    def edit(self, file_path: str, *args: Any, **kwargs: Any) -> Any:
        return self._note("edit_file", self._inner.edit(file_path, *args, **kwargs), file_path)

    def delete(self, file_path: str) -> Any:
        return self._note("delete", self._inner.delete(file_path), file_path)


# Canonical state files that the agent may NEVER write through raw file tools.
# These are the single source of truth and must only change via the controlled
# domain write tools (update_research_map / update_subquestions / add_evidence /
# update_research_plan).
_CANONICAL_STATE_PATHS = frozenset({
    "workspace.json",
    "research-map.json",
    "subquestions.json",
    "research-plan.json",
})
# Free-write areas that stay outside the canonical state: scratch notes, drafts,
# and any reports the assistant drafts for the user. Writes here are allowed
# (path-scoped), but they never touch canonical JSON.
_ALLOWED_SCRATCH_PREFIXES = ("scratch/", "notes/", "drafts/")


class PathScopedBackend(_EventEmittingBackend):
    """Backend wrapper that denies writes to canonical state files.

    The agent may freely write/edit/delete under the scratch/notes/drafts areas,
    but every write targeting a canonical state path is rejected before it reaches
    the underlying backend. This enforces the design rule that Research Map,
    subquestions, the typed research plan and evidence can only change through
    the controlled domain tools.
    """

    def _is_canonical_path(self, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/").lstrip("/")
        if normalized in _CANONICAL_STATE_PATHS:
            return True
        if normalized.startswith("evidence/") and normalized.endswith(".json"):
            return True
        return False

    def _reject(self, action: str, file_path: str) -> Any:
        error = WriteResult(error="forbidden: canonical state may only change through the controlled domain tools")
        self._on_file_event(action, file_path)
        return error

    def write(self, file_path: str, content: str) -> Any:
        if self._is_canonical_path(file_path):
            return self._reject("write_file", file_path)
        return self._note("write_file", self._inner.write(file_path, content), file_path)

    def edit(self, file_path: str, *args: Any, **kwargs: Any) -> Any:
        if self._is_canonical_path(file_path):
            return self._reject("edit_file", file_path)
        return self._note("edit_file", self._inner.edit(file_path, *args, **kwargs), file_path)

    def delete(self, file_path: str) -> Any:
        if self._is_canonical_path(file_path):
            return self._reject("delete", file_path)
        return self._note("delete", self._inner.delete(file_path), file_path)


# Mode-aware assistant system prompts. A resolved scope is hard state, so the
# prompt is chosen by mode rather than asking the model to infer it.
_PAPER_LOCAL_PROMPT = (
    "You are a paper Q&A assistant. Answer the user's question about a single paper "
    "directly, using the evidence context provided or the paper's blocks you read. "
    "Cite every claim with the FULL id of a block you actually read, exactly as returned by the tools, "
    "each id alone in its own square brackets; never abbreviate an id, never merge ids into one bracket, "
    "never invent an id for text you did not read. "
    "To read a whole paper, first call read_paper_metadata to get the FULL block_index "
    "(id + section_path + page), then read_managed_blocks on the block ids you actually "
    "need (abstract / method / results / conclusion) — do not try to read every block at once. "
    "The ask is about THIS paper only — do not search arXiv or the catalog for related "
    "work, do not write research state, do not plan an exploration. "
    "If the provided context already answers it, answer immediately. "
    "Label the answer as a draft; do not invent citations."
)

_PAPER_IN_RESEARCH_PROMPT = (
    "You are a research assistant working for an AI researcher. "
    "A paper is open and the user is asking how it relates to an active research question. "
    "Extract this paper's key findings and map them onto the research question and related "
    "When recording workspace state, use Simplified Chinese first and retain important English "
    "terms in parentheses; do not create bare English-only labels, questions, or evidence claims. "
    "work. Cite every claim with the FULL id of a block you actually read; each full id alone in its own square brackets; never abbreviate an id, never merge ids into one bracket, never invent an id for text you did not read. You may read this paper's "
    "blocks and record evidence in the workspace; do not re-search the catalog merely to "
    "locate this paper, but you may search for genuinely new related work."
)

_SYNTHESIS_PROMPT = (
    "You are completing a research answer from persisted evidence after a prior "
    "attempt exhausted its budget. Do not search for new sources, do not create "
    "new research tasks, and do not write workspace state. Use only the listed "
    "persisted evidence and the user's question. If a listed block is needed for "
    "a precise citation, read it with read_managed_blocks; otherwise synthesize "
    "the best bounded answer immediately. State the evidence boundary honestly "
    "instead of continuing exploration. Prior search candidates may also be listed "
    "as unverified leads: use them only to explain what should be imported or read "
    "next, never infer findings from a title or abstract preview and never cite an "
    "unread candidate. If the question asks for a survey or comparison beyond the "
    "read blocks, explicitly include a short '待精读候选' section instead of "
    "presenting the answer as comprehensive. Cite only blocks actually read with "
    "their full stable ids."
)

_DIRECT_PROMPT = (
    "Answer the user's question directly and concisely. Do not search the web, arXiv, "
    "or the paper catalog. Use the supplied paper/workspace read tools only when the "
    "question explicitly refers to attached material. Do not create research tasks or "
    "write canonical workspace state. If the question needs current or multi-source "
    "evidence, say so and ask the user to switch to search or research mode."
)

_PAPER_EVIDENCE_PROMPT = (
    "Answer from the attached paper and current research context. Read the anchor paper "
    "when exact paper claims are needed, but do not search for external papers unless the "
    "user explicitly asks for new or latest related work. Keep paper claims cited with "
    "full stable block ids and distinguish general explanation from paper evidence."
)

_WEB_LOOKUP_PROMPT = (
    "Answer using the configured web search provider. Search only when needed, keep the "
    "query focused, and cite factual claims with the returned page URLs in Markdown. "
    "Do not call search_sources or search_arxiv, do not treat web pages as managed paper "
    "blocks, and do not write canonical research workspace state. If the provider is "
    "unavailable or returns no results, explain the limitation instead of switching to "
    "arXiv or inventing sources. Try at most two focused queries; do not keep rewriting "
    "the same query after consecutive empty results."
)


def _assistant_prompt_for_mode(mode: str | None, retrieval_plan: str = "") -> str | None:
    if retrieval_plan == "direct":
        return _DIRECT_PROMPT
    if retrieval_plan == "web_lookup":
        return _WEB_LOOKUP_PROMPT
    if mode == ResolveMode.PAPER_IN_RESEARCH_CONTEXT and retrieval_plan != "research_exploration":
        return _PAPER_EVIDENCE_PROMPT
    if mode == ResolveMode.PAPER_LOCAL:
        return _PAPER_LOCAL_PROMPT
    if mode == ResolveMode.PAPER_IN_RESEARCH_CONTEXT:
        return _PAPER_IN_RESEARCH_PROMPT
    return None  # caller falls back to the default research-progression prompt


class DeepAgentsV0Runtime:
    def __init__(
        self,
        *,
        model_factory: Callable[[BudgetEnforcer], BaseChatModel],
        tools_factory: Callable[[str, Callable[[str], dict[str, object]]], ReadOnlyResearchTools],
        assistant: bool = False,
        workspace_resolver: Callable[[str], str | None] | None = None,
        workspace_tools_factory: Callable[[str], "WorkspaceResearchTools | None"] | None = None,
        web_search_provider: Any | None = None,
    ) -> None:
        self._model_factory = model_factory
        self._tools_factory = tools_factory
        self._assistant = assistant
        self._workspace_resolver = workspace_resolver
        self._workspace_tools_factory = workspace_tools_factory
        self._web_search_provider = web_search_provider

    def capabilities(self) -> tuple[ToolCapability, ...]:
        allowed = ALLOWED_TOOLS_ASSISTANT if self._assistant else ALLOWED_TOOLS
        if self._web_search_provider is not None:
            allowed = (*allowed, "search_web")
        return tuple(ToolCapability(name, read_only=True) for name in allowed)

    def start(self, run_input: AgentRunInput) -> AgentRunResult:
        guard = BudgetEnforcer(run_input.budgets)
        context_was_read = False
        retrieval_plan = ""
        # Keep the facade visible to the termination handler.  A research
        # attempt may hit its model-round/token budget immediately after the
        # durable question/evidence/map writes have committed; in that case we
        # can still expose the intended user-direction checkpoint instead of
        # discarding the valid workspace state as a generic failure.
        workspace_tools: WorkspaceResearchTools | None = None

        def emit(event_type: str, summary: str, *, stable_ids: dict[str, str] | None = None, extras: list[dict[str, object]] | None = None) -> None:
            nonlocal context_was_read
            snapshot = guard.snapshot()
            event = AgentEvent(
                event_type=event_type,  # type: ignore[arg-type]
                summary=summary,
                stable_ids=stable_ids or {},
                counters={
                    "model_rounds": snapshot.model_rounds,
                    "tool_calls": snapshot.tool_calls,
                    "block_reads": snapshot.block_reads,
                    "input_tokens": snapshot.input_tokens,
                    "output_tokens": snapshot.output_tokens,
                },
                extras=tuple(extras) if extras else (),
            )
            context_was_read = context_was_read or event_type == "context_read"
            if run_input.on_event is not None:
                run_input.on_event(event)

        def status_reader(run_id: str) -> dict[str, object]:
            snapshot = guard.snapshot()
            return {"run_id": run_id, **snapshot.__dict__}

        def record_iteration(status: ResearchIterationStatus, draft: str) -> None:
            """Persist one durable receipt for this bounded research run.

            The model may add a richer iteration through ``update_research_plan``;
            this runtime fallback guarantees that a completed/abandoned run is
            still visible in the multi-round timeline.  The append operation is
            metadata-only and idempotent by ``run_id``.
            """
            if workspace_tools is None or retrieval_plan != "research_exploration":
                return
            try:
                # ``AgentRunInput.run_id`` is the physical Attempt id in the
                # durable worker.  A budget continuation creates a new Attempt
                # for the same logical ExplorationRun, so use the stable
                # logical id for the iteration receipt and its idempotency key.
                logical_run_id = str(
                    run_input.resolved.get("logical_run_id")
                    or run_input.run_id
                )
                state = workspace_tools.read_workspace_state()
                plan = state.get("research_plan") or {}
                raw_iterations = plan.get("iterations") if isinstance(plan, dict) else []
                iterations = raw_iterations if isinstance(raw_iterations, list) else []
                existing_item = next(
                    (
                        item
                        for item in iterations
                        if isinstance(item, dict) and item.get("run_id") == logical_run_id
                    ),
                    None,
                )
                questions = state.get("subquestions") or []
                active_focus_id = state.get("active_focus_id") or None
                candidate_ids = tuple(
                    str(item.get("question_id"))
                    for item in questions
                    if isinstance(item, dict)
                    and item.get("question_id")
                    and item.get("researchability") == "candidate"
                    and item.get("status") == "open"
                    and (not active_focus_id or str(item.get("question_id")) != str(active_focus_id))
                )
                # Only the initial round waits for a direction decision.  Once
                # a focus has been chosen, candidates are ordinary follow-up
                # options shown in the overview; they must not turn every
                # completed answer into another blocking checkpoint.
                awaiting_focus = bool(candidate_ids) and not active_focus_id
                normalized = " ".join(str(draft or "").split())
                if len(normalized) > 360:
                    normalized = normalized[:357].rstrip() + "…"
                next_status = (
                    ResearchIterationStatus.WAITING_FOR_USER
                    if awaiting_focus and status in {
                        ResearchIterationStatus.COMPLETED,
                        ResearchIterationStatus.ABANDONED,
                    }
                    else status
                )
                if existing_item is not None:
                    # A continuation can finish a round that was persisted as
                    # abandoned/waiting after its previous Attempt stopped.
                    # Refresh that receipt instead of adding a second round.
                    if (
                        status is not ResearchIterationStatus.ABANDONED
                        and existing_item.get("status") in {
                            ResearchIterationStatus.ABANDONED.value,
                            ResearchIterationStatus.WAITING_FOR_USER.value,
                        }
                    ):
                        refreshed = ResearchIteration(
                            iteration_id=str(existing_item.get("iteration_id") or f"ITER-{logical_run_id}"),
                            sequence=int(existing_item.get("sequence", 1)),
                            title=str(existing_item.get("title") or f"第 {int(existing_item.get('sequence', 1))} 轮：研究推进"),
                            status=next_status,
                            focus_question_id=(
                                str(existing_item["focus_question_id"])
                                if existing_item.get("focus_question_id") is not None
                                else None
                            ),
                            run_id=logical_run_id,
                            summary=normalized or str(existing_item.get("summary") or ""),
                            evidence_ids=tuple(
                                str(item.get("evidence_id"))
                                for item in (state.get("evidence") or [])
                                if isinstance(item, dict) and item.get("evidence_id")
                            ),
                            candidate_question_ids=candidate_ids,
                            decision="等待用户确认下一轮研究方向" if awaiting_focus else "",
                            next_step=(
                                "用户选择候选焦点后继续"
                                if awaiting_focus
                                else "基于本轮证据决定是否扩展研究"
                            ),
                        )
                        result = workspace_tools.update_research_iteration(refreshed)
                        if result.get("status") == "awaiting_decision":
                            decision_id = result.get("decision_point_id")
                            if decision_id:
                                workspace_tools._pending_decision_id = str(decision_id)
                    return
                sequence = max(
                    (int(item.get("sequence", 0)) for item in iterations if isinstance(item, dict)),
                    default=0,
                ) + 1
                title_text = " ".join(str(run_input.question or "本轮研究").split())
                if len(title_text) > 72:
                    title_text = title_text[:69].rstrip() + "…"
                iteration = ResearchIteration(
                    iteration_id=f"ITER-{logical_run_id}",
                    sequence=sequence,
                    title=f"第 {sequence} 轮：{title_text or '研究推进'}",
                    status=next_status,
                    focus_question_id=active_focus_id,
                    run_id=logical_run_id,
                    summary=normalized or "本轮运行未生成可展示的阶段摘要。",
                    evidence_ids=tuple(
                        str(item.get("evidence_id"))
                        for item in (state.get("evidence") or [])
                        if isinstance(item, dict) and item.get("evidence_id")
                    ),
                    candidate_question_ids=candidate_ids,
                    decision="等待用户确认下一轮研究方向" if awaiting_focus else "",
                    next_step=(
                        "用户选择候选焦点后继续"
                        if awaiting_focus
                        else "基于本轮证据决定是否扩展研究"
                    ),
                )
                result = workspace_tools.append_research_iteration(iteration)
                if result.get("status") == "awaiting_decision":
                    # This should not happen for the metadata-only append, but
                    # preserve the decision id if a custom pipeline classifies
                    # it conservatively.
                    decision_id = result.get("decision_point_id")
                    if decision_id:
                        workspace_tools._pending_decision_id = str(decision_id)
            except Exception:
                # A timeline receipt must never turn a valid answer into a
                # runtime failure.  Canonical evidence and map writes remain
                # authoritative when a deployment cannot persist the receipt.
                return

        try:
            emit("run_started", "探索已开始", stable_ids={"run_id": run_input.run_id})
            cancelled = guard.snapshot()
            if cancelled.status == "cancelled":
                raise RunTerminated("cancelled")
            facade = self._tools_factory(run_input.run_id, status_reader)
            workspace_tools = (
                self._workspace_tools_factory(run_input.run_id) if self._workspace_tools_factory else None
            )
            resolved = run_input.resolved or {}
            synthesis_only = resolved.get("recovery_strategy") == "synthesize_from_persisted_evidence"
            retrieval_plan = str(resolved.get("retrieval_plan") or "")
            if synthesis_only:
                workspace_tools = None
            if workspace_tools is not None and retrieval_plan != "direct":
                workspace_tools.ensure_initial_research()
                # A direction decision is resolved between runs.  The first
                # user turn after choosing an active focus is the explicit
                # transition into INVESTIGATING; do not leave the workspace
                # visibly stuck in the waiting state.
                workspace_tools.begin_investigation()
            mode = str(resolved.get("mode")) if resolved.get("mode") else None
            self._preflight_effective_tools(
                facade,
                run_input.allowed_tools,
                workspace_tools=workspace_tools,
                assistant_=self._assistant,
                mode=mode,
                synthesis_only=synthesis_only,
                retrieval_plan=retrieval_plan,
            )
            raw_model = self._model_factory(guard)
            model_name = "research-assistant" if self._assistant else "deepseek-v4-flash"
            model = raw_model if isinstance(raw_model, BudgetedChatModel) else BudgetedChatModel(
                delegate=raw_model,
                enforcer=guard,
                max_output_tokens=(
                    research_provider_output_limit(
                        experimental_mode=run_input.budgets.experimental_unbounded
                    )
                    if run_input.budgets.experimental_unbounded
                    else min(1000, run_input.budgets.output_tokens)
                ),
                model_name=model_name,
            )
            initial_search_calls = int(resolved.get("resume_search_calls", 0) or 0) if isinstance(resolved, dict) else 0
            graph = self._build_graph(
                model,
                facade,
                guard,
                emit,
                run_id=run_input.run_id,
                workspace_tools=workspace_tools,
                mode=mode,
                initial_search_calls=initial_search_calls,
                synthesis_only=synthesis_only,
                retrieval_plan=retrieval_plan,
            )
            # Scope-aware context injection: the resolved evidence (selection
            # text / anchor paper) is folded in as a privileged leading system
            # message so the model starts with real state; the stored question
            # (and therefore the echo) stays clean.
            messages: list[dict[str, str]] = [{"role": "user", "content": run_input.question}]
            context_block = resolved.get("context_block")
            if context_block:
                messages.insert(0, {"role": "system", "content": str(context_block)})
            recovery_block = self._recovery_context_block(resolved)
            if recovery_block:
                messages.insert(0, {"role": "system", "content": recovery_block})
            output = graph.invoke({"messages": messages})
            if (
                self._assistant
                and workspace_tools is not None
                and retrieval_plan == "research_exploration"
            ):
                try:
                    if workspace_tools.ensure_initial_map():
                        emit(
                            "phase_changed",
                            "已补齐初始研究地图，准备请求方向确认",
                            stable_ids={"run_id": run_input.run_id},
                        )
                except Exception:
                    # A fallback map is helpful but must never turn an
                    # otherwise valid answer into a hard runtime failure.
                    pass
            final = self._final_text(output.get("messages", ()))
            guard.complete()
            if workspace_tools is not None:
                record_iteration(ResearchIterationStatus.COMPLETED, final)
                # A hitl write raised a patch_approval decision — stop and wait.
                if workspace_tools.pending_decision_id is not None:
                    emit(
                        "awaiting_decision",
                        "研究需要用户决定（请审阅研究判断变更）",
                        stable_ids={
                            "run_id": run_input.run_id,
                            "decision_id": workspace_tools.pending_decision_id,
                        },
                    )
                    return AgentRunResult(final, "awaiting_decision")
                # A bounded research round is complete; stop at the next
                # direction checkpoint so the user can decide whether to
                # continue into another round.
                direction_decision_id = (
                    workspace_tools.report_direction_decision(allow_later=False)
                    if self._assistant else None
                )
                if direction_decision_id is not None:
                    emit(
                        "awaiting_decision",
                        "已形成初始理解；等待用户选择研究方向",
                        stable_ids={
                            "run_id": run_input.run_id,
                            "decision_id": direction_decision_id,
                        },
                    )
                    return AgentRunResult(final, "awaiting_decision")
            emit("final_draft", "探索草稿已生成", stable_ids={"run_id": run_input.run_id})
            return AgentRunResult(final, "completed")
        except RunTerminated as exc:
            if (
                exc.reason == "budget_exhausted"
                and self._assistant
                and workspace_tools is not None
                and retrieval_plan == "research_exploration"
            ):
                try:
                    if workspace_tools.ensure_initial_map():
                        emit(
                            "phase_changed",
                            "已补齐初始研究地图，准备请求方向确认",
                            stable_ids={"run_id": run_input.run_id},
                        )
                    record_iteration(
                        ResearchIterationStatus.ABANDONED,
                        self._partial_final(context_was_read),
                    )
                    # The report is safe to create only when the orchestrator
                    # sees a complete initial graph (map + open candidate).
                    # If writes are still incomplete, leave the run resumable
                    # as budget_exhausted and let Continue finish the graph.
                    if workspace_tools.pending_decision_id is None:
                        workspace_tools.report_direction_decision(allow_later=False)
                    if workspace_tools.pending_decision_id is not None:
                        emit(
                            "awaiting_decision",
                            "已形成初始理解；等待用户选择研究方向",
                            stable_ids={
                                "run_id": run_input.run_id,
                                "decision_id": workspace_tools.pending_decision_id,
                            },
                        )
                        return AgentRunResult(
                            self._direction_checkpoint_final(workspace_tools, context_was_read),
                            "awaiting_decision",
                        )
                except Exception:
                    # Never mask the original budget outcome if the graph is
                    # incomplete or a decision cannot be created yet.
                    pass
            if workspace_tools is not None:
                record_iteration(
                    ResearchIterationStatus.ABANDONED,
                    self._partial_final(context_was_read),
                )
            event_type = "run_cancelled" if exc.reason == "cancelled" else "run_failed"
            emit(event_type, "探索已停止" if exc.reason == "cancelled" else "探索预算已耗尽")
            return AgentRunResult(self._partial_final(context_was_read), exc.reason)  # type: ignore[arg-type]
        except CapabilityPreflightError:
            if retrieval_plan == "web_lookup":
                emit("run_failed", "网页检索未配置或当前不可用，请配置网页搜索 Provider")
            else:
                emit("run_failed", "Harness 工具能力校验失败")
            return AgentRunResult(self._partial_final(context_was_read), "failed")
        except Exception as exc:
            # A provider can reject the final synthesis request after the
            # initial graph has already been durably committed (for example a
            # transient OpenAI-compatible 400 caused by a malformed tool
            # transcript).  Do not turn that useful checkpoint into a terminal
            # failure: if map + candidate questions exist, expose the same
            # explicit direction decision as the normal completion path.  The
            # user can then choose a focus and the next run will continue from
            # persisted evidence.  When the graph is incomplete, retain the
            # original terminal diagnostic.
            if (
                self._assistant
                and workspace_tools is not None
                and retrieval_plan == "research_exploration"
            ):
                try:
                    if workspace_tools.ensure_initial_map():
                        emit(
                            "phase_changed",
                            "已补齐初始研究地图，准备请求方向确认",
                            stable_ids={"run_id": run_input.run_id},
                        )
                    # A provider/tool exception can happen after the
                    # continuation has entered the investigation phase but
                    # before the normal finalisation path runs.  Persist the
                    # bounded round receipt before creating the next
                    # direction checkpoint; otherwise the UI jumps straight
                    # back to the previous round and loses the fact that this
                    # attempt was actually tried and abandoned.
                    record_iteration(
                        ResearchIterationStatus.ABANDONED,
                        self._partial_final(context_was_read),
                    )
                    if workspace_tools.pending_decision_id is None:
                        workspace_tools.report_direction_decision(allow_later=False)
                    if workspace_tools.pending_decision_id is not None:
                        emit(
                            "awaiting_decision",
                            "已形成初始理解；等待用户选择研究方向",
                            stable_ids={
                                "run_id": run_input.run_id,
                                "decision_id": workspace_tools.pending_decision_id,
                            },
                        )
                        return AgentRunResult(
                            self._direction_checkpoint_final(workspace_tools, context_was_read),
                            "awaiting_decision",
                        )
                except Exception:
                    # Keep the original provider diagnostic when no complete
                    # checkpoint can be formed.
                    pass
            if workspace_tools is not None:
                record_iteration(
                    ResearchIterationStatus.ABANDONED,
                    self._partial_final(context_was_read),
                )
            emit("run_failed", f"外部服务或 Harness 执行失败（{type(exc).__name__}）")
            return AgentRunResult(self._partial_final(context_was_read), "failed")
    @classmethod
    def _preflight_effective_tools(
        cls, facade: ReadOnlyResearchTools, allowed_tools: Sequence[str],
        *, workspace_tools: "WorkspaceResearchTools | None" = None, assistant_: bool = False,
        mode: str | None = None, synthesis_only: bool = False,
        retrieval_plan: str = "",
    ) -> tuple[str, ...]:
        probe = EffectiveToolProbeModel(
            model_name="research-assistant" if assistant_ else "deepseek-v4-flash",
        )
        guard = BudgetEnforcer(RunBudgets(1, 1, 1, 30, 1000, 1000))
        graph = cls(
            model_factory=lambda _guard: probe,
            tools_factory=lambda _run_id, _status: facade,
            assistant=assistant_,
        )._build_graph(
            probe, facade, guard, lambda *args, **kwargs: None,
            run_id="preflight", workspace_tools=workspace_tools, mode=mode,
            synthesis_only=synthesis_only,
            retrieval_plan=retrieval_plan,
        )
        graph.invoke({"messages": [{"role": "user", "content": "capability preflight"}]})
        inventory = probe.recorded_tool_names
        if synthesis_only:
            inventory_caps = tuple(ToolCapability(name, True) for name in inventory)
            verify_capability_policy(
                synthesis_capability_policy(),
                inventory_caps,
                criteria={"workspace_present": False, "importer_present": False},
            )
        elif retrieval_plan == "direct":
            inventory_caps = tuple(ToolCapability(name, True) for name in inventory)
            verify_capability_policy(
                direct_capability_policy(),
                inventory_caps,
                criteria={"workspace_present": workspace_tools is not None},
            )
        elif retrieval_plan == "web_lookup":
            inventory_caps = tuple(ToolCapability(name, True) for name in inventory)
            verify_capability_policy(
                web_lookup_capability_policy(),
                inventory_caps,
                criteria={"workspace_present": workspace_tools is not None},
            )
        elif assistant_:
            # Assistant surface: verify the dynamically-built actual surface against
            # the fixed CapabilityPolicy. import_supporting_paper is conditional
            # (clean absence legal when no importer is wired); the workspace write
            # tools are conditional on a workspace being bound, so a fresh session
            # with no paper gets the read-only surface rather than a blocked run.
            # A resolved paper-local mode is verified against its own (narrower)
            # policy: the search tools become merely allowed, so a surface without
            # them is legal — the whole point of a single-paper ask.
            inventory_caps = tuple(ToolCapability(name, True) for name in inventory)
            importer_present = "import_supporting_paper" in inventory
            workspace_present = workspace_tools is not None
            verify_capability_policy(
                (
                    paper_evidence_capability_policy()
                    if retrieval_plan == "paper_evidence"
                    else capability_policy_for_mode(mode)
                ), inventory_caps,
                criteria={
                    "importer_present": importer_present,
                    "workspace_present": workspace_present,
                },
            )
        else:
            # Literature/V0 surface keeps the exact read-only inventory check.
            inventory_caps = tuple(ToolCapability(name, True) for name in inventory)
            require_exact_readonly_capabilities(inventory_caps, allowed_tools)
        return tuple(inventory)

    def _build_graph(
        self,
        model: BaseChatModel,
        facade: ReadOnlyResearchTools,
        guard: BudgetEnforcer,
        emit: Callable[..., None],
        *,
        run_id: str = "",
        workspace_tools: "WorkspaceResearchTools | None" = None,
        mode: str | None = None,
        initial_search_calls: int = 0,
        synthesis_only: bool = False,
        retrieval_plan: str = "",
    ) -> Any:
        register_readonly_profile()
        register_assistant_profile()
        from deepagents.backends import FilesystemBackend
        from langchain.agents.middleware import TodoListMiddleware

        # Assistant profile: workspace files persist in the session directory
        # (resolved per run); the plain literature profile keeps the ephemeral
        # state backend. Neither implements SandboxBackendProtocol, so `execute`
        # never materializes.
        backend: Any = StateBackend()
        if self._assistant:
            workspace_root = self._workspace_resolver(run_id) if self._workspace_resolver else None
            if workspace_root:
                backend = FilesystemBackend(root_dir=workspace_root, virtual_mode=True, max_file_size_mb=10)
        if isinstance(backend, SandboxBackendProtocol):
            raise RuntimeError("exploration backend must not support shell execution")

        def _emit_file_event(action: str, path: str) -> None:
            emit("file_written", f"{action} 工作区文件", stable_ids={"tool_name": action, "file_path": path})

        effective_backend = PathScopedBackend(backend, _emit_file_event) if self._assistant else backend
        search_state = {
            "calls": max(0, initial_search_calls),
            "failed_calls": 0,
            "disabled": False,
        }
        web_state = {
            "attempts": 0,
            "consecutive_failures": 0,
            "consecutive_empty": 0,
            "disabled": False,
            "seen_queries": set(),
        }
        workspace_state = {
            "write_calls": 0,
            "write_failures": 0,
            "state_reads": 0,
            "deferred_writes": [],
            "completed_write_keys": set(),
        }
        # ``experimental_unbounded`` is the explicit local-test mode exposed
        # by the run budget UI.  It removes application budgets (including
        # tool calls and token counters), so a second, hidden 20-write budget
        # would make that mode misleading and would truncate otherwise valid
        # upper-bound runs.  Production keeps the guard; experimental runs
        # retain wall-clock/provider safety without this application cap.
        def workspace_write_limit_reached() -> bool:
            return (
                not guard.limits.experimental_unbounded
                and workspace_state["write_calls"] >= _MAX_WORKSPACE_WRITES_PER_ATTEMPT
            )
        # LangGraph may dispatch several tool calls from one assistant message
        # concurrently.  Workspace patches use an optimistic revision, so
        # concurrent commits would all read the same revision and all but the
        # first would fail spuriously.  Serialize only the controlled writes;
        # read-only retrieval remains concurrent/unchanged.
        workspace_write_lock = RLock()
        dispatcher = ToolDispatcher(build_workbench_tool_registry(
            facade,
            workspace_tools,
            workspace_id=workspace_tools.workspace_id if workspace_tools is not None else None,
        ))

        def dispatched_value(tool_name: str, args: dict[str, Any]) -> tuple[object | None, str | None]:
            execution = dispatcher.execute(
                attempt_id=run_id or "legacy-attempt",
                tool_call_id=str(uuid4()),
                tool_name=tool_name,
                arguments=args,
            )
            if execution.outcome.status is ToolOutcomeStatus.SUCCEEDED:
                return execution.ephemeral_value, None
            return None, dispatcher.error_views(execution.outcome).model

        def dispatched_workspace_value(tool_name: str, args: dict[str, Any]) -> tuple[object | None, str | None]:
            if tool_name in _WORKSPACE_WRITE_TOOLS:
                with workspace_write_lock:
                    return dispatched_value(tool_name, args)
            return dispatched_value(tool_name, args)

        def deferred_write_key(tool_name: str, args: dict[str, Any]) -> str:
            """Return a stable key for one pending workspace mutation.

            The model can repeat the same write after receiving a recoverable
            gate error.  Deduplicating by the logical object id keeps retries
            bounded without making the model know about optimistic revisions.
            """
            identity_fields = {
                "update_subquestions": ("question_id", "operation"),
                "add_evidence": ("evidence_id",),
                "update_research_map": ("node_id",),
                "update_research_plan": ("object_id",),
                "import_supporting_paper": ("arxiv_id", "url", "source_id"),
            }.get(tool_name, ())
            identity = tuple(str(args.get(field) or "") for field in identity_fields)
            identity_text = "|".join(identity) if any(identity) else "-"
            # Include the payload digest so a legitimate update to the same
            # object is not swallowed by the idempotency guard, while exact
            # repeats from a parallel/model retry collapse to one operation.
            payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
            return f"{tool_name}:{identity_text}:{digest}"

        def should_defer_workspace_error(error: str) -> bool:
            """Only retry dependency/revision failures, never malformed input."""
            normalized = error.lower()
            return any(
                marker in normalized
                for marker in (
                    # ToolDispatcher intentionally redacts the concrete gate
                    # reason from the model-facing message.  Its safe
                    # ``INVALID_ARGUMENT`` wording also covers a temporary
                    # unknown-reference failure, so retain it for one bounded
                    # dependency retry instead of treating it as permanent.
                    "参数无效",
                    "前置条件无效",
                    "unknown subquestion",
                    "unknown evidence",
                    "references unknown",
                    "stale revision",
                    "revision conflict",
                    "workspace revision",
                )
            )

        def enqueue_deferred_write(tool_name: str, args: dict[str, Any], error: str) -> None:
            if not should_defer_workspace_error(error):
                return
            key = deferred_write_key(tool_name, args)
            pending = workspace_state["deferred_writes"]
            if any(item.get("key") == key for item in pending):
                return
            # Keep arguments isolated from the model's mutable tool payload and
            # cap the queue so a pathological turn cannot grow unbounded state.
            if len(pending) >= 24:
                return
            pending.append({"key": key, "tool_name": tool_name, "args": dict(args), "attempts": 0})

        def sanitized_workspace_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
            """Drop only unresolved relationship ids before a bounded retry.

            The model often plans evidence and map links in the same assistant
            message.  A link to a question/evidence that lost the arrival race
            is not a reason to discard the whole durable artifact.  The domain
            gate still validates the sanitized patch; this helper merely keeps
            the already-known ids and leaves the unresolved relationship for a
            later map update.
            """
            if workspace_tools is None:
                return None
            if tool_name not in {"add_evidence", "update_research_map"}:
                return None
            try:
                state = workspace_tools.read_workspace_state()
            except Exception:
                return None
            known_questions = {
                str(item.get("question_id"))
                for item in (state.get("subquestions") or [])
                if isinstance(item, dict) and item.get("question_id")
            }
            known_evidence = {
                str(item.get("evidence_id"))
                for item in (state.get("evidence") or [])
                if isinstance(item, dict) and item.get("evidence_id")
            }
            sanitized = dict(args)
            changed = False
            if tool_name == "add_evidence":
                values = args.get("supports_question_ids")
                if isinstance(values, list):
                    filtered = [str(value) for value in values if str(value) in known_questions]
                    if filtered != values:
                        sanitized["supports_question_ids"] = filtered
                        changed = True
            else:
                values = args.get("related_question_ids")
                if isinstance(values, list):
                    filtered = [str(value) for value in values if str(value) in known_questions]
                    if filtered != values:
                        sanitized["related_question_ids"] = filtered
                        changed = True
                values = args.get("evidence_ids")
                if isinstance(values, list):
                    filtered = [str(value) for value in values if str(value) in known_evidence]
                    if filtered != values:
                        sanitized["evidence_ids"] = filtered
                        changed = True
            return sanitized if changed else None

        def flush_deferred_writes() -> None:
            """Retry dependency-ordered writes after a successful commit.

            LangGraph may submit a map, evidence and question mutation in one
            batch.  The gate correctly rejects references that do not exist in
            the current snapshot; this bounded queue turns that arrival-order
            race into a deterministic dependency retry without bypassing the
            gate, risk classifier or commit service.
            """
            with workspace_write_lock:
                pending = list(workspace_state["deferred_writes"])
                workspace_state["deferred_writes"].clear()
                while pending and not workspace_write_limit_reached():
                    next_round: list[dict[str, Any]] = []
                    made_progress = False
                    for item in pending:
                        if workspace_write_limit_reached():
                            next_round.append(item)
                            continue
                        tool_name = str(item.get("tool_name") or "")
                        args = dict(item.get("args") or {})
                        workspace_state["write_calls"] += 1
                        guard.authorize_tool(tool_name, block_reads=0)
                        emit(
                            "tool_started",
                            f"延迟重试 workspace 工具 {tool_name}",
                            stable_ids={"tool_name": tool_name},
                        )
                        value, error = dispatched_workspace_value(tool_name, args)
                        if error is not None:
                            workspace_state["write_failures"] += 1
                            item["attempts"] = int(item.get("attempts") or 0) + 1
                            if item["attempts"] < 3 and should_defer_workspace_error(error):
                                next_round.append(item)
                            emit(
                                "tool_completed",
                                f"延迟 workspace 工具 {tool_name} 调用失败：{str(error)[:180]}",
                                stable_ids={"tool_name": tool_name},
                            )
                            continue
                        made_progress = True
                        workspace_state["completed_write_keys"].add(str(item.get("key") or ""))
                        emit(
                            "tool_completed",
                            f"延迟 workspace 工具 {tool_name} 已完成",
                            stable_ids={"tool_name": tool_name},
                        )
                        guard.ensure_running()
                    if not made_progress:
                        # Dependencies are still missing (or the error is
                        # transient); preserve the bounded entries for a later
                        # successful write in this same attempt.
                        workspace_state["deferred_writes"].extend(next_round)
                        return
                    pending = next_round
                workspace_state["deferred_writes"].extend(pending)

        def invoke(name: str, args: dict[str, Any]) -> str:
            read_requested = 0
            read_truncated = False
            block_count = 0
            if name == "read_managed_blocks":
                raw_block_ids = args.get("block_ids", ())
                if isinstance(raw_block_ids, list):
                    read_requested = len(raw_block_ids)
                    remaining = min(
                        guard.remaining("block_reads"),
                        max(0, _MAX_BLOCKS_PER_ATTEMPT - guard.snapshot().block_reads),
                    )
                    if remaining <= 0:
                        # Do not flip the attempt to budget_exhausted for a
                        # request that cannot fit. Return a deterministic
                        # instruction to synthesize from already-read blocks;
                        # the model can finish the turn without another large
                        # read and the persisted run remains resumable.
                        guard.authorize_tool(name, block_reads=0)
                        emit("tool_started", f"调用只读工具 {name}", stable_ids={"tool_name": name})
                        emit(
                            "tool_completed",
                            "证据读取预算已用尽，请基于已有证据总结",
                            stable_ids={"tool_name": name},
                        )
                        return json.dumps({
                            "error": "evidence read budget is exhausted; synthesize from already-read evidence",
                            "read_budget_exhausted": True,
                        }, ensure_ascii=False)
                    allowed = min(read_requested, _MAX_BLOCKS_PER_READ, remaining)
                    if allowed < read_requested:
                        args = dict(args)
                        args["block_ids"] = raw_block_ids[:allowed]
                        read_truncated = True
                    block_count = allowed
            guard.authorize_tool(name, block_reads=block_count)
            emit("tool_started", f"调用只读工具 {name}", stable_ids={"tool_name": name})
            if name == "search_arxiv":
                raw_query = args.get("query")
                if not isinstance(raw_query, str) or not raw_query.strip():
                    emit("tool_completed", "search_arxiv 缺少非空 query", stable_ids={"tool_name": name})
                    return json.dumps({
                        "error": "search_arxiv requires a non-empty string query. "
                        "Correct call shape: search_arxiv(query='direct preference optimization for recommendation', max_results=5)"
                    })
                if search_state["disabled"]:
                    message = "search_arxiv 本轮已因连续失败停止重试；请改用已有候选或网页检索"
                    emit("tool_completed", "arXiv 检索已停止重试", stable_ids={"tool_name": name})
                    return json.dumps({"error": message}, ensure_ascii=False)
                if search_state["calls"] >= _SEARCH_CALL_LIMIT:
                    message = f"search_arxiv 已达本次运行上限（{_SEARCH_CALL_LIMIT} 次）——停止检索，改为导入已发现候选并精读后直接总结"
                    emit("tool_completed", f"检索次数已达上限（{_SEARCH_CALL_LIMIT} 次），开始精读与总结", stable_ids={"tool_name": name})
                    return json.dumps({"error": message})
                search_state["failed_calls"] += 1
            if name == "search_web":
                raw_query = args.get("query")
                if not isinstance(raw_query, str) or not raw_query.strip():
                    emit("tool_completed", "search_web 缺少非空 query", stable_ids={"tool_name": name})
                    return json.dumps({"error": "search_web requires a non-empty string query."})
                normalized_query = " ".join(raw_query.split())[:500]
                if web_state["disabled"]:
                    message = "网页检索本轮已因连续失败或空结果停止重试；请基于当前结果回答并说明证据边界"
                    emit("tool_completed", "网页检索已停止重试", stable_ids={"tool_name": name})
                    return json.dumps({"error": message}, ensure_ascii=False)
                if normalized_query in web_state["seen_queries"]:
                    message = "网页检索已跳过重复 query；请改用不同且更聚焦的查询，或直接总结当前结果"
                    emit("tool_completed", "网页检索跳过重复 query", stable_ids={"tool_name": name})
                    return json.dumps({"error": message}, ensure_ascii=False)
                if web_state["attempts"] >= _WEB_SEARCH_ATTEMPT_LIMIT:
                    message = f"网页检索已达本次运行上限（{_WEB_SEARCH_ATTEMPT_LIMIT} 次），停止检索并总结当前结果"
                    web_state["disabled"] = True
                    emit("tool_completed", "网页检索次数已达上限", stable_ids={"tool_name": name})
                    return json.dumps({"error": message}, ensure_ascii=False)
                web_state["seen_queries"].add(normalized_query)
                web_state["attempts"] += 1
            value, error = dispatched_value(name, args)
            if error is not None:
                failure_extras: list[dict[str, object]] | None = None
                if name == "search_web":
                    usage = getattr(facade, "last_web_search_usage", None)
                    if callable(usage):
                        usage = usage()
                    if isinstance(usage, dict):
                        failure_extras = [usage]
                    if "returned no results" in error.lower() or "未返回结果" in error:
                        web_state["consecutive_empty"] += 1
                        web_state["consecutive_failures"] = 0
                        if web_state["consecutive_empty"] >= _WEB_SEARCH_FAILURE_LIMIT:
                            web_state["disabled"] = True
                            emit(
                                "tool_completed", "网页检索连续空结果，停止重试",
                                stable_ids={"tool_name": name},
                            )
                        else:
                            emit(
                                "tool_completed", "网页检索返回空结果，可改写一次 query",
                                stable_ids={"tool_name": name},
                            )
                    else:
                        web_state["consecutive_failures"] += 1
                        web_state["consecutive_empty"] = 0
                        if web_state["consecutive_failures"] >= _WEB_SEARCH_FAILURE_LIMIT:
                            web_state["disabled"] = True
                            emit(
                                "tool_completed", "网页检索连续失败，停止重试",
                                stable_ids={"tool_name": name},
                            )
                if name == "search_arxiv" and search_state["failed_calls"] >= _SEARCH_FAILURE_LIMIT:
                    search_state["disabled"] = True
                    emit(
                        "tool_completed", "arXiv 检索连续失败，停止重试",
                        stable_ids={"tool_name": name},
                    )
                emit(
                    "tool_completed", f"只读工具 {name} 调用失败",
                    stable_ids={"tool_name": name},
                    extras=failure_extras,
                )
                return json.dumps({"error": error}, ensure_ascii=False)
            if name == "search_web":
                web_state["consecutive_failures"] = 0
                web_state["consecutive_empty"] = 0
            if name == "search_arxiv":
                search_state["calls"] += 1
                search_state["failed_calls"] = 0
            if name == "search_sources":
                for item in value:
                    emit("source_discovered", "发现受管来源", stable_ids={"source_id": str(item["source_id"])})
            if name == "search_arxiv":
                for item in value:
                    if isinstance(item, dict) and item.get("source_id"):
                        emit(
                            "source_discovered", "发现 arXiv 候选来源",
                            stable_ids={
                                "source_id": str(item["source_id"]),
                                "source_kind": "external_candidate",
                                "title": str(item.get("title") or ""),
                                "url": str(item.get("url") or ""),
                                "relevance": f"{float(item.get('relevance') or 0.0):.0%}",
                            },
                        )
            if name == "search_web":
                for item in value:
                    if isinstance(item, dict) and item.get("url"):
                        emit(
                            "source_discovered", "发现网页来源",
                            stable_ids={
                                "source_id": str(item.get("url")),
                                "source_kind": "web_result",
                                "title": str(item.get("title") or "网页来源"),
                                "url": str(item.get("url")),
                            },
                        )
            if name == "read_managed_blocks":
                for item in value:
                    emit("context_read", "读取受管证据块", stable_ids={"block_id": str(item["block_id"]), "source_id": str(item["source_id"])})
            search_extras = (
                [_compact_candidate(item) for item in value[:10] if isinstance(item, dict)]
                if name in {"search_arxiv", "search_sources"} and isinstance(value, list)
                else None
            )
            if name == "search_web":
                usage = getattr(facade, "last_web_search_usage", None)
                if callable(usage):
                    usage = usage()
                if isinstance(usage, dict):
                    search_extras = list(search_extras or ())
                    search_extras.append(usage)
            emit(
                "tool_completed",
                (
                    f"只读工具 {name} 已完成（已按剩余预算读取 {block_count}/{read_requested} 个 block）"
                    if read_truncated else f"只读工具 {name} 已完成"
                ),
                stable_ids={"tool_name": name},
                extras=search_extras,
            )
            guard.ensure_running()
            if read_truncated:
                return json.dumps({
                    "items": value,
                    "truncated": True,
                    "requested": read_requested,
                    "read": block_count,
                    "message": "本次请求超过剩余证据预算，已读取前面的 block；请基于已有证据直接总结。",
                }, ensure_ascii=False)
            return json.dumps(value, ensure_ascii=False)

        def search_sources(query: str) -> str:
            return invoke("search_sources", {"query": query})

        def read_paper_metadata(source_id: str) -> str:
            return invoke("read_paper_metadata", {"source_id": source_id})

        def read_managed_blocks(source_id: str, block_ids: list[str]) -> str:
            return invoke("read_managed_blocks", {"source_id": source_id, "block_ids": block_ids})

        def read_run_status(run_id: str) -> str:
            return invoke("read_run_status", {"run_id": run_id})

        def search_arxiv(query: str, categories: list[str] | None = None, max_results: int = 5) -> str:
            return invoke("search_arxiv", {"query": query, "categories": categories, "max_results": max_results})

        def search_web(query: str, max_results: int = 5) -> str:
            return invoke("search_web", {"query": query, "max_results": max_results})

        def ws_invoke(tool_name: str, args: dict[str, Any]) -> str:
            with workspace_write_lock if tool_name in _WORKSPACE_WRITE_TOOLS else nullcontext():
                if tool_name in _WORKSPACE_WRITE_TOOLS:
                    key = deferred_write_key(tool_name, args)
                    pending_keys = {
                        str(item.get("key") or "")
                        for item in workspace_state["deferred_writes"]
                    }
                    if key in workspace_state["completed_write_keys"]:
                        guard.authorize_tool(tool_name, block_reads=0)
                        emit(
                            "tool_started", f"调用 workspace 工具 {tool_name}",
                            stable_ids={"tool_name": tool_name},
                        )
                        emit(
                            "tool_completed", f"workspace 工具 {tool_name} 已幂等完成",
                            stable_ids={"tool_name": tool_name},
                        )
                        return json.dumps({"status": "already_applied", "idempotent": True}, ensure_ascii=False)
                    if key in pending_keys:
                        guard.authorize_tool(tool_name, block_reads=0)
                        emit(
                            "tool_started", f"调用 workspace 工具 {tool_name}",
                            stable_ids={"tool_name": tool_name},
                        )
                        emit(
                            "tool_completed", f"workspace 工具 {tool_name} 已排队等待依赖完成",
                            stable_ids={"tool_name": tool_name},
                        )
                        return json.dumps({"status": "deferred", "idempotent": True}, ensure_ascii=False)
                    if workspace_write_limit_reached():
                        # This is a recoverable tool result, not a hard budget
                        # failure. The model can finish from durable writes.
                        guard.authorize_tool(tool_name, block_reads=0)
                        emit(
                            "tool_started", f"调用 workspace 工具 {tool_name}",
                            stable_ids={"tool_name": tool_name},
                        )
                        emit(
                            "tool_completed", "工作区写入已达到本轮上限，请基于已有证据直接总结",
                            stable_ids={"tool_name": tool_name},
                        )
                        return json.dumps({
                            "error": "workspace write limit reached; synthesize from persisted evidence",
                            "write_budget_exhausted": True,
                        }, ensure_ascii=False)
                    workspace_state["write_calls"] += 1
                guard.authorize_tool(tool_name, block_reads=0)
                if tool_name == "read_workspace_state":
                    workspace_state["state_reads"] += 1
                emit("tool_started", f"调用 workspace 工具 {tool_name}", stable_ids={"tool_name": tool_name})
                value, error = dispatched_workspace_value(tool_name, args)
                if error is not None:
                    if tool_name in _WORKSPACE_WRITE_TOOLS:
                        workspace_state["write_failures"] += 1
                        # Relationship references can arrive before their
                        # dependent writes.  Before queueing, make one bounded
                        # gate-validated retry with only ids already present in
                        # the canonical snapshot.  This preserves the useful
                        # evidence/map artifact instead of letting one stale
                        # link consume the whole write budget.
                        fallback_args = sanitized_workspace_args(tool_name, args)
                        if fallback_args is not None and not workspace_write_limit_reached():
                            workspace_state["write_calls"] += 1
                            guard.authorize_tool(tool_name, block_reads=0)
                            emit(
                                "tool_started",
                                f"重试 workspace 工具 {tool_name}（过滤未落盘关联）",
                                stable_ids={"tool_name": tool_name},
                            )
                            fallback_value, fallback_error = dispatched_workspace_value(tool_name, fallback_args)
                            if fallback_error is None:
                                workspace_state["completed_write_keys"].add(deferred_write_key(tool_name, args))
                                emit(
                                    "tool_completed",
                                    f"workspace 工具 {tool_name} 已完成（已保留已知关联）",
                                    stable_ids={"tool_name": tool_name},
                                )
                                guard.ensure_running()
                                flush_deferred_writes()
                                compact_result = _compact_workspace_result(tool_name, fallback_value)
                                if guard.remaining("input_tokens") < 60000:
                                    compact_result["next_step"] = "输入预算接近上限；停止调用 workspace 工具，立即生成最终草稿。"
                                return json.dumps(compact_result, ensure_ascii=False)
                        enqueue_deferred_write(tool_name, args, str(error))
                    # Keep the safe dispatcher message in the trace so a folded
                    # run-history panel can explain why a patch was rejected
                    # without exposing raw exceptions or provider payloads.
                    emit(
                        "tool_completed",
                        f"workspace 工具 {tool_name} 调用失败：{str(error)[:180]}",
                        stable_ids={"tool_name": tool_name},
                    )
                    return json.dumps({"error": error}, ensure_ascii=False)
                emit("tool_completed", f"workspace 工具 {tool_name} 已完成", stable_ids={"tool_name": tool_name})
                guard.ensure_running()
                if tool_name in _WORKSPACE_WRITE_TOOLS:
                    workspace_state["completed_write_keys"].add(deferred_write_key(tool_name, args))
                    # A successful dependent commit may make queued map/evidence
                    # writes valid.  Keep this inside the same lock so the
                    # revision snapshot and retry order stay deterministic.
                    flush_deferred_writes()
                # A full workspace snapshot or a write projection is useful for
                # persistence/UI, but returning it after every write makes the
                # next model prompt repeat all evidence and plans. Keep the
                # assistant-facing acknowledgement compact; canonical state
                # remains in the repository and is projected to the client later.
                if tool_name == "read_workspace_state":
                    return json.dumps(_compact_workspace_state(value), ensure_ascii=False)
                compact_result = _compact_workspace_result(tool_name, value)
                if guard.remaining("input_tokens") < 60000:
                    compact_result["next_step"] = "输入预算接近上限；停止调用 workspace 工具，立即生成最终草稿。"
                return json.dumps(compact_result, ensure_ascii=False)

        def read_workspace_state() -> str:
            return ws_invoke("read_workspace_state", {})

        def update_subquestions(
            operation: str,
            question_id: str,
            text: str | None = None,
            answer: str | None = None,
            researchability: str = "unknown",
        ) -> str:
            return ws_invoke(
                "update_subquestions",
                {
                    "operation": operation,
                    "question_id": question_id,
                    "text": text,
                    "answer": answer,
                    "researchability": researchability,
                },
            )

        def add_evidence(evidence_id: str, source_id: str, block_ids: list[str], supports_question_ids: list[str] | None = None, evidence_role: str = "supporting", claim: str = "", research_interpretation: str = "", confidence: str = "medium") -> str:
            return ws_invoke("add_evidence", {"evidence_id": evidence_id, "source_id": source_id, "block_ids": block_ids, "supports_question_ids": supports_question_ids, "evidence_role": evidence_role, "claim": claim, "research_interpretation": research_interpretation, "confidence": confidence})

        def update_research_map(node_id: str, label: str | None = None, related_question_ids: list[str] | None = None, evidence_ids: list[str] | None = None) -> str:
            return ws_invoke("update_research_map", {"node_id": node_id, "label": label, "related_question_ids": related_question_ids, "evidence_ids": evidence_ids})

        def update_research_plan(plan: dict) -> str:
            return ws_invoke("update_research_plan", {"plan": plan})

        def import_supporting_paper(arxiv_id: str | None = None, url: str | None = None, source_id: str | None = None, title: str | None = None) -> str:
            args = {
                "arxiv_id": arxiv_id, "url": url, "source_id": source_id, "title": title,
            }
            return ws_invoke("import_supporting_paper", {key: value for key, value in args.items() if value is not None})

        def structured_tool(function: Callable[..., str], *, name: str, description: str) -> StructuredTool:
            # Invalid model arguments are recoverable tool failures. Returning
            # them to the model lets it correct the call instead of aborting
            # the whole exploration with a Pydantic ValueError.
            return StructuredTool.from_function(
                function,
                name=name,
                description=description,
                handle_validation_error=True,
            )

        def workspace_agent_tools() -> list[StructuredTool]:
            if workspace_tools is None:
                return []
            tools_list = [
                structured_tool(read_workspace_state, name="read_workspace_state", description="Read the current research workspace state: research intent, active focus, research map, candidate questions, boundaries and evidence. Treat candidate questions as suggestions; do not promote one unless the user explicitly selects a focus."),
            ]
            if retrieval_plan not in {"direct", "web_lookup"}:
                tools_list.extend([
                    structured_tool(update_subquestions, name="update_subquestions", description="Apply ONE structured subquestion operation: add|update|resolve|deprioritize for a single question_id. For add/update, researchability must be candidate (testable follow-up), boundary (author-stated or out-of-scope limitation), or unknown; only candidate can trigger a follow-up exploration. Never overwrites other questions."),
                    structured_tool(add_evidence, name="add_evidence", description="Record evidence that a source supports a research question. Requires source_id and block_ids."),
                    structured_tool(update_research_map, name="update_research_map", description="Add or update a Research Map node by stable node_id, referencing related_question_ids / evidence_ids."),
                    structured_tool(
                        update_research_plan,
                        name="update_research_plan",
                        description=(
                            "Replace the typed research plan atomically. Only call this after the user has "
                            "selected an active research focus (active_focus_id is non-empty); during the "
                            "initial paper pass, do not call it. The smallest valid payload is "
                            "{\"plan\":{\"stage\":\"method_mapping\"}}. Allowed stages are "
                            "not_started, method_mapping, hypothesis_review, experiment_planning, ready. "
                            "Optional sections must be arrays; method_map must be an object with an entries "
                            "array. The iterations array records each bounded research round; append to it "
                            "instead of replacing history. Each item uses iteration_id, sequence, title, "
                            "status (in_progress|waiting_for_user|completed|abandoned), optional "
                            "focus_question_id/run_id, summary, evidence_ids, candidate_question_ids, "
                            "decision and next_step. Draft changes are reviewable and selected/approved/ready changes require "
                            "user approval."
                        ),
                    ),
                ])
            if retrieval_plan not in {"direct", "web_lookup"} and "import_supporting_paper" in workspace_tools.capability_names:
                tools_list.append(structured_tool(
                    import_supporting_paper, name="import_supporting_paper",
                    description="Turn an arXiv candidate into a managed readable source. Requires arxiv_id or url.",
                ))
            return tools_list

        tools = [
            structured_tool(read_paper_metadata, name="read_paper_metadata", description="Read metadata and a full block index for one managed source."),
            structured_tool(read_managed_blocks, name="read_managed_blocks", description="Read specific managed blocks by full stable ids."),
            structured_tool(
                read_run_status,
                name="read_run_status",
                description=(
                    "Read safe counters and status for this run. Always pass the current run_id "
                    f"{run_id!r}; do not invent, replace, or omit it."
                ),
            ),
        ]
        if retrieval_plan == "web_lookup":
            # Web mode is intentionally isolated from the paper evidence
            # surface. This prevents a current-facts answer from accidentally
            # mixing attached blocks into a URL-backed response. It also does
            # not need read_run_status: the lifecycle state is already
            # projected by the workbench, and exposing it invites the model to
            # spend a tool call on a non-retrieval operation after a search
            # provider failure.
            tools = []
        search_enabled = retrieval_plan in {"web_lookup", "research_exploration"}
        if not retrieval_plan:
            search_enabled = mode != ResolveMode.PAPER_LOCAL and not synthesis_only
        if search_enabled and not synthesis_only:
            # Paper-local asks get the read surface only (no literature search):
            # the single paper's evidence is already the allowed scope, so the
            # model should not be handed tools to wander off into related work.
            if retrieval_plan == "web_lookup":
                if "search_web" in facade.capability_names:
                    tools = [
                        structured_tool(search_web, name="search_web", description="Search configured web sources and return bounded URL-backed results."),
                        *tools,
                    ]
            else:
                tools = [
                    structured_tool(search_sources, name="search_sources", description="Search the frozen managed paper catalog by keywords."),
                    *tools,
                    structured_tool(search_arxiv, name="search_arxiv", description="Search arXiv literature for new candidate papers."),
                ]
                if not retrieval_plan and not self._assistant and "search_web" in facade.capability_names:
                    tools.append(structured_tool(search_web, name="search_web", description="Search configured web sources and return bounded URL-backed results."))
        assistant_tools = tools if synthesis_only else tools + workspace_agent_tools()
        if self._assistant:
            return create_deep_agent(
                model=model,
                tools=assistant_tools,
                backend=effective_backend,
                checkpointer=None,
                middleware=[] if retrieval_plan == "direct" else [TodoListMiddleware()],
                system_prompt=_SYNTHESIS_PROMPT if synthesis_only else _assistant_prompt_for_mode(mode, retrieval_plan) or (
                    "You are a research assistant working for an AI researcher. "
                    "Goal: move from the research question towards usable experiment designs or paper drafts, "
                    "not a shallow literature summary. "
                    "Plan first with write_todos, keep todos updated as you progress. "
                    "When the user's question is a focused passage-level ask (translate / explain / critique "
                    "a passage) and the question already embeds the selected text plus its anchor, answer "
                    "directly from that provided text — do NOT launch a catalog/arXiv search to locate "
                    "material you were already given. Use read_managed_blocks(source_id, [block_id]) only "
                    "when you truly need the verbatim block to cite it, then answer. Reserve the full "
                    "plan → search → read workflow for open-ended research progression. "
                    "Likewise, when the question is about an attached anchor paper whose source_id you "
                    "were given, read that source directly with read_paper_metadata + read_managed_blocks — "
                    "never search_sources/search_arxiv merely to re-locate an already-anchored paper. "
                    "Only search for genuinely NEW related work. "
                    "Search discipline: searches are discovery, not output — each search_arxiv call can "
                    "return many candidates and each one costs context. Use at most 4 search_arxiv calls in "
                    "the whole run. Rank results by their relevance score; surface the top candidates "
                    "(title + relevance) for the user and NEVER download/import/parse papers yourself — "
                    "adding a candidate only happens when the user clicks ＋ on it (async download + parse). "
                    "Read the anchor paper and any already-parsed papers; stop and synthesize once you have "
                    "enough evidence — endless searching exhausts the budget and ends the run without a draft. "
                    "Evidence discipline: in the first pass read no more than 12 of the most relevant blocks; "
                    "prefer abstract, method, results and limitations, then synthesize instead of enumerating "
                    "the entire block index. Keep controlled workspace writes minimal (at most a few durable "
                    "records per category); once the evidence boundary is recorded, stop writing and produce "
                    "the user-facing draft. "
                    "NOTE: the anchor source_id of the attached paper equals its paper_id; the supplied "
                    "block_id is a valid managed block under that source_id. "
                    "Search the managed catalog and arXiv for related work; read managed blocks for evidence "
                    "and cite them with the FULL block id of every block you actually read; each full id alone in its own square brackets; never abbreviate an id, never merge ids into one bracket, never invent an id for text you did not read. "
                    "Update your persistent research state ONLY through the controlled tools: read_workspace_state, "
                    "update_subquestions, add_evidence, update_research_map, update_research_plan — never write workspace.json, "
                    "research-map.json, subquestions.json, research-plan.json or evidence/*.json directly. "
                    "Treat the research map, subquestions, typed research plan and evidence as the durable record of your research. "
                    "The user interface is Chinese-first: write research questions, map labels, subquestion text, "
                    "evidence claims, interpretations, and direction recommendations in Simplified Chinese. "
                    "Keep essential technical terms or the original English in parentheses after the Chinese wording; "
                    "do not write a bare English-only workspace record. "
                    "You may draft freeform notes under scratch/notes/drafts via write_file/edit_file. "
                    "Do not turn every author-stated limitation into a follow-up question. When adding a "
                    "subquestion, set researchability='candidate' only if it is a testable question that can "
                    "be checked through related literature, data, or an experiment. Set researchability='boundary' "
                    "for an explicit paper limitation, unavailable evaluation, or item the authors place outside "
                    "scope; set 'unknown' when you cannot judge. Only candidate questions may trigger a "
                    "continuation suggestion. When you reach an initial understanding plus a research map plus subquestions, report to the "
                    "user what you understand, which questions are most worth pursuing, and your recommended direction. "
                    "Research is iterative: do not treat one active focus as the final research question. After each "
                    "bounded round, append one item to the research plan's iterations array (keep the existing items) "
                    "with a Chinese-first title, sequence, status, current run_id, summary, evidence_ids, candidate_question_ids, "
                    "decision, and next_step. A continuation must start from the previous round's unresolved questions "
                    "and evidence, then produce new candidates or revise the focus; do not restart from scratch. "
                    "After a later round, if you have produced a genuinely new candidate question, finish the bounded "
                    "draft and let the Harness present a low-priority continuation decision; do not silently switch "
                    "the active focus or auto-promote a limitation. "
                    "If a catalog search returns nothing or reports no managed papers, use search_arxiv "
                    "instead of retrying the catalog. "
                    "Label all outputs as exploration drafts pending verification. "
                    "Do not invent citations."
                ),
            )
        return create_deep_agent(
            model=model,
            tools=tools,
            backend=backend,
            checkpointer=None,
            system_prompt=_DIRECT_PROMPT if retrieval_plan == "direct" else (
                "You are a read-only research explorer. Use the supplied tools before answering. "
                "Use no more than two catalog searches. If a catalog search returns nothing or reports "
                "that no managed papers are attached, immediately use search_arxiv instead of retrying "
                "the catalog. Read metadata first, then use only full sample block ids "
                "returned for the same source. Synthesize once enough evidence is read. "
                "Cite only blocks actually read, each full id alone in its own square brackets; never abbreviate an id, never merge ids into one bracket, never invent an id for text you did not read. "
                "Label the answer as an exploration draft. Do not invent citations."
            ),
        )

    @staticmethod
    def _recovery_context_block(resolved: dict[str, object]) -> str | None:
        evidence = resolved.get("evidence")
        read_items = [
            item for item in evidence
            if isinstance(item, dict) and item.get("read")
        ] if isinstance(evidence, list) else []
        candidates = resolved.get("search_candidates")
        candidate_items = [
            item for item in candidates if isinstance(item, dict)
        ] if isinstance(candidates, list) else []
        if not read_items and not candidate_items:
            return None
        lines = ["恢复上下文：以下内容来自上一轮持久化结果。"]
        if read_items:
            lines.append("已读证据（可以作为引用依据）：")
        for item in read_items[:32]:
            stable_id = str(item.get("stable_id") or "")
            source_id = str(item.get("source_id") or "")
            if stable_id:
                lines.append(f"- source_id={source_id or '由证据标识解析'} block_id={stable_id}")
        if candidate_items:
            lines.append("上一轮检索候选（未读取，不是证据；只能作为后续导入/精读线索）：")
            for item in candidate_items[:20]:
                source_id = str(item.get("source_id") or item.get("arxiv_id") or "")
                title = str(item.get("title") or "")[:240]
                abstract = str(item.get("abstract_preview") or "")[:360]
                relevance = item.get("relevance")
                lines.append(
                    f"- {source_id} | {title} | relevance={relevance} | abstract={abstract}"
                )
        return "\n".join(lines)

    @staticmethod
    def _final_text(messages: Sequence[BaseMessage]) -> str | None:
        for message in reversed(messages):
            if isinstance(message, AIMessage) and isinstance(message.content, str) and message.content.strip():
                return message.content
        return None

    @staticmethod
    def _partial_final(context_was_read: bool) -> str | None:
        return "部分探索结果可用；运行未完整完成。" if context_was_read else None

    @classmethod
    def _direction_checkpoint_final(
        cls,
        workspace_tools: "WorkspaceResearchTools",
        context_was_read: bool,
    ) -> str | None:
        """Build a useful user-facing checkpoint when synthesis hit a limit.

        A budget/provider stop can happen after the durable graph is complete but
        before the model emits its final prose.  Returning only the generic
        ``部分探索结果可用`` message makes a healthy direction checkpoint look
        like a failed run and leaves the user without the candidate they are
        expected to choose.  Re-read the bounded workspace projection and render
        only the candidate questions (never raw provider output or unverified
        evidence) so the next action is explicit and safe.
        """
        fallback = cls._partial_final(context_was_read)
        try:
            state = workspace_tools.read_workspace_state()
        except Exception:
            return fallback
        if not isinstance(state, dict):
            return fallback
        questions = state.get("subquestions")
        active_focus_id = str(state.get("active_focus_id") or "").strip()
        plan = state.get("research_plan")
        raw_iterations = plan.get("iterations") if isinstance(plan, dict) else []
        iterations = [item for item in raw_iterations if isinstance(item, dict)] if isinstance(raw_iterations, list) else []
        latest_sequence = max(
            (int(item.get("sequence", 0)) for item in iterations),
            default=0,
        )
        candidates = [
            item for item in questions
            if isinstance(item, dict)
            and item.get("researchability") == "candidate"
            and item.get("status") == "open"
            and str(item.get("text") or "").strip()
            and not (
                active_focus_id
                and latest_sequence > 1
                and str(item.get("question_id") or "") == active_focus_id
            )
        ] if isinstance(questions, list) else []
        if not candidates:
            return fallback
        evidence = state.get("evidence")
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        # Keep the checkpoint copy aligned with the actual round.  Reusing the
        # initial-pass wording after a focus was selected makes a later
        # continuation look as if it restarted from scratch, even though the
        # workspace and evidence are cumulative.
        if active_focus_id and latest_sequence > 1:
            focus_text = next(
                (
                    str(item.get("text") or "").strip()
                    for item in (questions if isinstance(questions, list) else [])
                    if isinstance(item, dict) and str(item.get("question_id") or "") == active_focus_id
                ),
                "当前研究焦点",
            )
            lines = [f"第 {latest_sequence} 轮研究已保存阶段性结果。", f"当前焦点：{focus_text}"]
        else:
            lines = ["已完成论文初读，并保存了阶段性研究状态。"]
        if evidence_count:
            lines.append(f"已记录 {evidence_count} 条可追溯证据。")
        lines.append("系统识别到以下可验证研究方向，请选择一个后继续：")
        for item in candidates[:5]:
            lines.append(f"- {str(item['text']).strip()}")
        return "\n".join(lines)


def extract_actual_block_ids(events: Sequence[AgentEvent]) -> frozenset[str]:
    return frozenset(
        event.stable_ids["block_id"]
        for event in events
        if event.event_type == "context_read" and "block_id" in event.stable_ids
    )
