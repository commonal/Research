"""Attempt-scoped, framework-free seam for research agent execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from research_pulse.workbench.agent_runtime import RunBudgets, ToolCapability
from research_pulse.workbench.cancellation import CancellationToken
from research_pulse.workbench.run_events import UnsequencedEvent
from research_pulse.workbench.run_models import AttemptOutcome


class CapabilityIsolationError(ValueError):
    """Raised before execution when an Attempt requests tools outside its profile."""


class EventSink(Protocol):
    def emit(self, event: UnsequencedEvent) -> None: ...


@dataclass(frozen=True)
class AttemptContext:
    attempt_id: str
    run_id: str
    generation: int
    profile: str
    question: str
    allowed_tools: tuple[str, ...]
    budgets: RunBudgets
    input_snapshot: Mapping[str, object] = field(default_factory=dict)
    recovery_facts: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (
            self.attempt_id, self.run_id, self.profile, self.question,
        )):
            raise ValueError("attempt execution identity and question are required")
        if self.generation < 1:
            raise ValueError("attempt generation must be positive")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed tools must be unique")
        object.__setattr__(self, "input_snapshot", MappingProxyType(dict(self.input_snapshot)))
        object.__setattr__(self, "recovery_facts", MappingProxyType(dict(self.recovery_facts)))


class AgentKernel(Protocol):
    def execute(
        self,
        context: AttemptContext,
        event_sink: EventSink,
        cancellation: CancellationToken,
    ) -> AttemptOutcome: ...

    def capabilities(self, profile: str) -> tuple[ToolCapability, ...]: ...


class ScriptedAgentKernel:
    """Deterministic adapter for scenario and fault-injection tests."""

    def __init__(
        self,
        *,
        capabilities_by_profile: Mapping[str, tuple[ToolCapability, ...]],
        execute_script: Callable[
            [AttemptContext, EventSink, CancellationToken], AttemptOutcome
        ],
    ) -> None:
        self._capabilities = {
            profile: tuple(capabilities)
            for profile, capabilities in capabilities_by_profile.items()
        }
        self._execute_script = execute_script

    def capabilities(self, profile: str) -> tuple[ToolCapability, ...]:
        return self._capabilities.get(profile, ())

    def execute(
        self,
        context: AttemptContext,
        event_sink: EventSink,
        cancellation: CancellationToken,
    ) -> AttemptOutcome:
        exposed = {item.name for item in self.capabilities(context.profile)}
        unavailable = set(context.allowed_tools) - exposed
        if unavailable:
            raise CapabilityIsolationError(
                "attempt requested unavailable tools: " + ", ".join(sorted(unavailable))
            )
        cancellation.raise_if_cancelled()
        outcome = self._execute_script(context, event_sink, cancellation)
        if outcome.attempt_id != context.attempt_id:
            raise ValueError("kernel outcome belongs to a different attempt")
        return outcome
