"""M3.9 — fixed CapabilityPolicy + verify_capability_policy preflight consistency.

The literature/V0 surface keeps ``require_exact_readonly_capabilities``. The
assistant surface is verified against a fixed ``CapabilityPolicy`` so the
runtime can build ``actual`` dynamically (e.g. ``import_supporting_paper`` only
when an importer is available) while the preflight still fail-closes on a
forbidden / unexpected / missing-required tool. This module is orthogonal to
``WorkspaceRiskClassifier``.
"""

from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.agent_runtime import ToolCapability
from research_pulse.workbench.capability_preflight import CapabilityPreflightError
from research_pulse.workbench.capability_policy import (
    CapabilityPolicy,
    ConditionalCapability,
    assistant_capability_policy,
    verify_capability_policy,
    web_lookup_capability_policy,
)


# The current assistant tool surface (mirrors deepagents_v0.ALLOWED_TOOLS_ASSISTANT).
ASSISTANT_SURFACE = (
    "search_sources", "read_paper_metadata", "read_managed_blocks",
    "read_run_status", "search_arxiv", "ls", "read_file", "write_file",
    "edit_file", "glob", "grep", "write_todos", "read_workspace_state",
    "update_subquestions", "add_evidence", "update_research_map", "update_research_plan",
    "import_supporting_paper",
)


def _caps(names: list[str] | tuple[str, ...]) -> tuple[ToolCapability, ...]:
    return tuple(ToolCapability(name, name in _READ_ONLY) for name in names)


_READ_ONLY = frozenset(
    {
        "search_sources", "read_paper_metadata", "read_managed_blocks",
        "read_run_status", "search_arxiv", "ls", "read_file", "glob", "grep",
        "write_todos", "read_workspace_state",
    }
)


class CapabilityPolicyTests(TestCase):
    def setUp(self) -> None:
        self.policy = assistant_capability_policy()

    def test_full_assistant_surface_with_importer_passes(self) -> None:
        inventory = _caps(ASSISTANT_SURFACE)
        receipt = verify_capability_policy(
            self.policy, inventory, criteria={"importer_present": True, "workspace_present": True}
        )
        self.assertIn("import_supporting_paper", receipt.conditional_present)
        # The controlled canonical-write channel is conditional on an attached workspace.
        self.assertIn("update_subquestions", receipt.conditional_present)
        self.assertIn("write_file", receipt.allowed)

    def test_readonly_assistant_surface_without_workspace_or_importer_passes(self) -> None:
        # A fresh session with no bound paper / no importer gets the read-only
        # assistant surface; the legal absence of the workspace write tools and
        # import is NOT a missing-required failure.
        inventory = _caps(
            tuple(name for name in ASSISTANT_SURFACE if name not in (
                "read_workspace_state", "update_subquestions", "add_evidence",
                "update_research_map", "update_research_plan", "import_supporting_paper", "write_file", "edit_file",
            ))
        )
        receipt = verify_capability_policy(
            self.policy, inventory, criteria={"importer_present": False, "workspace_present": False}
        )
        self.assertEqual(receipt.conditional_present, ())
        self.assertIn("read_managed_blocks", receipt.actual)

    def test_required_tool_missing_fails_closed(self) -> None:
        # A genuinely required tool (the read-evidence entry, not a workspace tool)
        # being absent fails closed.
        inventory = _caps(tuple(name for name in ASSISTANT_SURFACE if name != "read_managed_blocks"))
        with self.assertRaisesRegex(CapabilityPreflightError, "missing"):
            verify_capability_policy(
                self.policy, inventory, criteria={"importer_present": True, "workspace_present": True}
            )

    def test_forbidden_tool_present_fails_closed(self) -> None:
        inventory = _caps(ASSISTANT_SURFACE + ("execute",))
        with self.assertRaisesRegex(CapabilityPreflightError, "forbidden"):
            verify_capability_policy(
                self.policy, inventory, criteria={"importer_present": True, "workspace_present": True}
            )

    def test_unexpected_tool_not_declared_fails_closed(self) -> None:
        inventory = _caps(ASSISTANT_SURFACE + ("publish",))
        with self.assertRaisesRegex(CapabilityPreflightError, "unexpected"):
            verify_capability_policy(
                self.policy, inventory, criteria={"importer_present": True, "workspace_present": True}
            )

    def test_conditional_present_when_importer_present_is_mandatory(self) -> None:
        inventory = _caps(
            tuple(name for name in ASSISTANT_SURFACE if name != "import_supporting_paper")
        )
        with self.assertRaisesRegex(CapabilityPreflightError, "must be present"):
            verify_capability_policy(
                self.policy, inventory, criteria={"importer_present": True, "workspace_present": True}
            )

    def test_conditional_absent_when_importer_unavailable_is_mandatory(self) -> None:
        with self.assertRaisesRegex(CapabilityPreflightError, "must be absent"):
            verify_capability_policy(
                self.policy, _caps(ASSISTANT_SURFACE), criteria={"importer_present": False, "workspace_present": True}
            )

    def test_missing_criterion_fails_closed(self) -> None:
        with self.assertRaisesRegex(CapabilityPreflightError, "unresolved"):
            verify_capability_policy(self.policy, _caps(ASSISTANT_SURFACE), criteria={})

    def test_duplicate_declaration_fails_closed(self) -> None:
        inventory = _caps(ASSISTANT_SURFACE)
        with self.assertRaisesRegex(CapabilityPreflightError, "duplicate"):
            verify_capability_policy(
                self.policy, _caps(ASSISTANT_SURFACE + ("read_file",)),
                criteria={"importer_present": True, "workspace_present": True},
            )

    def test_policy_buckets_do_not_overlap(self) -> None:
        with self.assertRaises(CapabilityPreflightError):
            CapabilityPolicy(
                required=("a",),
                allowed=("a",),
                forbidden=("execute",),
            )

    def test_duplicate_capability_policy_reports_both_categories(self) -> None:
        with self.assertRaisesRegex(CapabilityPreflightError, "both"):
            CapabilityPolicy(required=("x",), conditional=(ConditionalCapability("x", "k"),))

    def test_web_lookup_requires_web_and_forbids_paper_search(self) -> None:
        policy = web_lookup_capability_policy()
        inventory = _caps(("search_web",))
        receipt = verify_capability_policy(
            policy, inventory, criteria={"workspace_present": False}
        )
        self.assertIn("search_web", receipt.required)
        with self.assertRaisesRegex(CapabilityPreflightError, "forbidden"):
            verify_capability_policy(
                policy,
                _caps((
                    "search_web", "search_arxiv", "read_paper_metadata",
                    "read_managed_blocks", "read_run_status",
                )),
                criteria={"workspace_present": False},
            )
