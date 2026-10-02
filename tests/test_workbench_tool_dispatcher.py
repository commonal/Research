from __future__ import annotations

from time import sleep
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from unittest import TestCase

from research_pulse.workbench.agent_runtime import RunBudgets
from research_pulse.workbench.budget_enforcer import BudgetLedger

from research_pulse.workbench.tool_dispatcher import (
    ToolDispatcher,
    Idempotency,
    ParallelPolicy,
    RateLimitError,
    ServiceUnavailableError,
    ToolEffect,
    ToolPolicy,
    ToolPolicyRegistry,
    ToolPreflightError,
)


class WorkbenchToolPolicyTests(TestCase):
    def test_execute_returns_ephemeral_value_without_adding_it_to_outcome(self) -> None:
        calls: list[dict[str, object]] = []
        value = {"items": ("source:1",), "cursor": "next-page"}

        def search(*, query: str) -> dict[str, object]:
            calls.append({"query": query})
            return value

        registry = ToolPolicyRegistry()
        registry.register("search_sources", search, policy=ToolPolicy(
            ToolEffect.READ, Idempotency.IDEMPOTENT, 1, 0,
            lambda _args: ("source:index",), ParallelPolicy.SERIAL,
        ))

        execution = ToolDispatcher(registry).execute(
            attempt_id="attempt-1",
            tool_call_id="call-1",
            tool_name="search_sources",
            arguments={"query": "DPO"},
        )

        self.assertEqual("succeeded", execution.outcome.status.value)
        self.assertIs(value, execution.ephemeral_value)
        self.assertEqual([{"query": "DPO"}], calls)
        self.assertFalse(hasattr(execution.outcome, "ephemeral_value"))

    def test_preflight_fails_closed_for_missing_or_incomplete_policy(self) -> None:
        registry = ToolPolicyRegistry()
        registry.register("unclassified_tool", lambda: "result")

        with self.assertRaisesRegex(ToolPreflightError, "unclassified_tool"):
            registry.preflight(("unclassified_tool",))

        with self.assertRaises(ValueError):
            ToolPolicy(
                effect=ToolEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                timeout_seconds=0,
                max_retries=1,
                resource_keys=lambda _args: ("source:index",),
                parallel_policy=ParallelPolicy.SERIAL,
            )

    def test_complete_policy_is_exposed_by_preflight(self) -> None:
        registry = ToolPolicyRegistry()
        policy = ToolPolicy(
            effect=ToolEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            timeout_seconds=10,
            max_retries=1,
            resource_keys=lambda _args: ("source:index",),
            parallel_policy=ParallelPolicy.SERIAL,
        )
        registry.register("search_sources", lambda: "result", policy=policy)

        exposed = registry.preflight(("search_sources",))

        self.assertEqual(("search_sources",), tuple(exposed))
        self.assertIs(policy, exposed["search_sources"].policy)

    def test_dispatcher_normalizes_arguments_and_separates_error_views(self) -> None:
        registry = ToolPolicyRegistry()
        policy = ToolPolicy(
            ToolEffect.READ, Idempotency.IDEMPOTENT, 10, 0,
            lambda _args: ("source:index",), ParallelPolicy.SERIAL,
        )

        def rejected_tool(*, query: str):
            raise ValueError(f"invalid query; secret-token={query}")

        registry.register("search_sources", rejected_tool, policy=policy)
        dispatcher = ToolDispatcher(registry, diagnostic_id_factory=lambda: "diag-1")

        outcome = dispatcher.dispatch(
            attempt_id="attempt-1",
            tool_call_id="call-1",
            tool_name="search_sources",
            arguments={"query": "private"},
        )
        views = dispatcher.error_views(outcome)

        self.assertEqual("rejected", outcome.status.value)
        self.assertEqual("invalid_argument", outcome.error_code.value)
        self.assertNotIn("secret-token", views.user)
        self.assertNotIn("private", views.model)
        self.assertIn("secret-token", dispatcher.diagnostic("diag-1"))

    def test_dispatcher_wraps_timeout_permission_external_and_invalid_results(self) -> None:
        policy = ToolPolicy(
            ToolEffect.READ, Idempotency.IDEMPOTENT, 0.01, 0,
            lambda _args: ("source:index",), ParallelPolicy.SERIAL,
        )
        scenarios = (
            ("slow", lambda: sleep(0.05), "retryable_failure", "timed_out"),
            ("denied", lambda: (_ for _ in ()).throw(PermissionError("api-key=secret")), "terminal_failure", "permission_denied"),
            ("offline", lambda: (_ for _ in ()).throw(ConnectionError("host private")), "retryable_failure", "connection_failed"),
            ("unavailable", lambda: (_ for _ in ()).throw(ServiceUnavailableError()), "retryable_failure", "service_unavailable"),
            ("invalid", lambda: {"content": "full paper"}, "terminal_failure", "invalid_result"),
        )
        for name, handler, expected_status, expected_code in scenarios:
            with self.subTest(name=name):
                registry = ToolPolicyRegistry()
                registry.register(name, handler, policy=policy)
                dispatcher = ToolDispatcher(registry)
                outcome = dispatcher.dispatch(
                    attempt_id="attempt-1", tool_call_id=f"call-{name}",
                    tool_name=name, arguments={},
                )
                self.assertEqual(expected_status, outcome.status.value)
                self.assertEqual(expected_code, outcome.error_code.value)
                self.assertNotIn("secret", outcome.safe_message or "")

    def test_recovery_budget_bounds_retries_without_layer_multiplication(self) -> None:
        calls: list[int] = []
        def flaky() -> str:
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                raise ConnectionError("temporary")
            return "source:result-1"

        registry = ToolPolicyRegistry()
        registry.register("search_sources", flaky, policy=ToolPolicy(
            ToolEffect.READ, Idempotency.IDEMPOTENT, 1, 2,
            lambda _args: ("source:index",), ParallelPolicy.SERIAL,
        ))
        ledger = BudgetLedger(
            RunBudgets(2, 4, 4, 10, 100, 20), recovery_limit=2,
            attempt_id="attempt-1",
        )

        outcome = ToolDispatcher(registry).dispatch_with_recovery(
            attempt_id="attempt-1", tool_call_id="call-1",
            tool_name="search_sources", arguments={}, budget_ledger=ledger,
            sleeper=lambda _seconds: None,
        )

        self.assertEqual("succeeded", outcome.status.value)
        self.assertEqual(3, len(calls))
        self.assertEqual(3, ledger.snapshot().tool_calls)
        self.assertEqual(2, ledger.snapshot().recovery_attempts)

    def test_retry_after_is_bounded_and_exhaustion_returns_last_outcome(self) -> None:
        calls: list[int] = []
        waits: list[float] = []
        def limited() -> None:
            calls.append(1)
            raise RateLimitError(retry_after_seconds=2)
        registry = ToolPolicyRegistry()
        registry.register("search", limited, policy=ToolPolicy(
            ToolEffect.READ, Idempotency.IDEMPOTENT, 1, 1,
            lambda _args: ("source:index",), ParallelPolicy.SERIAL,
        ))
        ledger = BudgetLedger(RunBudgets(2, 3, 3, 10, 100, 20), recovery_limit=1)

        outcome = ToolDispatcher(registry).dispatch_with_recovery(
            attempt_id="attempt-1", tool_call_id="call-1", tool_name="search",
            arguments={}, budget_ledger=ledger, sleeper=waits.append,
        )

        self.assertEqual("rate_limited", outcome.error_code.value)
        self.assertEqual(2, len(calls))
        self.assertEqual([2], waits)

    def test_first_phase_serializes_concurrent_workspace_writes(self) -> None:
        state = {"active": 0, "maximum": 0}
        guard = Lock()
        def observed() -> str:
            with guard:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            sleep(0.02)
            with guard:
                state["active"] -= 1
            return "ok"
        registry = ToolPolicyRegistry()
        registry.register("add_evidence", observed, policy=ToolPolicy(
            ToolEffect.WRITE, Idempotency.NON_IDEMPOTENT, 1, 0,
            lambda _args: ("workspace:workspace-1",), ParallelPolicy.SERIAL,
        ))
        dispatcher = ToolDispatcher(registry)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = tuple(pool.map(lambda index: dispatcher.dispatch(
                attempt_id="attempt-1", tool_call_id=f"call-{index}",
                tool_name="add_evidence", arguments={},
            ), range(4)))
        self.assertTrue(all(item.status.value == "succeeded" for item in outcomes))
        self.assertEqual(1, state["maximum"])


if __name__ == "__main__":
    import unittest

    unittest.main()
