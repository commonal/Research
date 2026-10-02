"""Explicit execution policies for the existing workbench tool facades."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from research_pulse.workbench.tool_dispatcher import (
    Idempotency,
    ParallelPolicy,
    ToolEffect,
    ToolPolicy,
    ToolPolicyRegistry,
)


_WORKSPACE_WRITES = frozenset({
    "update_subquestions",
    "add_evidence",
    "update_research_map",
    "update_research_plan",
    "import_supporting_paper",
})
_REMOTE_READS = frozenset({"search_arxiv", "search_web"})


def build_workbench_tool_registry(
    research_tools: Any,
    workspace_tools: Any | None = None,
    *,
    workspace_id: str | None = None,
) -> ToolPolicyRegistry:
    registry = ToolPolicyRegistry()
    _register_facade(registry, research_tools, workspace_id=None)
    if workspace_tools is not None:
        if not workspace_id or not workspace_id.strip():
            raise ValueError("workspace id is required for workspace tool policies")
        _register_facade(registry, workspace_tools, workspace_id=workspace_id)
    return registry


def _register_facade(
    registry: ToolPolicyRegistry, facade: Any, *, workspace_id: str | None
) -> None:
    names = getattr(facade, "capability_names", None)
    if names is None and getattr(facade, "inner", None) is not None:
        names = getattr(facade.inner, "capability_names", None)
    if names is None:
        raise ValueError("tool facade must declare capability_names")
    for name in names:
        is_write = name in _WORKSPACE_WRITES
        timeout = 60.0 if name in _REMOTE_READS or name == "import_supporting_paper" else 10.0
        policy = ToolPolicy(
            effect=ToolEffect.WRITE if is_write else ToolEffect.READ,
            idempotency=(
                Idempotency.NON_IDEMPOTENT if is_write else Idempotency.IDEMPOTENT
            ),
            timeout_seconds=timeout,
            max_retries=1 if name in _REMOTE_READS else 0,
            resource_keys=(
                (lambda _args, value=workspace_id: (f"workspace:{value}",))
                if workspace_id is not None
                else _research_resource_keys
            ),
            parallel_policy=ParallelPolicy.SERIAL,
        )

        def handler(_name: str = name, **arguments: object) -> object:
            return facade.invoke(_name, arguments)

        registry.register(name, handler, policy=policy)


def _research_resource_keys(arguments: Mapping[str, object]) -> tuple[str, ...]:
    source_id = arguments.get("source_id")
    return (f"source:{source_id}",) if source_id else ("source:index",)
