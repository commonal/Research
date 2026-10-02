"""Scope Resolver — resolve a user query + interaction context into an explicit
evidence scope and task mode BEFORE the Planner/Agent runs (M3.10).

Design contract (two orthogonal dimensions, never one intent):

    QueryIntent    — what kind of answer is wanted
    EvidenceScope  — which evidence MAY be used

``ResolveMode`` is the (intent, evidence_scope) pairing that drives the tool
surface and system prompt. The resolver resolves *the facts* about the ask (the
scope); it does NOT choose how to answer (the Planner does) and does NOT decide
what may be committed into canonical state (the WorkspaceGate does).

Priority ladder (context beats natural-language classification):

    1. explicit user scope
    2. current UI / workspace scope
    3. conversation scope (stub in V1)
    4. entities named in the query (light lexical scan)
    5. LLM intent inference (stub in V1 — deliberately NOT on the hot path)
    6. unambiguous fallback

V1 implements tiers 1, 2 and a light 4; tiers 3 and 5 fall through to 6, which
defaults to a direct answer so small questions do not consume research budget.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field


class QueryIntent:
    PAPER_QA = "paper_qa"
    RESEARCH_QA = "research_qa"
    NAVIGATION = "navigation"
    COMPARE = "compare"
    UNKNOWN = "unknown"


class EvidenceScope:
    CURRENT_PAPER = "current_paper"
    PAPER_SET = "paper_set"
    PROJECT = "project"
    GLOBAL_LIBRARY = "global_library"


class ResolveMode:
    # Low-cost conversational answer. No retrieval tools are needed unless the
    # user explicitly upgrades the request.
    DIRECT = "direct"
    # Single-paper Q&A: answer directly from the paper, minimal tool surface
    # (no literature search). Selection/whole-paper local asks.
    PAPER_LOCAL = "paper_local"
    # Open-ended research progression: full plan → search → read → synthesize.
    RESEARCH_SYNTHESIS = "research_synthesis"
    # An open paper's findings mapped onto an active research question.
    PAPER_IN_RESEARCH_CONTEXT = "paper_in_research_context"
    # Multiple papers compared on a common axis.
    CROSS_PAPER_COMPARE = "cross_paper_compare"
    # Could not determine the intent; keep the safe research-synthesis default.
    AMBIGUOUS = "ambiguous"


class RetrievalPlan:
    """How much external/paper retrieval this turn is allowed to use."""

    DIRECT = "direct"
    WEB_LOOKUP = "web_lookup"
    PAPER_LOCAL = "paper_local"
    PAPER_EVIDENCE = "paper_evidence"
    RESEARCH_EXPLORATION = "research_exploration"


class RetrievalIntent:
    """The user's explicit retrieval ask this turn (Tier-0 signal).

    A small structured override that runs BEFORE the UI-context ladder: when the
    user explicitly asks to confine or widen evidence, that beats whatever the
    current surface would otherwise default to. UNSPECIFIED means no explicit
    retrieval ask — the UI context stays authoritative.
    """

    LOCAL_ONLY = "local_only"          # e.g. "只看这篇论文"
    EXPAND_EXTERNAL = "expand_external"  # e.g. "搜索近两年的相关工作"
    WEB_LOOKUP = "web_lookup"          # e.g. "查网页上的最新消息"
    UNSPECIFIED = "unspecified"        # UI context decides


class ResolveSource:
    EXPLICIT = "explicit"
    RETRIEVAL_OVERRIDE = "retrieval_override"
    RESEARCH_ESCALATION = "research_escalation"
    UI_CONTEXT = "ui_context"
    CONVERSATION = "conversation"
    LLM_INFERENCE = "llm_inference"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SelectionAnchor:
    paper_id: str
    block_id: str
    text: str
    page: int | None = None
    section_path: tuple[str, ...] = ()
    block_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class InteractionContext:
    """Where the user is right now. This state is authoritative over any
    natural-language classification of the query."""

    surface: str = "global_chat"  # paper_reader | research_workspace | library | global_chat
    canonical_paper_id: str | None = None
    research_question_id: str | None = None
    project_id: str | None = None
    selection: SelectionAnchor | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "canonical_paper_id": self.canonical_paper_id,
            "research_question_id": self.research_question_id,
            "project_id": self.project_id,
            "selection": (
                {
                    "paper_id": self.selection.paper_id,
                    "block_id": self.selection.block_id,
                    "block_ids": list(self.selection.block_ids or (self.selection.block_id,)),
                    "text": self.selection.text,
                    "page": self.selection.page,
                    "section_path": list(self.selection.section_path),
                }
                if self.selection is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> "InteractionContext":
        data = data or {}
        selection = data.get("selection")
        return cls(
            surface=str(data.get("surface", "global_chat")),
            canonical_paper_id=(str(data["canonical_paper_id"]) if data.get("canonical_paper_id") else None),
            research_question_id=(str(data["research_question_id"]) if data.get("research_question_id") else None),
            project_id=(str(data["project_id"]) if data.get("project_id") else None),
            selection=(
                SelectionAnchor(
                    paper_id=str(selection["paper_id"]),
                    block_id=str(selection["block_id"]),
                    text=str(selection.get("text", "")),
                    page=int(selection["page"]) if selection.get("page") is not None else None,
                    section_path=tuple(str(p) for p in selection.get("section_path", [])),
                    block_ids=tuple(str(block_id) for block_id in selection.get("block_ids", []) if block_id),
                )
                if isinstance(selection, dict)
                else None
            ),
        )


@dataclass(frozen=True)
class ResolvedScope:
    intent: str
    evidence_scope: str
    paper_ids: tuple[str, ...] = ()
    research_question_id: str | None = None
    selection: SelectionAnchor | None = None
    confidence: float = 1.0
    source: str = ResolveSource.UI_CONTEXT

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "evidence_scope": self.evidence_scope,
            "paper_ids": list(self.paper_ids),
            "research_question_id": self.research_question_id,
            "confidence": self.confidence,
            "source": self.source,
            "selection": (
                {
                    "paper_id": self.selection.paper_id,
                    "block_id": self.selection.block_id,
                    "block_ids": list(self.selection.block_ids or (self.selection.block_id,)),
                    "text": self.selection.text,
                    "page": self.selection.page,
                    "section_path": list(self.selection.section_path),
                }
                if self.selection is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ResolvedRequest:
    task: str = "answer_question"
    mode: str = ResolveMode.AMBIGUOUS
    retrieval_plan: str = "direct"
    query: str = ""
    scope: ResolvedScope = field(
        default_factory=lambda: ResolvedScope(QueryIntent.UNKNOWN, EvidenceScope.CURRENT_PAPER)
    )
    # A ready-to-inject context snippet describing the evidence
    # the model starts with (selection text / anchor). Kept apart from
    # ``query`` so the question echo stays clean.
    context_block: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "mode": self.mode,
            "retrieval_plan": self.retrieval_plan,
            "query": self.query,
            "scope": self.scope.to_dict(),
            "context_block": self.context_block,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> "ResolvedRequest | None":
        if not data:
            return None
        scope_data = data.get("scope") or {}
        selection_data = scope_data.get("selection")
        selection = (
            SelectionAnchor(
                paper_id=str(selection_data["paper_id"]),
                block_id=str(selection_data["block_id"]),
                text=str(selection_data.get("text", "")),
                page=int(selection_data["page"]) if selection_data.get("page") is not None else None,
                section_path=tuple(str(p) for p in selection_data.get("section_path", [])),
                block_ids=tuple(str(block_id) for block_id in selection_data.get("block_ids", []) if block_id),
            )
            if isinstance(selection_data, dict)
            else None
        )
        return cls(
            task=str(data.get("task", "answer_question")),
            mode=str(data.get("mode", ResolveMode.AMBIGUOUS)),
            retrieval_plan=str(data.get("retrieval_plan", "direct")),
            query=str(data.get("query", "")),
            scope=ResolvedScope(
                intent=str(scope_data.get("intent", QueryIntent.UNKNOWN)),
                evidence_scope=str(scope_data.get("evidence_scope", EvidenceScope.CURRENT_PAPER)),
                paper_ids=tuple(str(p) for p in scope_data.get("paper_ids", [])),
                research_question_id=(str(scope_data["research_question_id"]) if scope_data.get("research_question_id") else None),
                selection=selection,
                confidence=float(scope_data.get("confidence", 1.0)),
                source=str(scope_data.get("source", ResolveSource.UI_CONTEXT)),
            ),
            context_block=(str(data["context_block"]) if data.get("context_block") else None),
        )


# Light lexical hints for the tier-4 entity scan (never the primary source).
_OPEN_DISCOURSE_HINTS = ("比较", "对比", "差异", "vs", "相比", "survey", "综述")
_NAVIGATION_HINTS = ("打开", "看", "跳到", "去", "翻到", "第", "页", "打开这篇")
_OPEN_ENDED_HINTS = ("怎么", "如何", "有什么方法", "方案", "设计实验", "review", "总结", "总结一下", "帮助我")
_RESEARCH_ESCALATION_HINTS = (
    "比较", "对比", "综述", "survey", "review", "最新进展", "相关工作",
    "多篇论文", "不同论文", "全面调研", "系统调研",
)


def detect_research_request(query: str) -> bool:
    """Detect explicit multi-source intent without treating every question as research."""
    lowered = (query or "").lower()
    return any(hint in lowered for hint in _RESEARCH_ESCALATION_HINTS)


# ---------------------------------------------------------------------------
# Retrieval-intent detector (Tier-0 override).
#
# Deliberately NOT a keyword→mode mapping. It classifies the query's explicit
# *retrieval ask* into one small structured signal — LOCAL_ONLY /
# EXPAND_EXTERNAL / UNSPECIFIED — with rule families that require structure,
# never a lone trigger word:
#   R3 (checked first) explicit containment : "只看这篇论文" / negated search
#   R1 (imperative)    search verb + paper-ish object : "搜索近两年的相关工作"
#   R2 (nominal)       expansion head + paper-ish tail : "看看最新进展"
# UNSPECIFIED falls through to the UI-context ladder. Over-expansion is the
# safe failure direction (search enabled, anchor kept); under-expansion traps
# the user in a paper-local mode, which is the bug we are fixing.
# ---------------------------------------------------------------------------
_RETRIEVAL_VERBS = (
    "搜索", "检索", "搜一搜", "搜索一下", "搜", "查一查", "查一下", "查",
    "找一找", "找一下", "找找", "找", "调研",
)
_WEB_HINTS = (
    "网页", "网上", "网站", "官网", "新闻", "最新消息", "实时", "web", "online",
)
_PAPERISH_OBJECTS = (
    "论文", "文献", "相关工作", "工作", "进展", "研究", "资料",
    "方法", "技术", "模型", "工具", "成果", "survey", "paper",
    "papers", "literature", "work", "articles", "arxiv",
)
_PAPER_SPECIFIC_HINTS = (
    "论文", "文献", "相关工作", "survey", "review", "literature",
    "paper", "papers", "arxiv",
)
_PROGRESS_HINTS = ("进展", "动态", "趋势", "现状", "变化")
_EXPANSION_HEADS = (
    "最新", "近期", "近两年", "近年", "最近", "业界", "领域内",
    "领域", "相关", "类似", "别的", "更多", "其他", "外部", "外面", "全网",
)
_LOCAL_ONLY_PATTERNS = (
    re.compile(r"只(?:看|读|围绕|基于|针对|想|要|能)[^。？！?!]{0,8}(?:这篇|当前|本|该)?(?:论文|文章|工作)"),
    re.compile(r"(?:不要|不用|别|无需|不需要)(?:再|去|给我)?(?:搜索|检索|搜|查)(?:外面|外部|别的|其他)?(?:的)?(?:论文|文献|资料|工作)?"),
    re.compile(r"(?:只看|仅看|只读)这篇论文"),
)


def detect_retrieval_intent(query: str) -> str:
    """Return the narrow retrieval escalation requested by the user."""
    text = (query or "").strip()
    if not text:
        return RetrievalIntent.UNSPECIFIED
    lowered = text.lower()
    # R3 — explicit containment wins over everything.
    if any(pattern.search(text) for pattern in _LOCAL_ONLY_PATTERNS):
        return RetrievalIntent.LOCAL_ONLY
    # A web hint plus a retrieval/recency cue is intentionally narrower than
    # generic "latest" language: it must not turn every current-paper question
    # into an external network call.
    if any(hint in lowered for hint in _WEB_HINTS) and (
        any(verb in text for verb in _RETRIEVAL_VERBS)
        or any(hint in lowered for hint in ("最新", "近期", "最近", "today", "latest"))
    ):
        return RetrievalIntent.WEB_LOOKUP
    # R1 — an imperative retrieval verb plus a paper-ish object.
    verb = next((v for v in _RETRIEVAL_VERBS if v in text), None)
    if verb is not None:
        obj = next((o for o in _PAPERISH_OBJECTS if o in lowered), None)
        if obj is not None:
            # A temporal landscape question without an explicit paper-shaped
            # object is a current-web lookup.  This keeps queries such as
            # "查一下 2026 年最新的 LLM 推荐系统研究进展" out of the
            # expensive arXiv -> paper-block evidence chain.  Explicit
            # "论文/文献/arXiv/相关工作" still selects paper discovery.
            is_current = bool(
                re.search(r"\b20\d{2}\b", lowered)
                or any(hint in lowered for hint in _EXPANSION_HEADS)
            )
            if (
                is_current
                and any(hint in lowered for hint in _PROGRESS_HINTS)
                and not any(hint in lowered for hint in _PAPER_SPECIFIC_HINTS)
            ):
                return RetrievalIntent.WEB_LOOKUP
            return RetrievalIntent.EXPAND_EXTERNAL
        return RetrievalIntent.UNSPECIFIED
    # R1b — English imperative retrieval.
    if re.search(r"\b(search|find|look\s*up)\b", lowered):
        if re.search(r"\b(paper|papers|literature|survey|work|articles|arxiv|related\s+work)\b", lowered):
            return RetrievalIntent.EXPAND_EXTERNAL
        return RetrievalIntent.UNSPECIFIED
    # R2 — nominal expansion ask ("最新进展", "近两年的相关工作").
    head = next((h for h in _EXPANSION_HEADS if h in lowered), None)
    if head is not None:
        tail = next((o for o in _PAPERISH_OBJECTS if o in lowered), None)
        if tail is not None:
            return RetrievalIntent.EXPAND_EXTERNAL
    return RetrievalIntent.UNSPECIFIED


class ScopeResolver:
    """Deterministic, hot-path-free resolver (no LLM on the primary tiers)."""

    def resolve(
        self,
        query: str,
        context: InteractionContext,
        *,
        explicit_mode: str | None = None,
        explicit_scope: str | None = None,
        explicit_paper_ids: tuple[str, ...] = (),
        explicit_research_question_id: str | None = None,
    ) -> ResolvedRequest:
        query = (query or "").strip()
        rq = (explicit_research_question_id or context.research_question_id)
        source = ResolveSource.EXPLICIT

        # --- Tier 1: explicit user scope (overrides everything) ---
        if explicit_mode:
            return self._resolved(explicit_mode, explicit_scope, query, source=source, paper_ids=explicit_paper_ids, rq=rq)

        # --- Tier 1b: explicit retrieval override (the query's retrieval ask
        # beats the UI-context default for THIS turn; the UI context still
        # provides the default scope when the ask is UNSPECIFIED). An external
        # expansion keeps the current paper / selection as the anchor.
        retrieval_intent = detect_retrieval_intent(query)
        if retrieval_intent in {RetrievalIntent.EXPAND_EXTERNAL, RetrievalIntent.WEB_LOOKUP} or detect_research_request(query):
            escalation_source = (
                ResolveSource.RETRIEVAL_OVERRIDE
                if retrieval_intent in {RetrievalIntent.EXPAND_EXTERNAL, RetrievalIntent.WEB_LOOKUP}
                else ResolveSource.RESEARCH_ESCALATION
            )
            retrieval_plan = (
                RetrievalPlan.WEB_LOOKUP
                if retrieval_intent == RetrievalIntent.WEB_LOOKUP
                else RetrievalPlan.RESEARCH_EXPLORATION
            )
            selection = context.selection
            if selection is not None:
                return self._resolved(
                    ResolveMode.RESEARCH_SYNTHESIS, EvidenceScope.CURRENT_PAPER, query,
                    source=escalation_source,
                    paper_ids=(selection.paper_id,), selection=selection,
                    retrieval_plan=retrieval_plan,
                )
            if rq:
                return self._resolved(
                    ResolveMode.RESEARCH_SYNTHESIS, EvidenceScope.PROJECT, query,
                    source=escalation_source, rq=rq,
                    paper_ids=(context.canonical_paper_id,) if context.canonical_paper_id else (),
                    retrieval_plan=retrieval_plan,
                )
            if context.canonical_paper_id:
                return self._resolved(
                    ResolveMode.RESEARCH_SYNTHESIS, EvidenceScope.CURRENT_PAPER, query,
                    source=escalation_source,
                    paper_ids=(context.canonical_paper_id,),
                    retrieval_plan=retrieval_plan,
                )
            return self._resolved(
                ResolveMode.RESEARCH_SYNTHESIS, EvidenceScope.GLOBAL_LIBRARY, query,
                source=escalation_source,
                retrieval_plan=retrieval_plan,
            )

        # --- Tier 2: current UI / workspace scope (authoritative default) ---
        if context.selection is not None:
            sel = context.selection
            return self._resolved(
                ResolveMode.PAPER_LOCAL, EvidenceScope.CURRENT_PAPER, query,
                source=ResolveSource.UI_CONTEXT,
                paper_ids=(sel.paper_id,), selection=sel,
            )
        if context.surface == "paper_reader" and context.canonical_paper_id:
            return self._resolved(
                ResolveMode.PAPER_LOCAL, EvidenceScope.CURRENT_PAPER, query,
                source=ResolveSource.UI_CONTEXT,
                paper_ids=(context.canonical_paper_id,),
            )
        if context.surface == "research_workspace":
            if context.canonical_paper_id and rq:
                return self._resolved(
                    ResolveMode.PAPER_IN_RESEARCH_CONTEXT, EvidenceScope.PROJECT, query,
                    source=ResolveSource.UI_CONTEXT,
                    paper_ids=(context.canonical_paper_id,), rq=rq,
                    retrieval_plan=RetrievalPlan.PAPER_EVIDENCE,
                )
            if rq:
                return self._resolved(
                    ResolveMode.RESEARCH_SYNTHESIS, EvidenceScope.PROJECT, query,
                    source=ResolveSource.UI_CONTEXT, rq=rq,
                    retrieval_plan=RetrievalPlan.DIRECT,
                )
            if context.canonical_paper_id:
                return self._resolved(
                    ResolveMode.PAPER_IN_RESEARCH_CONTEXT, EvidenceScope.CURRENT_PAPER, query,
                    source=ResolveSource.UI_CONTEXT,
                    paper_ids=(context.canonical_paper_id,),
                    retrieval_plan=RetrievalPlan.PAPER_EVIDENCE,
                )

        # --- Tier 4: light entity/lexical scan (never the primary source) ---
        if any(h in query for h in _NAVIGATION_HINTS):
            return self._resolved(
                ResolveMode.DIRECT, EvidenceScope.CURRENT_PAPER, query,
                source=ResolveSource.AMBIGUOUS,
                paper_ids=(context.canonical_paper_id,) if context.canonical_paper_id else (),
            )

        # --- Tier 5/6: ambiguous — answer directly. Retrieval is an explicit
        # escalation, not the default for a conversational question.
        return self._resolved(
            ResolveMode.DIRECT, EvidenceScope.GLOBAL_LIBRARY, query,
            source=ResolveSource.AMBIGUOUS, rq=rq,
            paper_ids=(context.canonical_paper_id,) if context.canonical_paper_id else (),
        )

    def _resolved(
        self,
        mode: str,
        scope: str,
        query: str,
        *,
        source: str,
        paper_ids: tuple[str, ...] = (),
        selection: SelectionAnchor | None = None,
        rq: str | None = None,
        retrieval_plan: str | None = None,
    ) -> ResolvedRequest:
        intent = self._intent_for(mode, selection is not None)
        resolved_scope = ResolvedScope(
            intent=intent,
            evidence_scope=scope,
            paper_ids=paper_ids,
            research_question_id=rq,
            selection=selection,
            source=source,
        )
        return ResolvedRequest(
            task="answer_question",
            mode=mode,
            retrieval_plan=retrieval_plan or self._retrieval_plan_for(mode),
            query=query,
            scope=resolved_scope,
            context_block=self._context_block(mode, resolved_scope),
        )

    @staticmethod
    def _retrieval_plan_for(mode: str) -> str:
        if mode == ResolveMode.PAPER_LOCAL:
            return RetrievalPlan.PAPER_LOCAL
        if mode == ResolveMode.PAPER_IN_RESEARCH_CONTEXT:
            return RetrievalPlan.PAPER_EVIDENCE
        if mode in {ResolveMode.RESEARCH_SYNTHESIS, ResolveMode.CROSS_PAPER_COMPARE}:
            return RetrievalPlan.RESEARCH_EXPLORATION
        return RetrievalPlan.DIRECT

    @staticmethod
    def _intent_for(mode: str, has_selection: bool) -> str:
        if mode == ResolveMode.PAPER_LOCAL:
            return QueryIntent.PAPER_QA
        if mode == ResolveMode.PAPER_IN_RESEARCH_CONTEXT:
            return QueryIntent.RESEARCH_QA
        if mode == ResolveMode.CROSS_PAPER_COMPARE:
            return QueryIntent.COMPARE
        if mode == ResolveMode.RESEARCH_SYNTHESIS:
            return QueryIntent.RESEARCH_QA
        return QueryIntent.UNKNOWN

    @staticmethod
    def _context_block(mode: str, scope: ResolvedScope) -> str | None:
        if scope.selection is not None:
            sel = scope.selection
            locator = " · ".join(sel.section_path) if sel.section_path else "论文正文"
            if sel.page is not None:
                locator = f"{locator} · 第 {sel.page} 页"
            text = sel.text[:2500]
            block_ids = sel.block_ids or (sel.block_id,)
            block_id_json = ", ".join(f'"{block_id}"' for block_id in block_ids)
            return (
                f"本问题基于当前选区（{locator}，source_id={sel.paper_id}，block_ids={list(block_ids)}）：\n"
                f"「{text}」\n"
                f"如需精确引用，可直接 read_managed_blocks(source_id=\"{sel.paper_id}\", block_ids=[{block_id_json}])。"
            )
        if scope.paper_ids:
            anchor = f"本问题锚定当前论文（source_id={scope.paper_ids[0]}）。"
            if mode == ResolveMode.PAPER_LOCAL:
                return anchor + "请直接 read_paper_metadata + read_managed_blocks 读这篇，不要为定位它而搜索。"
            return anchor + "先读它的正文；如问题需要最新外部进展，再检索 arXiv / 目录补充相关工作。"
        return None
