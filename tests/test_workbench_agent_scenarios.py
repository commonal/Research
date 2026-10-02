from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.tool_dispatcher import (
    RateLimitError,
    ServiceUnavailableError,
    ToolNotConfiguredError,
)
from research_pulse.workbench.run_models import AttemptStatus

from tests.workbench_scenarios import (
    AgentScenario,
    ControlledClock,
    FakeTool,
    FinalAnswer,
    ScriptedModel,
    ToolCall,
    readonly_policy,
    PersistedAttemptScenario,
)


class WorkbenchAgentScenarioTests(TestCase):
    def test_budget_and_network_terminal_scenarios_are_fully_observable(self) -> None:
        cases = (
            (
                AttemptStatus.BUDGET_EXHAUSTED,
                "budget_exhausted",
                "探索预算已耗尽",
            ),
            (
                AttemptStatus.RETRYABLE_FAILURE,
                "connection_failed",
                "外部服务连接中断",
            ),
        )
        for status, event_type, safe_error in cases:
            with self.subTest(status=status.value):
                scenario = PersistedAttemptScenario()
                self.addCleanup(scenario.close)
                scenario.emit(event_type, safe_error)
                scenario.finish(status, safe_error=safe_error)

                result = scenario.result()

                self.assertEqual(status, result.snapshot.current_attempt.status)
                self.assertEqual(safe_error, result.snapshot.current_attempt.safe_error)
                self.assertEqual((event_type,), tuple(e.event_type for e in result.events))
                self.assertEqual((), result.side_effects)

    def test_user_cancel_blocks_late_events_and_side_effects(self) -> None:
        scenario = PersistedAttemptScenario()
        self.addCleanup(scenario.close)
        scenario.emit("tool_started", "远程工具开始")

        scenario.cancel()

        with self.assertRaisesRegex(ValueError, "terminal"):
            scenario.emit("tool_completed", "取消后的晚返回")
        with self.assertRaisesRegex(ValueError, "not running"):
            scenario.commit_side_effect("late-write")
        result = scenario.result()
        self.assertEqual(AttemptStatus.CANCELLED, result.snapshot.current_attempt.status)
        self.assertEqual(("tool_started",), tuple(e.event_type for e in result.events))
        self.assertEqual((), result.side_effects)

    def test_stale_worker_late_return_cannot_change_new_generation(self) -> None:
        scenario = PersistedAttemptScenario()
        self.addCleanup(scenario.close)
        scenario.emit("tool_started", "旧 worker 开始")

        reclaimed = scenario.reclaim()

        self.assertEqual(scenario.lease.generation + 1, reclaimed.generation)
        with self.assertRaisesRegex(ValueError, "generation changed"):
            scenario.emit(
                "tool_completed", "旧 worker 晚返回",
                generation=scenario.lease.generation,
            )
        with self.assertRaisesRegex(ValueError, "generation changed"):
            scenario.commit_side_effect(
                "stale-write", generation=scenario.lease.generation,
            )
        result = scenario.result()
        self.assertEqual(AttemptStatus.RUNNING, result.snapshot.current_attempt.status)
        self.assertEqual(("tool_started",), tuple(e.event_type for e in result.events))
        self.assertEqual((), result.side_effects)

    def test_scenario_exposes_exact_calls_events_budget_and_outcomes(self) -> None:
        clock = ControlledClock()
        search = FakeTool(ConnectionError("temporary"), {"items": ["source:1"]})
        model = ScriptedModel(
            ToolCall("call-1", "search_sources", {"query": "DPO"}),
            FinalAnswer("有界结论"),
        )

        result = AgentScenario(
            model,
            {"search_sources": (search, readonly_policy(max_retries=1))},
            clock=clock,
            recovery_limit=1,
        ).run()

        self.assertEqual(2, len(model.calls))
        self.assertEqual([{"query": "DPO"}, {"query": "DPO"}], search.calls)
        self.assertEqual(
            ("tool_started", "tool_completed", "final_draft"),
            tuple(event.event_type for event in result.events),
        )
        self.assertEqual(("succeeded",), tuple(item.status.value for item in result.outcomes))
        self.assertEqual(2, result.budget.model_rounds)
        self.assertEqual(2, result.budget.tool_calls)
        self.assertEqual(1, result.budget.recovery_attempts)
        self.assertEqual("completed", result.budget.status)
        self.assertEqual(1.0, clock.seconds)
        self.assertEqual("有界结论", result.answer)

    def test_complete_tool_outcome_failure_classification_matrix(self) -> None:
        cases = (
            ("invalid_argument", FakeTool(ValueError("bad arguments")), "rejected", "invalid_argument", "after_correction", 1),
            ("timed_out", FakeTool("late", delay_seconds=0.03), "retryable_failure", "timed_out", "automatic", 1),
            ("rate_limited", FakeTool(RateLimitError(retry_after_seconds=2)), "retryable_failure", "rate_limited", "automatic", 1),
            ("service_unavailable", FakeTool(ServiceUnavailableError("503")), "retryable_failure", "service_unavailable", "automatic", 1),
            ("permission_denied", FakeTool(PermissionError("secret credential")), "terminal_failure", "permission_denied", "never", 1),
            ("not_configured", FakeTool(ToolNotConfiguredError("provider missing")), "terminal_failure", "not_configured", "never", 1),
            ("invalid_result", FakeTool({"content": "full paper"}), "terminal_failure", "invalid_result", "never", 1),
        )
        for name, tool, status, code, retryability, calls in cases:
            with self.subTest(name=name):
                model = ScriptedModel(
                    ToolCall(f"call-{name}", "required_tool", {}),
                    FinalAnswer("边界内结束"),
                )
                policy = readonly_policy(
                    timeout_seconds=0.005 if name == "timed_out" else 1,
                )

                result = AgentScenario(
                    model,
                    {"required_tool": (tool, policy)},
                ).run()

                outcome = result.outcomes[0]
                self.assertEqual(status, outcome.status.value)
                self.assertEqual(code, outcome.error_code.value)
                self.assertEqual(retryability, outcome.retryability.value)
                self.assertEqual("none", outcome.side_effect_state.value)
                self.assertEqual(calls, len(tool.calls))
                self.assertEqual(
                    ("tool_started", "tool_completed", "final_draft"),
                    tuple(event.event_type for event in result.events),
                )
                self.assertNotIn("secret", outcome.safe_message or "")


if __name__ == "__main__":
    import unittest
    unittest.main()
