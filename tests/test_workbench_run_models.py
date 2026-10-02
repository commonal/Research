from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.run_models import (
    Attempt,
    AttemptOutcome,
    AttemptStatus,
    AttemptTransitionError,
    ResearchRun,
    RunSnapshot,
)


class WorkbenchRunModelTests(TestCase):
    def test_research_run_and_attempt_outcome_are_validated_contracts(self) -> None:
        run = ResearchRun("run-1", "session-1", "问题", {"mode": "deep"})
        outcome = AttemptOutcome(
            "attempt-1",
            AttemptStatus.RETRYABLE_FAILURE,
            safe_error="网络暂时不可用",
            diagnostic_id="diag-1",
            budget_used={"tool_calls": 2},
        )

        self.assertEqual("run-1", run.run_id)
        self.assertEqual(2, outcome.budget_used["tool_calls"])
        with self.assertRaisesRegex(ValueError, "terminal"):
            AttemptOutcome("attempt-2", AttemptStatus.RUNNING)

    def test_terminal_attempt_cannot_be_reopened_or_overwritten(self) -> None:
        exhausted = Attempt(
            attempt_id="attempt-1",
            run_id="run-1",
            attempt_no=1,
            status=AttemptStatus.BUDGET_EXHAUSTED,
            budgets={"tool_calls": 4},
            safe_error="预算已耗尽",
        )

        with self.assertRaisesRegex(
            AttemptTransitionError,
            "budget_exhausted -> queued",
        ):
            exhausted.transition(AttemptStatus.QUEUED)

        self.assertEqual(AttemptStatus.BUDGET_EXHAUSTED, exhausted.status)
        self.assertEqual("预算已耗尽", exhausted.safe_error)

    def test_run_snapshot_requires_current_attempt_to_end_history(self) -> None:
        first = Attempt("attempt-1", "run-1", 1, AttemptStatus.BUDGET_EXHAUSTED)
        second = Attempt("attempt-2", "run-1", 2, AttemptStatus.QUEUED)

        snapshot = RunSnapshot("run-1", "session-1", "问题", second, (first, second))

        self.assertEqual(AttemptStatus.QUEUED, snapshot.status)
        with self.assertRaisesRegex(ValueError, "current attempt"):
            RunSnapshot("run-1", "session-1", "问题", first, (first, second))


if __name__ == "__main__":
    import unittest

    unittest.main()
