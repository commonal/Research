from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.tool_dispatcher import ToolDispatcher, ToolEffect
from research_pulse.workbench.tool_policy_adapters import build_workbench_tool_registry


class _Facade:
    def __init__(self, names: tuple[str, ...], outcomes: dict[str, object] | None = None) -> None:
        self.capability_names = names
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def invoke(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        outcome = self.outcomes.get(name, {"stable_id": "result-1"})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class WorkbenchToolPolicyAdapterTests(TestCase):
    def test_existing_research_and_workspace_tools_receive_explicit_policies(self) -> None:
        research = _Facade(("search_sources", "read_managed_blocks", "search_arxiv"))
        workspace = _Facade(("read_workspace_state", "add_evidence"))

        registry = build_workbench_tool_registry(
            research, workspace, workspace_id="workspace-1"
        )
        tools = registry.preflight(research.capability_names + workspace.capability_names)

        self.assertEqual(ToolEffect.READ, tools["search_sources"].policy.effect)
        self.assertEqual(ToolEffect.READ, tools["read_workspace_state"].policy.effect)
        self.assertEqual(ToolEffect.WRITE, tools["add_evidence"].policy.effect)
        self.assertEqual(
            ("workspace:workspace-1",),
            tuple(tools["add_evidence"].policy.resource_keys({})),
        )

    def test_registered_facade_failures_produce_distinct_outcomes(self) -> None:
        research = _Facade(
            ("ok", "rejected", "denied"),
            {"rejected": ValueError("bad args"), "denied": PermissionError("secret")},
        )
        dispatcher = ToolDispatcher(build_workbench_tool_registry(research))

        outcomes = {
            name: dispatcher.dispatch(
                attempt_id="attempt-1", tool_call_id=f"call-{name}",
                tool_name=name, arguments={},
            )
            for name in research.capability_names
        }

        self.assertEqual("succeeded", outcomes["ok"].status.value)
        self.assertEqual("rejected", outcomes["rejected"].status.value)
        self.assertEqual("terminal_failure", outcomes["denied"].status.value)

    def test_dispatcher_exposes_the_same_single_facade_result_to_the_model(self) -> None:
        value = [{"source_id": "source-1", "title": "Paper"}]
        research = _Facade(("search_sources",), {"search_sources": value})
        dispatcher = ToolDispatcher(build_workbench_tool_registry(research))

        execution = dispatcher.execute(
            attempt_id="attempt-1", tool_call_id="call-1",
            tool_name="search_sources", arguments={"query": "DPO"},
        )

        self.assertEqual("succeeded", execution.outcome.status.value)
        self.assertIs(value, execution.ephemeral_value)
        self.assertEqual([("search_sources", {"query": "DPO"})], research.calls)


if __name__ == "__main__":
    import unittest

    unittest.main()
