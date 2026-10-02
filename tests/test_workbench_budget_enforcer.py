from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase

from research_pulse.workbench.agent_runtime import RunBudgets
from research_pulse.workbench.budget_enforcer import (
    BudgetLedger,
    BudgetEnforcer,
    RunTerminated,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class WorkbenchBudgetEnforcerTests(TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.limits = RunBudgets(2, 3, 4, 10, 100, 20)
        self.guard = BudgetEnforcer(self.limits, clock=self.clock)

    def test_authorizes_model_and_tool_work_before_adapter_and_tracks_all_dimensions(self) -> None:
        reservation = self.guard.authorize_model(input_tokens=30, max_output_tokens=10)
        self.guard.settle_model(reservation, actual_output_tokens=6)
        self.guard.authorize_tool("read_managed_blocks", block_reads=2)

        snapshot = self.guard.snapshot()

        self.assertEqual(snapshot.status, "running")
        self.assertEqual(snapshot.model_rounds, 1)
        self.assertEqual(snapshot.tool_calls, 1)
        self.assertEqual(snapshot.block_reads, 2)
        self.assertEqual(snapshot.input_tokens, 30)
        self.assertEqual(snapshot.output_tokens, 6)

    def test_each_hard_budget_fails_before_usage_can_exceed_limit(self) -> None:
        scenarios = (
            ("model_rounds", lambda g: (g.authorize_model(1, 1), g.authorize_model(1, 1), g.authorize_model(1, 1))),
            ("tool_calls", lambda g: (g.authorize_tool("search_sources"), g.authorize_tool("search_sources"), g.authorize_tool("search_sources"), g.authorize_tool("search_sources"))),
            ("block_reads", lambda g: g.authorize_tool("read_managed_blocks", block_reads=5)),
            ("input_tokens", lambda g: g.authorize_model(101, 1)),
            ("output_tokens", lambda g: g.authorize_model(1, 21)),
        )
        for dimension, action in scenarios:
            with self.subTest(dimension=dimension):
                guard = BudgetEnforcer(self.limits, clock=self.clock)
                with self.assertRaises(RunTerminated) as raised:
                    action(guard)
                self.assertEqual(raised.exception.reason, "budget_exhausted")
                self.assertEqual(raised.exception.dimension, dimension)
                self.assertEqual(guard.snapshot().status, "budget_exhausted")

    def test_wall_clock_and_user_cancel_are_terminal_and_reject_later_actions(self) -> None:
        self.clock.value += 11
        with self.assertRaises(RunTerminated) as wall:
            self.guard.authorize_tool("search_sources")
        self.assertEqual(wall.exception.dimension, "wall_seconds")

        cancelled = BudgetEnforcer(self.limits, clock=self.clock)
        cancelled.request_cancel()
        with self.assertRaises(RunTerminated) as stopped:
            cancelled.authorize_model(1, 1)
        self.assertEqual(stopped.exception.reason, "cancelled")
        self.assertEqual(cancelled.snapshot().status, "cancelled")

    def test_settlement_cannot_exceed_reserved_output_and_completion_is_terminal(self) -> None:
        reservation = self.guard.authorize_model(input_tokens=1, max_output_tokens=5)
        with self.assertRaises(ValueError):
            self.guard.settle_model(reservation, actual_output_tokens=6)
        self.guard.settle_model(reservation, actual_output_tokens=5)
        self.guard.complete()
        with self.assertRaises(RunTerminated) as stopped:
            self.guard.authorize_tool("search_sources")
        self.assertEqual(stopped.exception.reason, "completed")

    def test_recovery_budget_is_attempt_scoped_and_new_attempt_starts_fresh(self) -> None:
        first = BudgetLedger(self.limits, recovery_limit=1, attempt_id="attempt-1", clock=self.clock)
        first.authorize_recovery("network_retry")
        with self.assertRaises(RunTerminated) as exhausted:
            first.authorize_recovery("network_retry")
        self.assertEqual("recovery_attempts", exhausted.exception.dimension)

        second = BudgetLedger(self.limits, recovery_limit=1, attempt_id="attempt-2", clock=self.clock)
        second.authorize_recovery("network_retry")
        self.assertEqual(1, second.snapshot().recovery_attempts)

    def test_concurrent_authorization_never_exceeds_attempt_limit(self) -> None:
        limits = RunBudgets(20, 10, 20, 10, 100, 20)
        ledger = BudgetLedger(limits, attempt_id="attempt-concurrent", clock=self.clock)

        def authorize(_index: int) -> bool:
            try:
                ledger.authorize_tool("search_sources")
                return True
            except RunTerminated:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(authorize, range(30)))

        self.assertEqual(10, results.count(True))
        self.assertEqual(10, ledger.snapshot().tool_calls)

    def test_experimental_mode_does_not_stop_on_application_dimensions(self) -> None:
        limits = RunBudgets(
            0, 0, 0, 10, 0, 0, experimental_unbounded=True
        )
        ledger = BudgetLedger(limits, clock=self.clock)

        reservation = ledger.authorize_model(input_tokens=500, max_output_tokens=400)
        ledger.settle_model(reservation, actual_output_tokens=321)
        ledger.authorize_tool("read_managed_blocks", block_reads=200)

        snapshot = ledger.snapshot()
        self.assertEqual("running", snapshot.status)
        self.assertEqual(1, snapshot.model_rounds)
        self.assertEqual(1, snapshot.tool_calls)
        self.assertEqual(200, snapshot.block_reads)
        self.assertEqual(500, snapshot.input_tokens)
        self.assertEqual(321, snapshot.output_tokens)
        self.assertGreater(ledger.remaining("output_tokens"), 1_000_000)

    def test_experimental_mode_still_enforces_wall_clock(self) -> None:
        limits = RunBudgets(
            0, 0, 0, 10, 0, 0, experimental_unbounded=True
        )
        ledger = BudgetLedger(limits, clock=self.clock)
        self.clock.value += 11
        with self.assertRaises(RunTerminated) as raised:
            ledger.authorize_tool("search_sources")
        self.assertEqual("wall_seconds", raised.exception.dimension)
