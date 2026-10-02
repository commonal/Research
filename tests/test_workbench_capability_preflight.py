from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.agent_runtime import ToolCapability
from research_pulse.workbench.capability_preflight import (
    CapabilityPreflightError,
    require_exact_readonly_capabilities,
)


class WorkbenchCapabilityPreflightTests(TestCase):
    def setUp(self) -> None:
        self.allowed = (
            "search_sources", "read_paper_metadata",
            "read_managed_blocks", "read_run_status",
        )

    def test_accepts_exact_readonly_inventory(self) -> None:
        inventory = tuple(ToolCapability(name, True) for name in self.allowed)

        receipt = require_exact_readonly_capabilities(inventory, self.allowed)

        self.assertEqual(receipt.allowed, self.allowed)
        self.assertEqual(receipt.actual, self.allowed)

    def test_fails_closed_when_harness_exposes_extra_forbidden_capability(self) -> None:
        for forbidden in ("write_file", "execute", "task", "publish", "subagent"):
            with self.subTest(forbidden=forbidden):
                inventory = tuple(ToolCapability(name, True) for name in self.allowed) + (
                    ToolCapability(forbidden, False),
                )
                with self.assertRaisesRegex(CapabilityPreflightError, "unexpected"):
                    require_exact_readonly_capabilities(inventory, self.allowed)

    def test_fails_closed_for_missing_duplicate_or_mutating_allowed_tool(self) -> None:
        cases = (
            tuple(ToolCapability(name, True) for name in self.allowed[:-1]),
            tuple(ToolCapability(name, True) for name in self.allowed) + (
                ToolCapability(self.allowed[0], True),
            ),
            tuple(
                ToolCapability(name, name != "read_managed_blocks")
                for name in self.allowed
            ),
        )
        for inventory in cases:
            with self.subTest(inventory=inventory):
                with self.assertRaises(CapabilityPreflightError):
                    require_exact_readonly_capabilities(inventory, self.allowed)
