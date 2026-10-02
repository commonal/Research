"""Startup gate that requires an exact, read-only Harness inventory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from research_pulse.workbench.agent_runtime import ToolCapability


class CapabilityPreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapabilityPreflightReceipt:
    allowed: tuple[str, ...]
    actual: tuple[str, ...]


def require_exact_readonly_capabilities(
    inventory: Sequence[ToolCapability], allowed_tools: Sequence[str]
) -> CapabilityPreflightReceipt:
    allowed = tuple(allowed_tools)
    actual = tuple(item.name for item in inventory)
    if not allowed or len(set(allowed)) != len(allowed):
        raise CapabilityPreflightError("allowed capability policy is invalid")
    if len(set(actual)) != len(actual):
        raise CapabilityPreflightError("duplicate capability declaration")
    unexpected = sorted(set(actual) - set(allowed))
    if unexpected:
        raise CapabilityPreflightError(
            "unexpected Harness capabilities: " + ", ".join(unexpected)
        )
    missing = sorted(set(allowed) - set(actual))
    if missing:
        raise CapabilityPreflightError(
            "required Harness capabilities are missing: " + ", ".join(missing)
        )
    mutating = sorted(item.name for item in inventory if not item.read_only)
    if mutating:
        raise CapabilityPreflightError(
            "Harness capabilities are not read-only: " + ", ".join(mutating)
        )
    return CapabilityPreflightReceipt(allowed=allowed, actual=actual)
