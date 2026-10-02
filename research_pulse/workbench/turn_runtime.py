"""Unified, framework-free turn contracts and capability profiles.

The first migration slice deliberately contains no model or persistence code.
It gives the existing ChatService and ExplorationService a stable boundary so
they can be adapted without changing their lifecycle semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import os
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from research_pulse.workbench.agent_runtime import ToolCapability
from research_pulse.workbench.capability_policy import (
    CapabilityPolicy,
    assistant_capability_policy,
    verify_capability_policy,
    web_lookup_capability_policy,
)
from research_pulse.workbench.capability_preflight import CapabilityPreflightError
from research_pulse.workbench.scope_resolver import InteractionContext
from research_pulse.workbench.turn_events import TurnEvent, UnsequencedTurnEvent


class TurnValidationError(ValueError):
    """Raised when a turn cannot cross the runtime boundary safely."""


_CAPABILITIES = frozenset({"basic", "paper", "web", "research"})
_EXECUTION_MODES = frozenset({"sync", "background", "durable"})
_ROUTE_STATUSES = frozenset({"selected", "ambiguous", "unavailable"})

# Budget contract for the multi-step assistant research capability.  Keeping
# this at the routing seam ensures the real unified TurnRuntime path and the
# legacy exploration entry point use the same limits.
ASSISTANT_RESEARCH_BUDGET = {
    "model_rounds": 24,
    "tool_calls": 64,
    "block_reads": 96,
    "wall_seconds": 600,
    "input_tokens": 200000,
    "output_tokens": 40000,
}


def experimental_research_mode_enabled() -> bool:
    """Whether application-level research budgets are disabled for testing.

    This is an explicit opt-in switch for local validation.  Provider limits,
    wall-clock timeout and the runtime's safety guards remain active; the
    switch only removes the Harness token/step budget that otherwise causes a
    long exploratory synthesis to be cut short.
    """
    return os.getenv("RESEARCH_PULSE_EXPERIMENTAL_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def assistant_research_budget(*, experimental_mode: bool | None = None) -> dict[str, int | bool]:
    """Return the assistant budget contract, optionally in test mode."""
    enabled = experimental_research_mode_enabled() if experimental_mode is None else experimental_mode
    budget: dict[str, int | bool] = dict(ASSISTANT_RESEARCH_BUDGET)
    if enabled:
        budget["experimental_unbounded"] = True
    return budget


def research_provider_output_limit(*, experimental_mode: bool | None = None) -> int:
    """Provider output cap used by the explicit experimental mode.

    The normal product path keeps the existing compact 1,000-token response.
    Test mode defaults to a larger provider cap so the result demonstrates the
    available upper bound instead of the application truncation floor; callers
    can still set an exact value through the environment.
    """
    enabled = experimental_research_mode_enabled() if experimental_mode is None else experimental_mode
    default = "8000" if enabled else "1000"
    raw = os.getenv("RESEARCH_PULSE_RESEARCH_MAX_OUTPUT_TOKENS", default).strip()
    try:
        value = int(raw)
    except ValueError:
        value = int(default)
    return max(1, value)


def _bounded_mapping(value: Mapping[str, object] | None) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class TurnRequest:
    """Normalized input accepted by every conversational entry point."""

    session_id: str
    message: str
    interaction_context: InteractionContext = field(default_factory=InteractionContext)
    explicit_action: str | None = None
    client_request_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise TurnValidationError("session_id is required")
        if not self.message.strip():
            raise TurnValidationError("message is required")
        if self.explicit_action is not None and not self.explicit_action.strip():
            raise TurnValidationError("explicit_action must not be blank")
        if self.client_request_id is not None and not self.client_request_id.strip():
            raise TurnValidationError("client_request_id must not be blank")
        object.__setattr__(self, "metadata", _bounded_mapping(self.metadata))

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "TurnRequest":
        if not isinstance(data, Mapping):
            raise TurnValidationError("turn request must be an object")
        raw_context = data.get("interaction_context")
        try:
            context = InteractionContext.from_dict(
                raw_context if isinstance(raw_context, dict) else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TurnValidationError("interaction_context is invalid") from exc
        return cls(
            session_id=str(data.get("session_id", "")),
            message=str(data.get("message", "")),
            interaction_context=context,
            explicit_action=(
                str(data["explicit_action"])
                if data.get("explicit_action") is not None
                else None
            ),
            client_request_id=(
                str(data["client_request_id"])
                if data.get("client_request_id") is not None
                else None
            ),
            metadata=(
                data.get("metadata", {})
                if isinstance(data.get("metadata", {}), Mapping)
                else {}
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "message": self.message,
            "interaction_context": self.interaction_context.to_dict(),
            "explicit_action": self.explicit_action,
            "client_request_id": self.client_request_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CapabilityProfile:
    """Immutable tool/evidence contract for one user-visible capability."""

    name: str
    allowed_tools: tuple[str, ...]
    evidence_scope: str
    workspace_write_policy: str
    default_execution_mode: str
    budget_profile: Mapping[str, int | bool] = field(default_factory=dict)
    failure_policy: str = "bounded"
    policy: CapabilityPolicy = field(default_factory=CapabilityPolicy)

    def __post_init__(self) -> None:
        if self.name not in _CAPABILITIES:
            raise TurnValidationError(f"unknown capability: {self.name}")
        if self.default_execution_mode not in _EXECUTION_MODES:
            raise TurnValidationError("execution mode is invalid")
        if not self.evidence_scope.strip():
            raise TurnValidationError("evidence scope is required")
        if not self.workspace_write_policy.strip():
            raise TurnValidationError("workspace write policy is required")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise TurnValidationError("profile tools must be unique")
        if any(not name.strip() for name in self.allowed_tools):
            raise TurnValidationError("profile tool names must not be blank")
        if any(value < 0 for value in self.budget_profile.values()):
            raise TurnValidationError("profile budgets must not be negative")
        object.__setattr__(self, "budget_profile", MappingProxyType(dict(self.budget_profile)))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "allowed_tools": list(self.allowed_tools),
            "evidence_scope": self.evidence_scope,
            "workspace_write_policy": self.workspace_write_policy,
            "default_execution_mode": self.default_execution_mode,
            "budget_profile": dict(self.budget_profile),
            "failure_policy": self.failure_policy,
        }


@dataclass(frozen=True)
class CapabilityDecision:
    """Persistable decision made before an executor is selected."""

    capability: str
    retrieval_plan: str
    execution_mode: str
    allowed_tools: tuple[str, ...]
    evidence_scope: str
    reason: str
    rule_version: str = "v1"
    confidence: float = 1.0
    status: str = "selected"

    def __post_init__(self) -> None:
        if self.capability not in _CAPABILITIES:
            raise TurnValidationError(f"unknown capability: {self.capability}")
        if self.execution_mode not in _EXECUTION_MODES:
            raise TurnValidationError("execution mode is invalid")
        if self.status not in _ROUTE_STATUSES:
            raise TurnValidationError("route status is invalid")
        if not self.reason.strip() or not self.rule_version.strip():
            raise TurnValidationError("route reason and rule version are required")
        if not 0.0 <= self.confidence <= 1.0:
            raise TurnValidationError("route confidence must be between 0 and 1")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise TurnValidationError("decision tools must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "retrieval_plan": self.retrieval_plan,
            "execution_mode": self.execution_mode,
            "allowed_tools": list(self.allowed_tools),
            "evidence_scope": self.evidence_scope,
            "reason": self.reason,
            "rule_version": self.rule_version,
            "confidence": self.confidence,
            "status": self.status,
        }


@dataclass(frozen=True)
class TurnResult:
    """Common result projection for synchronous and durable execution."""

    turn_id: str
    assistant_message: str | None
    capability_decision: CapabilityDecision
    citations: tuple[Mapping[str, object], ...] = ()
    event_summary: tuple[Mapping[str, object], ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)
    status: str = "completed"
    durable_handle: str | None = None
    error: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.turn_id.strip():
            raise TurnValidationError("turn_id is required")
        if self.assistant_message is not None and not self.assistant_message.strip():
            raise TurnValidationError("assistant_message must not be blank")
        if self.status not in {"queued", "running", "completed", "failed", "cancelled"}:
            raise TurnValidationError("turn status is invalid")
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        if self.error is not None:
            object.__setattr__(self, "error", MappingProxyType(dict(self.error)))
        object.__setattr__(
            self,
            "citations",
            tuple(MappingProxyType(dict(item)) for item in self.citations),
        )
        object.__setattr__(
            self,
            "event_summary",
            tuple(MappingProxyType(dict(item)) for item in self.event_summary),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "assistant_message": self.assistant_message,
            "capability_decision": self.capability_decision.to_dict(),
            "citations": [dict(item) for item in self.citations],
            "event_summary": [dict(item) for item in self.event_summary],
            "usage": dict(self.usage),
            "status": self.status,
            "durable_handle": self.durable_handle,
            "error": dict(self.error) if self.error is not None else None,
        }


@dataclass(frozen=True)
class TurnAuditRecord:
    """Durable route decision associated with one conversational turn."""

    turn_id: str
    session_id: str
    decision: CapabilityDecision
    context_snapshot: Mapping[str, object] = field(default_factory=dict)
    client_request_id: str | None = None
    status: str = "queued"
    run_id: str | None = None
    attempt_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.turn_id.strip() or not self.session_id.strip():
            raise TurnValidationError("turn and session identities are required")
        if self.client_request_id is not None and not self.client_request_id.strip():
            raise TurnValidationError("client_request_id must not be blank")
        if self.status not in {"queued", "running", "completed", "failed", "cancelled"}:
            raise TurnValidationError("turn audit status is invalid")
        object.__setattr__(self, "context_snapshot", _bounded_mapping(self.context_snapshot))

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "decision": self.decision.to_dict(),
            "context_snapshot": dict(self.context_snapshot),
            "client_request_id": self.client_request_id,
            "status": self.status,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "created_at": self.created_at.isoformat(),
        }


class TurnAuditRepository(Protocol):
    def create_or_get_turn_audit(self, record: TurnAuditRecord) -> TurnAuditRecord: ...
    def get_turn_audit(self, turn_id: str) -> TurnAuditRecord | None: ...
    def save_turn_audit(self, record: TurnAuditRecord) -> None: ...
    def append_turn_event(self, turn_id: str, event: UnsequencedTurnEvent) -> TurnEvent: ...


class CapabilityProfileRegistry:
    """Registry and fail-closed preflight for user-visible capabilities."""

    def __init__(self, profiles: Sequence[CapabilityProfile] = ()) -> None:
        self._profiles: dict[str, CapabilityProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: CapabilityProfile) -> None:
        if profile.name in self._profiles:
            raise TurnValidationError(f"duplicate capability profile: {profile.name}")
        self._profiles[profile.name] = profile

    def get(self, name: str) -> CapabilityProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise CapabilityPreflightError(f"unknown capability profile: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def preflight(
        self,
        name: str,
        actual: Sequence[ToolCapability],
        *,
        criteria: Mapping[str, bool] | None = None,
    ):
        profile = self.get(name)
        return verify_capability_policy(
            profile.policy,
            actual,
            criteria=dict(criteria or {}),
        )


def default_capability_profiles(
    *, experimental_mode: bool | None = None,
) -> tuple[CapabilityProfile, ...]:
    """Return the four V1 capability profiles used by the routing seam."""

    # basic intentionally has no tools. It is a safe conversational fallback.
    basic = CapabilityProfile(
        name="basic",
        allowed_tools=(),
        evidence_scope="none",
        workspace_write_policy="forbidden",
        default_execution_mode="sync",
        budget_profile={
            "model_rounds": 1,
            "tool_calls": 1,
            "block_reads": 0,
            "wall_seconds": 60,
            "input_tokens": 4000,
            "output_tokens": 1000,
        },
        failure_policy="return_safe_error",
        policy=CapabilityPolicy(),
    )
    paper = CapabilityProfile(
        name="paper",
        allowed_tools=("read_paper_metadata", "read_managed_blocks", "read_run_status"),
        evidence_scope="current_paper",
        workspace_write_policy="forbidden",
        # Paper answers persist a generating message first, then execute the
        # provider in a bounded background worker so PDF selection never waits
        # on model latency before showing feedback.
        default_execution_mode="background",
        budget_profile={
            "model_rounds": 1,
            "tool_calls": 3,
            "block_reads": 3,
            "wall_seconds": 60,
            "input_tokens": 10000,
            "output_tokens": 2000,
        },
        failure_policy="bounded_read_retry",
        policy=CapabilityPolicy(
            allowed=("read_paper_metadata", "read_managed_blocks", "read_run_status"),
            forbidden=(
                "search_sources", "search_arxiv", "search_web", "execute", "task",
                "delete", "shell", "add_evidence", "update_subquestions",
                "update_research_map", "update_research_plan",
            ),
        ),
    )
    web = CapabilityProfile(
        name="web",
        allowed_tools=("search_web",),
        evidence_scope="web_sources",
        workspace_write_policy="forbidden",
        # Web search already has a bounded durable worker path in the current
        # application; keep it durable until a dedicated lightweight web
        # adapter is introduced.
        default_execution_mode="durable",
        budget_profile={
            "model_rounds": 4,
            "tool_calls": 8,
            "block_reads": 0,
            "wall_seconds": 180,
            "input_tokens": 30000,
            "output_tokens": 6000,
        },
        failure_policy="bounded_network_retry",
        policy=web_lookup_capability_policy(),
    )
    research = CapabilityProfile(
        name="research",
        allowed_tools=tuple(
            name
            for name in (
                "search_sources", "read_paper_metadata", "read_managed_blocks", "read_run_status",
                "search_arxiv", "write_todos", "ls", "read_file", "glob", "grep",
                "read_workspace_state", "update_subquestions", "add_evidence",
                "update_research_map", "update_research_plan", "import_supporting_paper", "write_file", "edit_file",
            )
        ),
        evidence_scope="research_workspace",
        workspace_write_policy="controlled_workspace_only",
        default_execution_mode="durable",
        budget_profile=assistant_research_budget(experimental_mode=experimental_mode),
        failure_policy="attempt_scoped_recovery",
        policy=assistant_capability_policy(),
    )
    return (basic, paper, web, research)


def default_capability_registry(
    *, experimental_mode: bool | None = None,
) -> CapabilityProfileRegistry:
    return CapabilityProfileRegistry(
        default_capability_profiles(experimental_mode=experimental_mode)
    )
