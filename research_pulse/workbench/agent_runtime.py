"""Framework-free product port for a replaceable research Agent Harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol


AgentEventType = Literal[
    "run_started", "phase_changed", "tool_started", "tool_completed",
    "source_discovered", "context_read", "budget_updated", "final_draft",
    "run_failed", "run_cancelled",
    "operation_started", "operation_completed", "note_completed", "note_failed",
    "file_written", "plan_updated", "awaiting_decision",
]
StopReason = Literal["completed", "failed", "cancelled", "budget_exhausted", "awaiting_decision"]

_FORBIDDEN_EVENT_KEYS = frozenset({
    "api_key", "token", "secret", "password", "checkpoint",
    "chain_of_thought", "reasoning", "prompt", "content", "path",
})


@dataclass(frozen=True)
class ToolCapability:
    name: str
    read_only: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("capability name must not be blank")


@dataclass(frozen=True)
class RunBudgets:
    model_rounds: int
    tool_calls: int
    block_reads: int
    wall_seconds: int
    input_tokens: int
    output_tokens: int
    # Explicit local-test escape hatch.  It disables application dimensions
    # (steps, tools, reads and token counters) while retaining wall-clock and
    # recovery safety.  Production profiles leave this false.
    experimental_unbounded: bool = False

    def __post_init__(self) -> None:
        # A web-only run deliberately has no paper-block read budget.  Keep
        # every executable budget strictly positive, but allow zero for this
        # capability-specific counter so it can express "forbidden" rather
        # than forcing a fake read allowance.
        if self.experimental_unbounded:
            invalid = self.wall_seconds <= 0 or self.block_reads < 0
        else:
            invalid = (
                any(value <= 0 for value in (
                    self.model_rounds, self.tool_calls,
                    self.wall_seconds, self.input_tokens, self.output_tokens,
                ))
                or self.block_reads < 0
            )
        if invalid:
            raise ValueError("run budgets must be positive; block_reads may be zero")


@dataclass(frozen=True)
class AgentRunInput:
    run_id: str
    question: str
    allowed_tools: tuple[str, ...]
    budgets: RunBudgets
    material_source_ids: tuple[str, ...] = ()
    # Optional resolved interaction scope (mode + evidence scope + selection
    # anchor), produced by the Scope Resolver before the agent runs. When None
    # the harness behaves exactly as it did before (no narrowing, no context
    # injection). When present the runtime picks the tool surface, prompt and
    # injected evidence context for that mode.
    resolved: dict[str, object] | None = None
    # Optional live-event sink. When provided, the runtime invokes it with each
    # event at the moment it is produced (before the run finishes) so the
    # executor can persist-first and the workbench can stream progress live.
    on_event: Callable[[AgentEvent], None] | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.question.strip():
            raise ValueError("run id and question must not be blank")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed tools must be unique")


@dataclass(frozen=True)
class SafeAgentEvent:
    sequence_no: int
    event_type: AgentEventType
    summary: str
    stable_ids: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    # Optional rich payload attached to one event (e.g. the arXiv candidate
    # records a search returned). Kept out of summary/stable_ids so the
    # sanitized surface stays unchanged; used to seed continuation runs.
    extras: tuple[dict[str, object], ...] = ()
    # Attempt identity is required when a run contains multiple attempts:
    # sequence_no is scoped to one attempt and therefore cannot be used as a
    # globally unique React key by itself. Keep it optional for old callers
    # that construct SafeAgentEvent directly.
    attempt_id: str = ""

    def __post_init__(self) -> None:
        if self.sequence_no < 1 or not self.summary.strip():
            raise ValueError("safe event requires sequence and summary")
        keys = {key.lower() for key in (*self.stable_ids.keys(), *self.counters.keys())}
        if keys & _FORBIDDEN_EVENT_KEYS:
            raise ValueError("unsafe event field")
        if len(self.summary) > 500:
            raise ValueError("event summary is too large")
        if any(value < 0 for value in self.counters.values()):
            raise ValueError("event counters must not be negative")


@dataclass(frozen=True)
class AgentEvent:
    """Ephemeral runtime event; persistent ordering belongs to EventStore."""

    event_type: AgentEventType
    summary: str
    stable_ids: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    extras: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not self.summary.strip() or len(self.summary) > 500:
            raise ValueError("agent event requires a bounded summary")
        keys = {key.lower() for key in (*self.stable_ids.keys(), *self.counters.keys())}
        if keys & _FORBIDDEN_EVENT_KEYS:
            raise ValueError("unsafe event field")
        if any(value < 0 for value in self.counters.values()):
            raise ValueError("event counters must not be negative")


@dataclass(frozen=True)
class AgentRunResult:
    final_draft: str | None
    stop_reason: StopReason


class AgentRuntimePort(Protocol):
    def capabilities(self) -> tuple[ToolCapability, ...]: ...
    def start(self, run_input: AgentRunInput) -> AgentRunResult: ...
