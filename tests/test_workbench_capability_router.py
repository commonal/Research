from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.capability_router import CapabilityRouter
from research_pulse.workbench.scope_resolver import InteractionContext
from research_pulse.workbench.turn_runtime import TurnRequest


class CapabilityRouterTests(TestCase):
    def setUp(self) -> None:
        self.router = CapabilityRouter()

    def test_explicit_selection_action_wins_over_global_context(self) -> None:
        decision = self.router.resolve(TurnRequest(
            session_id="s",
            message="请翻译这段",
            explicit_action="translate",
            interaction_context=InteractionContext(surface="global_chat"),
        ))
        self.assertEqual(decision.capability, "paper")
        self.assertEqual(decision.retrieval_plan, "paper_local")
        self.assertIn("显式动作", decision.reason)

    def test_web_request_wins_over_paper_reader_context(self) -> None:
        decision = self.router.resolve(TurnRequest(
            session_id="s",
            message="查网页上的 2026 年最新进展",
            interaction_context=InteractionContext(
                surface="paper_reader", canonical_paper_id="paper-1"
            ),
        ))
        self.assertEqual(decision.capability, "web")
        self.assertEqual(decision.retrieval_plan, "web_lookup")
        self.assertEqual(decision.execution_mode, "durable")

    def test_paper_reader_defaults_to_paper_without_external_search(self) -> None:
        decision = self.router.resolve(TurnRequest(
            session_id="s",
            message="总结这篇论文",
            interaction_context=InteractionContext(
                surface="paper_reader", canonical_paper_id="paper-1"
            ),
        ))
        self.assertEqual(decision.capability, "paper")
        self.assertEqual(decision.retrieval_plan, "paper_local")

    def test_explicit_research_run_wins_when_paper_reader_is_open(self) -> None:
        decision = self.router.resolve(TurnRequest(
            session_id="s",
            message="请围绕当前研究焦点继续深入研究：beta 敏感性",
            explicit_action="research_run",
            interaction_context=InteractionContext(
                surface="paper_reader",
                canonical_paper_id="paper-1",
                research_question_id="rq-1",
            ),
        ))
        self.assertEqual(decision.capability, "research")
        self.assertEqual(decision.retrieval_plan, "research_exploration")
        self.assertEqual(decision.execution_mode, "durable")

    def test_plain_global_question_defaults_to_basic(self) -> None:
        decision = self.router.resolve(TurnRequest(
            session_id="s",
            message="什么是 RLHF？",
        ))
        self.assertEqual(decision.capability, "basic")
        self.assertEqual(decision.retrieval_plan, "direct")
        self.assertEqual(decision.execution_mode, "sync")

    def test_research_workspace_uses_research_capability(self) -> None:
        decision = self.router.resolve(TurnRequest(
            session_id="s",
            message="比较这几篇论文的方法",
            interaction_context=InteractionContext(
                surface="research_workspace", research_question_id="rq-1"
            ),
        ))
        self.assertEqual(decision.capability, "research")
        self.assertEqual(decision.execution_mode, "durable")

    def test_unavailable_capability_is_reported_without_escalation(self) -> None:
        decision = self.router.resolve(
            TurnRequest(session_id="s", message="搜索网页上的最新消息"),
            available_capabilities={"basic", "paper", "research"},
        )
        self.assertEqual(decision.capability, "web")
        self.assertEqual(decision.status, "unavailable")
        self.assertEqual(decision.confidence, 0.0)
