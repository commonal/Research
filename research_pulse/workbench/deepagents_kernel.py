"""Adapter from the legacy Deep Agents runtime to the Attempt-scoped kernel seam."""

from __future__ import annotations

from collections.abc import Mapping

from research_pulse.workbench.agent_kernel import (
    AgentKernel,
    AttemptContext,
    CapabilityIsolationError,
    EventSink,
)
from research_pulse.workbench.agent_runtime import AgentRunInput, AgentRuntimePort, ToolCapability
from research_pulse.workbench.cancellation import CancellationToken
from research_pulse.workbench.run_events import UnsequencedEvent
from research_pulse.workbench.run_models import AttemptOutcome, AttemptStatus


_OUTCOME_STATUS = {
    "completed": AttemptStatus.COMPLETED,
    "failed": AttemptStatus.TERMINAL_FAILURE,
    "cancelled": AttemptStatus.CANCELLED,
    "budget_exhausted": AttemptStatus.BUDGET_EXHAUSTED,
    "awaiting_decision": AttemptStatus.AWAITING_USER,
}


class DeepAgentsKernelAdapter(AgentKernel):
    """Run one physical Attempt while hiding the legacy runtime interface."""

    def __init__(self, runtimes_by_profile: Mapping[str, AgentRuntimePort]) -> None:
        self._runtimes = dict(runtimes_by_profile)

    def capabilities(self, profile: str) -> tuple[ToolCapability, ...]:
        runtime = self._runtimes.get(profile)
        return runtime.capabilities() if runtime is not None else ()

    def execute(
        self,
        context: AttemptContext,
        event_sink: EventSink,
        cancellation: CancellationToken,
    ) -> AttemptOutcome:
        runtime = self._runtimes.get(context.profile)
        if runtime is None:
            raise CapabilityIsolationError(f"unknown agent profile: {context.profile}")
        exposed = {item.name for item in runtime.capabilities()}
        unavailable = set(context.allowed_tools) - exposed
        if unavailable:
            raise CapabilityIsolationError(
                "attempt requested unavailable tools: " + ", ".join(sorted(unavailable))
            )
        cancellation.raise_if_cancelled()

        def emit(event) -> None:
            if cancellation.cancelled:
                cancellation.raise_if_cancelled()
            event_sink.emit(UnsequencedEvent(
                event.event_type,
                event.summary,
                payload={
                    "stable_ids": dict(event.stable_ids),
                    "counters": dict(event.counters),
                    "extras": tuple(event.extras),
                },
            ))

        resolved = dict(context.input_snapshot)
        config = context.input_snapshot.get("config")
        if isinstance(config, Mapping):
            configured_scope = config.get("resolved")
            if isinstance(configured_scope, Mapping):
                # CreateRunCommand stores the resolver output under the
                # immutable config snapshot. Flatten it at the kernel seam so
                # every runtime sees the same explicit retrieval plan.
                resolved.update(configured_scope)
        resolved.update(context.recovery_facts)
        resolved["logical_run_id"] = context.run_id
        resolved["attempt_generation"] = context.generation
        result = runtime.start(AgentRunInput(
            run_id=context.attempt_id,
            question=context.question,
            allowed_tools=context.allowed_tools,
            budgets=context.budgets,
            resolved=resolved,
            on_event=emit,
        ))
        cancellation.raise_if_cancelled()
        stop_status = _OUTCOME_STATUS[result.stop_reason]
        # ``awaiting_decision`` is a successful, user-visible checkpoint, not
        # an agent failure.  Keeping the generic stop message here makes the
        # API project a failure card even though a draft and decision point
        # were persisted.  Only actual stop/failure reasons should populate
        # the safe error field.
        safe_error = (
            None
            if result.stop_reason in {"completed", "awaiting_decision"}
            else "agent attempt stopped"
        )
        # A completed run without a user-visible draft is not a successful
        # research result. Treat it as a terminal failure so the UI can expose
        # a retryable diagnosis instead of showing an empty completed card.
        if result.stop_reason == "completed" and not (result.final_draft or "").strip():
            stop_status = AttemptStatus.TERMINAL_FAILURE
            safe_error = "agent completed without a draft"
        return AttemptOutcome(
            context.attempt_id,
            stop_status,
            safe_error=safe_error,
            final_draft=result.final_draft,
        )
