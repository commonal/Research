from __future__ import annotations

from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.tool_execution import (
    Retryability,
    SideEffectState,
    ToolErrorCode,
    ToolOutcome,
    ToolOutcomeStatus,
)


class WorkbenchToolOutcomeTests(TestCase):
    def make_outcome(self, **changes: object) -> ToolOutcome:
        values: dict[str, object] = {
            "tool_call_id": "call-1",
            "attempt_id": "attempt-1",
            "tool_name": "search_sources",
            "status": ToolOutcomeStatus.REJECTED,
            "error_code": ToolErrorCode.INVALID_ARGUMENT,
            "retryability": Retryability.AFTER_CORRECTION,
            "side_effect_state": SideEffectState.NONE,
            "started_at": datetime(2026, 9, 4, tzinfo=UTC),
            "finished_at": datetime(2026, 9, 4, 0, 0, 1, tzinfo=UTC),
            "safe_message": "参数无效",
            "diagnostic_id": "diag-1",
        }
        values.update(changes)
        return ToolOutcome(**values)  # type: ignore[arg-type]

    def test_failure_categories_keep_distinct_recovery_semantics(self) -> None:
        rejected = self.make_outcome()
        limited = self.make_outcome(
            status=ToolOutcomeStatus.RETRYABLE_FAILURE,
            error_code=ToolErrorCode.RATE_LIMITED,
            retryability=Retryability.AUTOMATIC,
            retry_after_seconds=2.0,
        )
        unavailable = self.make_outcome(
            status=ToolOutcomeStatus.RETRYABLE_FAILURE,
            error_code=ToolErrorCode.SERVICE_UNAVAILABLE,
            retryability=Retryability.AUTOMATIC,
        )
        auth = self.make_outcome(
            status=ToolOutcomeStatus.TERMINAL_FAILURE,
            error_code=ToolErrorCode.PERMISSION_DENIED,
            retryability=Retryability.NEVER,
        )
        conflict = self.make_outcome(
            error_code=ToolErrorCode.REVISION_CONFLICT,
            retryability=Retryability.AFTER_REPLAN,
            side_effect_state=SideEffectState.NOT_APPLIED,
        )
        unknown = self.make_outcome(
            status=ToolOutcomeStatus.EFFECT_UNKNOWN,
            error_code=ToolErrorCode.EFFECT_UNKNOWN,
            retryability=Retryability.NEVER,
            side_effect_state=SideEffectState.UNKNOWN,
            operation_id="operation-1",
        )

        self.assertEqual(ToolOutcomeStatus.REJECTED, rejected.status)
        self.assertEqual(Retryability.AUTOMATIC, limited.retryability)
        self.assertEqual(ToolErrorCode.SERVICE_UNAVAILABLE, unavailable.error_code)
        self.assertEqual(Retryability.NEVER, auth.retryability)
        self.assertEqual(Retryability.AFTER_REPLAN, conflict.retryability)
        self.assertEqual(SideEffectState.UNKNOWN, unknown.side_effect_state)

    def test_illegal_or_unsafe_combinations_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "effect_unknown"):
            self.make_outcome(
                status=ToolOutcomeStatus.EFFECT_UNKNOWN,
                error_code=ToolErrorCode.EFFECT_UNKNOWN,
                retryability=Retryability.AUTOMATIC,
                side_effect_state=SideEffectState.UNKNOWN,
                operation_id="operation-1",
            )

        with self.assertRaisesRegex(ValueError, "retry_after"):
            self.make_outcome(retry_after_seconds=-1)

        with self.assertRaisesRegex(ValueError, "forbidden"):
            self.make_outcome(safe_message="traceback: secret-token")


if __name__ == "__main__":
    import unittest

    unittest.main()
