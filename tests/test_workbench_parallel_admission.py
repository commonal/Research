from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import sleep
from unittest import TestCase

from research_pulse.workbench.agent_runtime import RunBudgets
from research_pulse.workbench.budget_enforcer import BudgetLedger, RunTerminated
from research_pulse.workbench.tool_dispatcher import (
    Idempotency,
    ParallelPolicy,
    ToolDispatcher,
    ToolEffect,
    ToolPolicy,
    ToolPolicyRegistry,
)
from research_pulse.workbench.tool_policy_adapters import build_workbench_tool_registry


class _ReadFacade:
    capability_names = ("search_sources", "read_managed_blocks", "search_arxiv")

    def invoke(self, name: str, arguments: dict[str, object]) -> object:
        return {"stable_id": f"result:{name}"}


class WorkbenchParallelAdmissionGateTests(TestCase):
    def test_no_read_tool_is_admitted_without_thread_safety_and_ordering_proof(self) -> None:
        registry = build_workbench_tool_registry(_ReadFacade())
        tools = registry.preflight(_ReadFacade.capability_names)

        self.assertTrue(all(
            tool.policy.parallel_policy is ParallelPolicy.SERIAL
            for tool in tools.values()
        ))

    def test_timed_out_handler_cannot_overlap_the_next_serial_tool(self) -> None:
        state = {"active": 0, "maximum": 0}
        guard = Lock()
        first_started = Event()

        def slow() -> str:
            with guard:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                first_started.set()
            sleep(0.05)
            with guard:
                state["active"] -= 1
            return "done"

        registry = ToolPolicyRegistry()
        registry.register("read", slow, policy=ToolPolicy(
            ToolEffect.READ, Idempotency.IDEMPOTENT, 0.01, 0,
            lambda _args: ("source:index",), ParallelPolicy.SERIAL,
        ))
        dispatcher = ToolDispatcher(registry)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                dispatcher.dispatch,
                attempt_id="attempt-1", tool_call_id="call-1",
                tool_name="read", arguments={},
            )
            self.assertTrue(first_started.wait(1))
            second = pool.submit(
                dispatcher.dispatch,
                attempt_id="attempt-1", tool_call_id="call-2",
                tool_name="read", arguments={},
            )
            self.assertEqual("timed_out", first.result().error_code.value)
            self.assertEqual("timed_out", second.result().error_code.value)

        sleep(0.06)
        self.assertEqual(1, state["maximum"])

    def test_budget_and_cancel_gate_concurrent_calls_before_handlers_start(self) -> None:
        calls: list[int] = []
        calls_guard = Lock()

        def read() -> str:
            with calls_guard:
                calls.append(1)
            return "ok"

        registry = ToolPolicyRegistry()
        registry.register("read", read, policy=ToolPolicy(
            ToolEffect.READ, Idempotency.IDEMPOTENT, 1, 0,
            lambda _args: ("source:index",), ParallelPolicy.SERIAL,
        ))
        dispatcher = ToolDispatcher(registry)
        ledger = BudgetLedger(
            RunBudgets(1, 8, 8, 10, 100, 100), attempt_id="attempt-1"
        )
        ledger.request_cancel()

        def invoke(index: int) -> str:
            try:
                dispatcher.dispatch_with_recovery(
                    attempt_id="attempt-1", tool_call_id=f"call-{index}",
                    tool_name="read", arguments={}, budget_ledger=ledger,
                    sleeper=lambda _seconds: None,
                )
            except RunTerminated as exc:
                return exc.reason
            return "started"

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = tuple(pool.map(invoke, range(64)))

        self.assertEqual({"cancelled"}, set(results))
        self.assertEqual([], calls)
        self.assertEqual(0, ledger.snapshot().tool_calls)


if __name__ == "__main__":
    import unittest

    unittest.main()
