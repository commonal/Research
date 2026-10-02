"""Reusable deterministic fixtures for Workbench Agent fault scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from time import sleep
from research_pulse.workbench.agent_runtime import RunBudgets
from research_pulse.workbench.budget_enforcer import BudgetLedger, BudgetSnapshot
from research_pulse.workbench.run_events import UnsequencedEvent
from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.run_events import AttemptEventStore, PersistedEvent
from research_pulse.workbench.run_models import AttemptStatus, RunSnapshot
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.tool_dispatcher import (
    Idempotency,
    ParallelPolicy,
    ToolDispatcher,
    ToolEffect,
    ToolPolicy,
    ToolPolicyRegistry,
)
from research_pulse.workbench.tool_execution import ToolOutcome


class ControlledClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.origin = datetime(2026, 1, 1, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.origin + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class FinalAnswer:
    text: str


class ScriptedModel:
    def __init__(self, *responses: ToolCall | FinalAnswer) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def respond(self, prompt: str) -> ToolCall | FinalAnswer:
        self.calls.append(prompt)
        if not self._responses:
            raise AssertionError("scripted model has no response left")
        return self._responses.pop(0)


class FakeTool:
    def __init__(self, *results: object | BaseException, delay_seconds: float = 0) -> None:
        self._results = list(results)
        self.delay_seconds = delay_seconds
        self.calls: list[dict[str, object]] = []

    def __call__(self, **arguments: object) -> object:
        self.calls.append(dict(arguments))
        if self.delay_seconds:
            sleep(self.delay_seconds)
        if not self._results:
            raise AssertionError("fake tool has no result left")
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@dataclass(frozen=True)
class ScenarioResult:
    answer: str
    events: tuple[UnsequencedEvent, ...]
    outcomes: tuple[ToolOutcome, ...]
    budget: BudgetSnapshot


class AgentScenario:
    """Small public test harness joining model, tools, clock and budgets."""

    def __init__(
        self,
        model: ScriptedModel,
        tools: dict[str, tuple[FakeTool, ToolPolicy]],
        *,
        clock: ControlledClock | None = None,
        budgets: RunBudgets = RunBudgets(8, 16, 16, 60, 1000, 500),
        recovery_limit: int = 3,
    ) -> None:
        self.model = model
        self.tools = tools
        self.clock = clock or ControlledClock()
        self.budgets = budgets
        self.recovery_limit = recovery_limit

    def run(self, *, attempt_id: str = "attempt-1", question: str = "研究问题") -> ScenarioResult:
        registry = ToolPolicyRegistry()
        for name, (handler, policy) in self.tools.items():
            registry.register(name, handler, policy=policy)
        dispatcher = ToolDispatcher(registry, clock=self.clock.now)
        ledger = BudgetLedger(
            self.budgets,
            recovery_limit=self.recovery_limit,
            attempt_id=attempt_id,
            clock=self.clock.monotonic,
        )
        events: list[UnsequencedEvent] = []
        outcomes: list[ToolOutcome] = []
        prompt = question
        while True:
            reservation = ledger.authorize_model(len(prompt), 32)
            response = self.model.respond(prompt)
            ledger.settle_model(reservation, actual_output_tokens=1)
            if isinstance(response, FinalAnswer):
                events.append(UnsequencedEvent("final_draft", "脚本化模型已完成"))
                ledger.complete()
                return ScenarioResult(response.text, tuple(events), tuple(outcomes), ledger.snapshot())
            events.append(UnsequencedEvent(
                "tool_started", f"调用 {response.tool_name}",
                {"tool_call_id": response.tool_call_id, "tool_name": response.tool_name},
            ))
            outcome = dispatcher.dispatch_with_recovery(
                attempt_id=attempt_id,
                tool_call_id=response.tool_call_id,
                tool_name=response.tool_name,
                arguments=response.arguments,
                budget_ledger=ledger,
                sleeper=self.clock.sleep,
            )
            outcomes.append(outcome)
            events.append(UnsequencedEvent(
                "tool_completed", f"{response.tool_name}: {outcome.status.value}",
                {"tool_call_id": response.tool_call_id, "outcome_status": outcome.status.value},
            ))
            prompt = outcome.status.value


def readonly_policy(*, max_retries: int = 0, timeout_seconds: float = 1) -> ToolPolicy:
    return ToolPolicy(
        ToolEffect.READ,
        Idempotency.IDEMPOTENT,
        timeout_seconds,
        max_retries,
        lambda _arguments: ("source:index",),
        ParallelPolicy.SERIAL,
    )


@dataclass(frozen=True)
class PersistedScenarioResult:
    snapshot: RunSnapshot
    events: tuple[PersistedEvent, ...]
    side_effects: tuple[str, ...]


class PersistedAttemptScenario:
    """Script fixture exposing terminal state, durable events and side effects."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 4, tzinfo=UTC)
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            "session-1", "故障注入", "故障注入", self.now,
        ))
        self.coordinator = ResearchRunCoordinator(
            self.repository,
            run_id_factory=lambda: "run-1",
            attempt_id_factory=lambda: "attempt-1",
            clock=lambda: self.now,
        )
        created = self.coordinator.create(CreateRunCommand(
            "session-1", "验证故障", {}, {"tool_calls": 2},
        ))
        self.run_id = created.run_id
        self.lease = self.repository.claim_next_attempt(
            "worker-a", now=self.now, lease_duration=timedelta(seconds=5),
        )
        self.event_store = AttemptEventStore(self.repository)
        self.side_effects: list[str] = []

    def close(self) -> None:
        self.connection.close()

    def emit(self, event_type: str, summary: str, *, generation: int | None = None) -> None:
        self.event_store.append(
            self.lease.attempt_id,
            self.lease.generation if generation is None else generation,
            UnsequencedEvent(event_type, summary),
        )

    def finish(self, status: AttemptStatus, *, safe_error: str | None = None) -> None:
        self.coordinator.record_attempt_outcome(
            self.lease.attempt_id,
            status,
            expected_generation=self.lease.generation,
            safe_error=safe_error,
        )

    def cancel(self) -> None:
        self.coordinator.cancel_current(
            self.run_id,
            expected_attempt_id=self.lease.attempt_id,
            idempotency_key="cancel-1",
        )

    def reclaim(self):
        return self.repository.claim_next_attempt(
            "worker-b",
            now=self.now + timedelta(seconds=6),
            lease_duration=timedelta(seconds=5),
        )

    def commit_side_effect(self, label: str, *, generation: int | None = None) -> None:
        active_generation = self.lease.generation if generation is None else generation
        operation_id = f"operation:{label}"
        self.repository.begin_coordinated_operation(
            operation_id,
            self.lease.attempt_id,
            expected_generation=active_generation,
            tool_call_id=f"call:{label}",
            resource_key="workspace:workspace-1",
            operation_kind="workspace_patch",
        )
        self.side_effects.append(label)
        self.repository.settle_coordinated_operation(
            operation_id,
            self.lease.attempt_id,
            expected_generation=active_generation,
            status="committed",
            receipt={"patch_id": label, "workspace_revision": 2},
        )

    def result(self) -> PersistedScenarioResult:
        return PersistedScenarioResult(
            snapshot=self.coordinator.inspect(self.run_id),
            events=self.repository.list_attempt_events(self.lease.attempt_id),
            side_effects=tuple(self.side_effects),
        )
