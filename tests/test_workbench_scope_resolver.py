from __future__ import annotations

import pytest

from research_pulse.workbench.scope_resolver import (
    EvidenceScope,
    InteractionContext,
    QueryIntent,
    RetrievalPlan,
    ResolveMode,
    ResolveSource,
    ResolvedRequest,
    ScopeResolver,
    SelectionAnchor,
)


def _resolver() -> ScopeResolver:
    return ScopeResolver()


def test_selection_resolves_to_paper_local_block_scope() -> None:
    ctx = InteractionContext(
        surface="paper_reader",
        canonical_paper_id="paper_123",
        selection=SelectionAnchor("paper_123", "B-42", "KNN 的最近邻检索", page=3, section_path=("方法",)),
    )
    resolved = _resolver().resolve("BSA 为什么使用 KNN？", ctx)
    assert resolved.mode == ResolveMode.PAPER_LOCAL
    assert resolved.scope.intent == QueryIntent.PAPER_QA
    assert resolved.scope.evidence_scope == EvidenceScope.CURRENT_PAPER
    assert resolved.scope.paper_ids == ("paper_123",)
    assert resolved.scope.selection is not None
    assert resolved.scope.selection.block_id == "B-42"
    assert resolved.scope.source == ResolveSource.UI_CONTEXT
    assert "KNN 的最近邻检索" in (resolved.context_block or "")
    assert "B-42" in (resolved.context_block or "")


def test_same_query_research_workspace_resolves_to_project_synthesis() -> None:
    ctx = InteractionContext(
        surface="research_workspace",
        research_question_id="rq_17",
    )
    resolved = _resolver().resolve("BSA 为什么使用 KNN？", ctx)
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.scope.evidence_scope == EvidenceScope.PROJECT
    assert resolved.scope.research_question_id == "rq_17"


def test_paper_open_in_research_workspace_is_paper_in_context() -> None:
    ctx = InteractionContext(
        surface="research_workspace",
        canonical_paper_id="paper_123",
        research_question_id="rq_17",
    )
    resolved = _resolver().resolve("这篇对我的问题有什么帮助？", ctx)
    assert resolved.mode == ResolveMode.PAPER_IN_RESEARCH_CONTEXT
    assert resolved.scope.evidence_scope == EvidenceScope.PROJECT
    assert resolved.scope.paper_ids == ("paper_123",)
    assert resolved.scope.research_question_id == "rq_17"


def test_whole_paper_reader_ask_is_paper_local() -> None:
    ctx = InteractionContext(surface="paper_reader", canonical_paper_id="paper_123")
    resolved = _resolver().resolve("这篇论文做了什么？", ctx)
    assert resolved.mode == ResolveMode.PAPER_LOCAL
    assert resolved.scope.paper_ids == ("paper_123",)
    assert resolved.scope.selection is None
    assert "source_id=paper_123" in (resolved.context_block or "")


def test_explicit_mode_overrides_all_context() -> None:
    ctx = InteractionContext(surface="paper_reader", canonical_paper_id="paper_123")
    resolved = _resolver().resolve(
        "比较这两篇", ctx,
        explicit_mode=ResolveMode.CROSS_PAPER_COMPARE,
        explicit_scope=EvidenceScope.PAPER_SET,
        explicit_paper_ids=("paper_1", "paper_2"),
    )
    assert resolved.mode == ResolveMode.CROSS_PAPER_COMPARE
    assert resolved.scope.evidence_scope == EvidenceScope.PAPER_SET
    assert resolved.scope.paper_ids == ("paper_1", "paper_2")
    assert resolved.scope.intent == QueryIntent.COMPARE
    assert resolved.scope.source == ResolveSource.EXPLICIT


def test_no_context_defaults_to_direct_answer_without_retrieval() -> None:
    ctx = InteractionContext(surface="global_chat")
    resolved = _resolver().resolve("LLM Agent 长期记忆的 consolidation 方法", ctx)
    assert resolved.mode == ResolveMode.DIRECT
    assert resolved.retrieval_plan == RetrievalPlan.DIRECT
    assert resolved.scope.evidence_scope == EvidenceScope.GLOBAL_LIBRARY
    assert resolved.scope.source == ResolveSource.AMBIGUOUS


def test_explicit_comparison_escalates_global_chat_to_research() -> None:
    resolved = _resolver().resolve(
        "比较推荐系统中的两条 RL 方法路线",
        InteractionContext(surface="global_chat"),
    )
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.retrieval_plan == RetrievalPlan.RESEARCH_EXPLORATION
    assert resolved.scope.source == ResolveSource.RESEARCH_ESCALATION


def test_explicit_web_lookup_uses_web_plan_without_arxiv_research_expansion() -> None:
    resolved = _resolver().resolve(
        "查网页上的 RLHF 最新消息",
        InteractionContext(surface="global_chat"),
    )
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.retrieval_plan == RetrievalPlan.WEB_LOOKUP
    assert resolved.scope.source == ResolveSource.RETRIEVAL_OVERRIDE


def test_current_research_progress_defaults_to_web_lookup_without_paper_hint() -> None:
    resolved = _resolver().resolve(
        "查一下 2026 年最新的 LLM 推荐系统研究进展",
        InteractionContext(surface="global_chat"),
    )
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.retrieval_plan == RetrievalPlan.WEB_LOOKUP
    assert resolved.scope.source == ResolveSource.RETRIEVAL_OVERRIDE


def test_current_research_progress_with_paper_hint_stays_paper_discovery() -> None:
    resolved = _resolver().resolve(
        "查一下 2026 年最新的 LLM 推荐系统相关论文进展",
        InteractionContext(surface="global_chat"),
    )
    assert resolved.retrieval_plan == RetrievalPlan.RESEARCH_EXPLORATION


def test_resolved_request_dict_round_trip() -> None:
    ctx = InteractionContext(
        surface="paper_reader",
        canonical_paper_id="paper_123",
        selection=SelectionAnchor("paper_123", "B-7", "abstract text", page=1, section_path=("摘要",)),
    )
    resolved = _resolver().resolve("解释一下", ctx)
    data = resolved.to_dict()
    assert data["mode"] == ResolveMode.PAPER_LOCAL
    assert data["retrieval_plan"] == RetrievalPlan.PAPER_LOCAL
    assert data["scope"]["paper_ids"] == ["paper_123"]

    # Round-trip through the JSON-ish dict and back to the identical dataclass.
    rebuilt = ResolvedRequest.from_dict(data)
    assert rebuilt is not None
    assert rebuilt.mode == resolved.mode
    assert rebuilt.query == resolved.query
    assert rebuilt.scope.paper_ids == resolved.scope.paper_ids
    assert rebuilt.scope.selection is not None
    assert rebuilt.scope.selection.block_id == resolved.scope.selection.block_id
    assert rebuilt.context_block == resolved.context_block


def test_interaction_context_dict_round_trip() -> None:
    ctx = InteractionContext(
        surface="research_workspace",
        canonical_paper_id="paper_1",
        research_question_id="rq_2",
        selection=SelectionAnchor("paper_1", "B-1", "some text", section_path=("方法", "3.1")),
    )
    rebuilt = InteractionContext.from_dict(ctx.to_dict())
    assert rebuilt.surface == "research_workspace"
    assert rebuilt.canonical_paper_id == "paper_1"
    assert rebuilt.research_question_id == "rq_2"
    assert rebuilt.selection is not None
    assert rebuilt.selection.section_path == ("方法", "3.1")


def test_missing_selection_is_valid_for_whole_paper() -> None:
    ctx = InteractionContext(surface="paper_reader", canonical_paper_id="paper_9")
    resolved = _resolver().resolve("方法有什么缺陷？", ctx)
    assert resolved.scope.selection is None
    # A whole-paper local ask still binds the single paper.
    assert resolved.scope.paper_ids == ("paper_9",)


def test_paper_local_policy_drops_literature_search_from_required() -> None:
    """Mode → policy coupling: paper-local makes the search tools merely allowed
    (not required), so a narrowed tool surface passes preflight — this is what
    lets a single-paper ask physically avoid the search dance."""
    from research_pulse.workbench.capability_policy import capability_policy_for_mode

    required_paper_local = set(capability_policy_for_mode("paper_local").required)
    allowed_paper_local = set(capability_policy_for_mode("paper_local").allowed)
    # Search tools may not be required for a paper-local ask...
    assert "search_sources" not in required_paper_local
    assert "search_arxiv" not in required_paper_local
    # ...but they remain legally present in the actual inventory.
    assert "search_sources" in allowed_paper_local
    # The read surface stays mandatory.
    assert "read_paper_metadata" in required_paper_local
    assert "read_managed_blocks" in required_paper_local
    # Unknown / absence-of-mode falls back to the full assistant surface (search required).
    assistant_required = set(capability_policy_for_mode(None).required)
    assert "search_sources" in assistant_required


# --- Tier-0 retrieval override: an explicit retrieval ask beats UI context ---

def test_explicit_retrieval_ask_widens_anchored_paper_reader_to_synthesis() -> None:
    ctx = InteractionContext(surface="paper_reader", canonical_paper_id="paper_123")
    resolved = _resolver().resolve("搜索一下近两年的相关工作", ctx)
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.scope.evidence_scope == EvidenceScope.CURRENT_PAPER
    # The current paper stays the anchor.
    assert resolved.scope.paper_ids == ("paper_123",)
    assert resolved.scope.source == ResolveSource.RETRIEVAL_OVERRIDE
    assert "paper_123" in (resolved.context_block or "")


def test_anchor_paper_plus_progress_ask_widens_without_losing_anchor() -> None:
    ctx = InteractionContext(surface="paper_reader", canonical_paper_id="paper_123")
    resolved = _resolver().resolve("结合这篇论文看看最新进展", ctx)
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.scope.paper_ids == ("paper_123",)


def test_explicit_retrieval_ask_with_selection_keeps_selection_anchor() -> None:
    ctx = InteractionContext(
        surface="paper_reader",
        canonical_paper_id="paper_123",
        selection=SelectionAnchor("paper_123", "B-42", "KNN 的最近邻检索", page=3, section_path=("方法",)),
    )
    resolved = _resolver().resolve("搜索类似方法的其他论文", ctx)
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.scope.selection is not None
    assert resolved.scope.selection.block_id == "B-42"
    assert resolved.scope.paper_ids == ("paper_123",)


def test_explicit_containment_ask_stays_paper_local_via_ui_context() -> None:
    ctx = InteractionContext(surface="paper_reader", canonical_paper_id="paper_123")
    resolved = _resolver().resolve("只看这篇论文", ctx)
    assert resolved.mode == ResolveMode.PAPER_LOCAL
    assert resolved.scope.source == ResolveSource.UI_CONTEXT


def test_plain_paper_question_without_retrieval_ask_keeps_paper_local() -> None:
    ctx = InteractionContext(surface="paper_reader", canonical_paper_id="paper_123")
    resolved = _resolver().resolve("作者这里为什么这么设计", ctx)
    assert resolved.mode == ResolveMode.PAPER_LOCAL
    assert resolved.scope.source == ResolveSource.UI_CONTEXT


def test_retrieval_ask_without_anchor_falls_back_to_global_synthesis() -> None:
    ctx = InteractionContext(surface="global_chat")
    resolved = _resolver().resolve("搜索最新的 dpo 推荐论文", ctx)
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.scope.evidence_scope == EvidenceScope.GLOBAL_LIBRARY
    assert resolved.scope.source == ResolveSource.RETRIEVAL_OVERRIDE


def test_retrieval_ask_in_research_workspace_keeps_project_scope() -> None:
    ctx = InteractionContext(
        surface="research_workspace", canonical_paper_id="paper_123", research_question_id="rq_17",
    )
    resolved = _resolver().resolve("结合这篇论文看看最新进展", ctx)
    assert resolved.mode == ResolveMode.RESEARCH_SYNTHESIS
    assert resolved.scope.evidence_scope == EvidenceScope.PROJECT
    assert resolved.scope.research_question_id == "rq_17"
    assert resolved.scope.paper_ids == ("paper_123",)
