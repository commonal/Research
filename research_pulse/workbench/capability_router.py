"""Deterministic routing from a normalized turn to one capability profile."""

from __future__ import annotations

from typing import Iterable

from research_pulse.workbench.scope_resolver import (
    ResolveMode,
    RetrievalPlan,
    ScopeResolver,
)
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    CapabilityProfileRegistry,
    TurnRequest,
    default_capability_registry,
)


_ACTION_TO_CAPABILITY = {
    "translate": "paper",
    "explain": "paper",
    "ask_selection": "paper",
    "paper": "paper",
    "web": "web",
    "web_search": "web",
    "search_web": "web",
    "research": "research",
    "research_run": "research",
    "basic": "basic",
}


class CapabilityRouter:
    """Resolve one capability using explicit actions before existing scope rules."""

    def __init__(
        self,
        registry: CapabilityProfileRegistry | None = None,
        scope_resolver: ScopeResolver | None = None,
    ) -> None:
        self.registry = registry or default_capability_registry()
        self.scope_resolver = scope_resolver or ScopeResolver()

    def resolve(
        self,
        request: TurnRequest,
        *,
        available_capabilities: Iterable[str] | None = None,
    ) -> CapabilityDecision:
        available = set(available_capabilities) if available_capabilities is not None else set(self.registry.names())
        action = (request.explicit_action or "").strip().lower()
        explicit_capability = _ACTION_TO_CAPABILITY.get(action)
        resolved = self.scope_resolver.resolve(
            request.message,
            request.interaction_context,
        )

        if explicit_capability is not None:
            capability = explicit_capability
            reason = f"用户显式动作指定 {capability} 能力"
            source = "explicit_action"
        else:
            capability, reason, source = self._from_scope(resolved)

        profile = self.registry.get(capability)
        if capability not in available:
            return CapabilityDecision(
                capability=capability,
                retrieval_plan=resolved.retrieval_plan,
                execution_mode=profile.default_execution_mode,
                allowed_tools=profile.allowed_tools,
                evidence_scope=profile.evidence_scope,
                reason=f"{reason}；能力未配置或暂不可用",
                confidence=0.0,
                status="unavailable",
            )

        retrieval_plan = self._retrieval_plan_for(capability, resolved.retrieval_plan)
        return CapabilityDecision(
            capability=capability,
            retrieval_plan=retrieval_plan,
            execution_mode=profile.default_execution_mode,
            allowed_tools=profile.allowed_tools,
            evidence_scope=profile.evidence_scope,
            reason=f"{reason}（{source}）",
            confidence=1.0 if source in {"explicit_action", "ui_context", "retrieval_override"} else 0.8,
            status="selected",
        )

    @staticmethod
    def _from_scope(resolved) -> tuple[str, str, str]:
        if resolved.retrieval_plan == RetrievalPlan.WEB_LOOKUP:
            return "web", "请求明确要求网页检索", "retrieval_override"
        if resolved.retrieval_plan == RetrievalPlan.RESEARCH_EXPLORATION:
            return "research", "请求需要跨来源研究探索", "retrieval_override"
        if resolved.mode in {
            ResolveMode.PAPER_LOCAL,
            ResolveMode.PAPER_IN_RESEARCH_CONTEXT,
        }:
            return "paper", "当前论文或选区上下文优先", "ui_context"
        if resolved.mode in {
            ResolveMode.RESEARCH_SYNTHESIS,
            ResolveMode.CROSS_PAPER_COMPARE,
        }:
            return "research", "研究工作区或多论文比较上下文", "ui_context"
        return "basic", "未检测到明确检索或研究意图，使用低成本回答", "fallback"

    @staticmethod
    def _retrieval_plan_for(capability: str, resolved_plan: str) -> str:
        if capability == "basic":
            return RetrievalPlan.DIRECT
        if capability == "paper":
            return (
                resolved_plan
                if resolved_plan in {RetrievalPlan.PAPER_LOCAL, RetrievalPlan.PAPER_EVIDENCE}
                else RetrievalPlan.PAPER_LOCAL
            )
        if capability == "web":
            return RetrievalPlan.WEB_LOOKUP
        return RetrievalPlan.RESEARCH_EXPLORATION
