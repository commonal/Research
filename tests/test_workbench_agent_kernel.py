from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.agent_kernel import (
    AttemptContext,
    CapabilityIsolationError,
    ScriptedAgentKernel,
)
from research_pulse.workbench.agent_runtime import RunBudgets, ToolCapability
from research_pulse.workbench.deepagents_kernel import DeepAgentsKernelAdapter
from research_pulse.workbench.cancellation import CancellationToken
from research_pulse.workbench.run_models import AttemptOutcome, AttemptStatus


class _Sink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


class WorkbenchAgentKernelTests(TestCase):
    def test_scripted_adapter_rejects_capabilities_outside_attempt_profile(self) -> None:
        called: list[str] = []
        kernel = ScriptedAgentKernel(
            capabilities_by_profile={
                "readonly": (ToolCapability("search_sources", True),),
                "assistant": (
                    ToolCapability("search_sources", True),
                    ToolCapability("add_evidence", False),
                ),
            },
            execute_script=lambda context, sink, token: (
                called.append(context.attempt_id)
                or AttemptOutcome(context.attempt_id, AttemptStatus.COMPLETED)
            ),
        )
        context = AttemptContext(
            attempt_id="attempt-1", run_id="run-1", generation=1,
            profile="readonly", question="研究问题",
            allowed_tools=("search_sources", "add_evidence"),
            budgets=RunBudgets(2, 3, 4, 30, 1000, 500),
        )

        with self.assertRaises(CapabilityIsolationError):
            kernel.execute(context, _Sink(), CancellationToken())

        self.assertEqual([], called)
        self.assertEqual(("search_sources",), tuple(
            item.name for item in kernel.capabilities("readonly")
        ))

    def test_deepagents_adapter_uses_attempt_identity_and_emits_unsequenced_events(self) -> None:
        observed = {}

        class Runtime:
            def capabilities(self):
                return (ToolCapability("search_sources", True),)

            def start(self, run_input):
                from research_pulse.workbench.agent_runtime import AgentEvent, AgentRunResult
                observed["input"] = run_input
                run_input.on_event(AgentEvent(
                    "run_started", "探索已开始",
                    stable_ids={"run_id": run_input.run_id},
                ))
                return AgentRunResult("草稿", "completed")

            def cancel(self, run_id):
                observed["cancelled"] = run_id

        kernel = DeepAgentsKernelAdapter({"readonly": Runtime()})
        context = AttemptContext(
            attempt_id="attempt-9", run_id="run-3", generation=2,
            profile="readonly", question="问题",
            allowed_tools=("search_sources",),
            budgets=RunBudgets(2, 3, 4, 30, 1000, 500),
        )
        sink = _Sink()

        outcome = kernel.execute(context, sink, CancellationToken())

        self.assertEqual("attempt-9", observed["input"].run_id)
        self.assertEqual("run-3", observed["input"].resolved["logical_run_id"])
        self.assertEqual(AttemptStatus.COMPLETED, outcome.status)
        self.assertEqual("attempt-9", outcome.attempt_id)
        self.assertEqual("草稿", outcome.final_draft)
        self.assertEqual("run_started", sink.events[0].event_type)
        self.assertFalse(hasattr(sink.events[0], "sequence_no"))

    def test_deepagents_adapter_does_not_mark_an_empty_completed_result_successful(self) -> None:
        class Runtime:
            def capabilities(self):
                return (ToolCapability("search_sources", True),)

            def start(self, run_input):
                from research_pulse.workbench.agent_runtime import AgentRunResult
                return AgentRunResult("", "completed")

        kernel = DeepAgentsKernelAdapter({"readonly": Runtime()})
        context = AttemptContext(
            attempt_id="attempt-empty", run_id="run-empty", generation=1,
            profile="readonly", question="问题",
            allowed_tools=("search_sources",),
            budgets=RunBudgets(2, 3, 4, 30, 1000, 500),
        )

        outcome = kernel.execute(context, _Sink(), CancellationToken())

        self.assertEqual(AttemptStatus.TERMINAL_FAILURE, outcome.status)
        self.assertEqual("agent completed without a draft", outcome.safe_error)

    def test_deepagents_adapter_keeps_waiting_for_user_as_a_checkpoint(self) -> None:
        class Runtime:
            def capabilities(self):
                return (ToolCapability("search_sources", True),)

            def start(self, run_input):
                from research_pulse.workbench.agent_runtime import AgentRunResult
                return AgentRunResult("草稿\n\n请选择研究方向。", "awaiting_decision")

        kernel = DeepAgentsKernelAdapter({"readonly": Runtime()})
        context = AttemptContext(
            attempt_id="attempt-awaiting", run_id="run-awaiting", generation=1,
            profile="readonly", question="问题",
            allowed_tools=("search_sources",),
            budgets=RunBudgets(2, 3, 4, 30, 1000, 500),
        )

        outcome = kernel.execute(context, _Sink(), CancellationToken())

        self.assertEqual(AttemptStatus.AWAITING_USER, outcome.status)
        self.assertIsNone(outcome.safe_error)
        self.assertEqual("草稿\n\n请选择研究方向。", outcome.final_draft)

    def test_deepagents_adapter_flattens_resolved_scope_from_config_snapshot(self) -> None:
        observed = {}

        class Runtime:
            def capabilities(self):
                return (ToolCapability("read_run_status", True),)

            def start(self, run_input):
                from research_pulse.workbench.agent_runtime import AgentRunResult
                observed["resolved"] = run_input.resolved
                return AgentRunResult("直接回答", "completed")

        kernel = DeepAgentsKernelAdapter({"readonly": Runtime()})
        context = AttemptContext(
            attempt_id="attempt-scope", run_id="run-scope", generation=1,
            profile="readonly", question="问题",
            allowed_tools=("read_run_status",),
            budgets=RunBudgets(2, 3, 4, 30, 1000, 500),
            input_snapshot={"config": {"resolved": {
                "mode": "direct", "retrieval_plan": "direct",
            }}},
        )

        kernel.execute(context, _Sink(), CancellationToken())

        self.assertEqual("direct", observed["resolved"]["retrieval_plan"])
        self.assertEqual("direct", observed["resolved"]["mode"])


if __name__ == "__main__":
    import unittest
    unittest.main()
