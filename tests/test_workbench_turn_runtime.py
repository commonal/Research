from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.agent_runtime import RunBudgets, ToolCapability
from research_pulse.workbench.capability_preflight import CapabilityPreflightError
from research_pulse.workbench.scope_resolver import InteractionContext
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    CapabilityProfile,
    TurnRequest,
    TurnResult,
    TurnValidationError,
    default_capability_registry,
)


class UnifiedTurnRuntimeContractTests(TestCase):
    def test_turn_request_round_trips_context_and_rejects_invalid_context(self) -> None:
        request = TurnRequest(
            session_id="session-1",
            message="解释当前选区",
            interaction_context=InteractionContext(surface="paper_reader"),
            explicit_action="explain",
            client_request_id="req-1",
            metadata={"source": "reader"},
        )
        restored = TurnRequest.from_dict(request.to_dict())
        self.assertEqual(restored.session_id, request.session_id)
        self.assertEqual(restored.interaction_context.surface, "paper_reader")
        self.assertEqual(restored.metadata["source"], "reader")

        with self.assertRaises(TurnValidationError):
            TurnRequest.from_dict({"session_id": "s", "message": "q", "interaction_context": {
                "selection": {"paper_id": "p"},
            }})

    def test_profiles_are_four_named_capability_snapshots(self) -> None:
        registry = default_capability_registry()
        self.assertEqual(set(registry.names()), {"basic", "paper", "web", "research"})
        self.assertEqual(registry.get("basic").default_execution_mode, "sync")
        self.assertEqual(registry.get("research").default_execution_mode, "durable")
        self.assertNotIn("search_web", registry.get("basic").allowed_tools)
        self.assertNotIn("search_sources", registry.get("paper").allowed_tools)

    def test_durable_profiles_declare_the_full_worker_budget_contract(self) -> None:
        required = {
            "model_rounds", "tool_calls", "block_reads",
            "wall_seconds", "input_tokens", "output_tokens",
        }
        registry = default_capability_registry()
        for name in registry.names():
            budget = registry.get(name).budget_profile
            self.assertEqual(set(budget), required, name)
            RunBudgets(**dict(budget))

    def test_preflight_rejects_unregistered_and_forbidden_tools(self) -> None:
        registry = default_capability_registry()
        with self.assertRaisesRegex(CapabilityPreflightError, "unknown capability"):
            registry.preflight("missing", ())

        with self.assertRaisesRegex(CapabilityPreflightError, "forbidden"):
            registry.preflight(
                "paper",
                (
                    ToolCapability("read_paper_metadata", True),
                    ToolCapability("search_web", True),
                    ToolCapability("read_managed_blocks", True),
                    ToolCapability("read_run_status", True),
                ),
            )

    def test_preflight_allows_basic_empty_surface_and_web_only_surface(self) -> None:
        registry = default_capability_registry()
        basic_receipt = registry.preflight("basic", ())
        self.assertEqual(basic_receipt.actual, ())
        web_receipt = registry.preflight(
            "web", (ToolCapability("search_web", True),)
        )
        self.assertEqual(web_receipt.required, ("search_web",))

    def test_turn_result_has_one_normalized_projection(self) -> None:
        decision = CapabilityDecision(
            capability="paper",
            retrieval_plan="paper_local",
            execution_mode="sync",
            allowed_tools=("read_managed_blocks",),
            evidence_scope="current_paper",
            reason="选区动作显式指定论文能力",
        )
        result = TurnResult(
            turn_id="turn-1",
            assistant_message="解释完成",
            capability_decision=decision,
            citations=({"source_anchor": "source:block-1"},),
            usage={"input_tokens": 10},
        )
        payload = result.to_dict()
        self.assertEqual(payload["capability_decision"]["capability"], "paper")
        self.assertEqual(payload["citations"][0]["source_anchor"], "source:block-1")

    def test_turn_result_projects_safe_error_without_raw_details(self) -> None:
        decision = CapabilityDecision(
            capability="web",
            retrieval_plan="web_lookup",
            execution_mode="durable",
            allowed_tools=("search_web",),
            evidence_scope="web_sources",
            reason="用户显式动作指定 web 能力",
        )
        result = TurnResult(
            turn_id="turn-error",
            assistant_message="当前回答未完成，请稍后重试。",
            capability_decision=decision,
            status="failed",
            error={"code": "turn_execution_failed", "message": "当前回答未完成，请稍后重试。"},
        )
        payload = result.to_dict()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["code"], "turn_execution_failed")
        self.assertNotIn("traceback", str(payload["error"]).lower())

    def test_profile_rejects_duplicate_tools_and_invalid_mode(self) -> None:
        with self.assertRaises(TurnValidationError):
            CapabilityProfile(
                name="basic",
                allowed_tools=("search_web", "search_web"),
                evidence_scope="none",
                workspace_write_policy="forbidden",
                default_execution_mode="sync",
            )
        with self.assertRaises(TurnValidationError):
            CapabilityProfile(
                name="basic",
                allowed_tools=(),
                evidence_scope="none",
                workspace_write_policy="forbidden",
                default_execution_mode="async",
            )
