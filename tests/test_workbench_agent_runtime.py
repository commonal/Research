from __future__ import annotations

from dataclasses import asdict
from unittest import TestCase

from research_pulse.workbench.agent_runtime import (
    AgentRunInput,
    AgentRuntimePort,
    AgentRunResult,
    RunBudgets,
    AgentEvent,
    ToolCapability,
)


class _HarnessCheckpoint:
    def __init__(self) -> None:
        self.internal_state = {"api_key": "secret", "chain_of_thought": "hidden"}


class _HarnessMessage:
    def __init__(self) -> None:
        self.content = "private framework message"


class _ContractRuntime:
    """Test double whose internals deliberately look unlike product DTOs."""

    def __init__(self) -> None:
        self.checkpoint = _HarnessCheckpoint()
        self.message = _HarnessMessage()
        self.cancelled: list[str] = []

    def capabilities(self) -> tuple[ToolCapability, ...]:
        return (ToolCapability("search_sources", read_only=True),)

    def start(self, run_input: AgentRunInput) -> AgentRunResult:
        run_input.on_event(AgentEvent(
            event_type="run_started", summary="探索已开始",
            stable_ids={"run_id": run_input.run_id}, counters={"model_rounds": 0},
        ))
        return AgentRunResult(final_draft="探索草稿", stop_reason="completed")

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class WorkbenchAgentRuntimeContractTests(TestCase):
    def test_run_budgets_allow_zero_block_reads_for_web_only_runs(self) -> None:
        budgets = RunBudgets(4, 8, 0, 180, 30000, 6000)

        self.assertEqual(budgets.block_reads, 0)

    def test_port_uses_framework_free_inputs_capabilities_and_safe_events(self) -> None:
        runtime: AgentRuntimePort = _ContractRuntime()
        events = []
        run_input = AgentRunInput(
            run_id="run-1",
            question="如何治理 Agent 长期记忆？",
            allowed_tools=("search_sources",),
            budgets=RunBudgets(8, 16, 24, 300, 40000, 8000),
            material_source_ids=("2608.21867",),
            on_event=events.append,
        )

        result = runtime.start(run_input)
        event_payload = asdict(events[0])

        self.assertEqual(runtime.capabilities()[0].name, "search_sources")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(event_payload["stable_ids"], {"run_id": "run-1"})
        self.assertNotIn("checkpoint", repr(event_payload).lower())
        self.assertNotIn("secret", repr(event_payload).lower())
        self.assertNotIn("chain_of_thought", repr(event_payload).lower())

    def test_safe_event_rejects_secret_internal_reasoning_and_large_payload_fields(self) -> None:
        for forbidden in ("api_key", "checkpoint", "chain_of_thought", "prompt", "content"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValueError):
                    AgentEvent(
                        event_type="tool_completed",
                        summary="完成",
                        stable_ids={forbidden: "must-not-cross-boundary"},
                    )
