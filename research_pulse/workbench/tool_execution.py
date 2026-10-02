"""Structured, persistence-safe outcomes for workbench tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class OperationEffectUnknownError(RuntimeError):
    """A prior write was started, but its durable side effect cannot be proven."""


class ToolOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    CANCELLED = "cancelled"
    EFFECT_UNKNOWN = "effect_unknown"


class Retryability(StrEnum):
    NEVER = "never"
    AUTOMATIC = "automatic"
    AFTER_CORRECTION = "after_correction"
    AFTER_REPLAN = "after_replan"


class SideEffectState(StrEnum):
    NONE = "none"
    NOT_APPLIED = "not_applied"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    PRECONDITION_FAILED = "precondition_failed"
    CONNECTION_FAILED = "connection_failed"
    TIMED_OUT = "timed_out"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    PERMISSION_DENIED = "permission_denied"
    NOT_CONFIGURED = "not_configured"
    REVISION_CONFLICT = "revision_conflict"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EFFECT_UNKNOWN = "effect_unknown"
    INVALID_RESULT = "invalid_result"
    NO_RESULTS = "no_results"
    INTERNAL_INVARIANT = "internal_invariant"


_FORBIDDEN_SAFE_MESSAGE_MARKERS = (
    "traceback",
    "secret-token",
    "authorization:",
    "api_key",
    "api-key",
)


@dataclass(frozen=True)
class ToolOutcome:
    tool_call_id: str
    attempt_id: str
    tool_name: str
    status: ToolOutcomeStatus
    error_code: ToolErrorCode | None
    retryability: Retryability
    side_effect_state: SideEffectState
    started_at: datetime
    finished_at: datetime
    safe_message: str | None = None
    diagnostic_id: str | None = None
    operation_id: str | None = None
    retry_after_seconds: float | None = None
    bounded_result_reference: str | None = None

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (
            self.tool_call_id,
            self.attempt_id,
            self.tool_name,
        )):
            raise ValueError("tool outcome identity fields must not be blank")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after must be non-negative")
        if self.safe_message and any(
            marker in self.safe_message.lower()
            for marker in _FORBIDDEN_SAFE_MESSAGE_MARKERS
        ):
            raise ValueError("safe_message contains forbidden diagnostic content")
        if self.status is ToolOutcomeStatus.SUCCEEDED:
            if self.error_code is not None or self.retryability is not Retryability.NEVER:
                raise ValueError("succeeded outcome cannot contain recovery instructions")
        elif self.error_code is None:
            raise ValueError("failed tool outcome requires a stable error code")
        if self.status is ToolOutcomeStatus.EFFECT_UNKNOWN:
            if (
                self.error_code is not ToolErrorCode.EFFECT_UNKNOWN
                or self.retryability is not Retryability.NEVER
                or self.side_effect_state is not SideEffectState.UNKNOWN
                or not self.operation_id
            ):
                raise ValueError(
                    "effect_unknown requires unknown side effects, operation id, and no retry"
                )
        if self.retryability is Retryability.AUTOMATIC and self.side_effect_state not in {
            SideEffectState.NONE,
            SideEffectState.NOT_APPLIED,
        }:
            raise ValueError("automatic retry requires proof that no side effect was applied")


@dataclass(frozen=True)
class ToolExecutionResult:
    """Durable outcome plus a return value scoped to the current attempt."""

    outcome: ToolOutcome
    ephemeral_value: Any | None
