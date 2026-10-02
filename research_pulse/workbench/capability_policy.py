"""Fixed capability policy for the research-assistant tool surface (M3.9).

The literature/V0 surface keeps using
``require_exact_readonly_capabilities`` (an exact read-only inventory). The
assistant surface is different: its actual tool set is built dynamically by the
runtime (e.g. ``import_supporting_paper`` only exists when an importer is
available), so it needs a fixed *policy* that the preflight can verify the
dynamically-built ``actual`` against.

``CapabilityPolicy`` is a plain declaration (never trimmed per run):
- ``required``  — the tool MUST be present; if missing the preflight fails closed.
- ``conditional`` — a tool whose presence is decided by a runtime criterion
  (e.g. importer availability): criterion true ⇒ MUST be present, false ⇒ MUST
  be absent. This is how ``import_supporting_paper`` is legal when absent.
- ``allowed``  — permitted if present, but NOT mandated (we never trim ``actual``
  down to ``allowed``; a missing allowed tool is not an error).
- ``forbidden`` — the tool MUST NOT be present (execute / task / delete / raw
  shell), enforced independently of the deepagents profile.

This is orthogonal to ``WorkspaceRiskClassifier``: preflight guards the *tool
surface* (what tools exist), risk classification guards *canonical data risk*
(what a mutation means). They are deliberately separate concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from research_pulse.workbench.agent_runtime import ToolCapability
from research_pulse.workbench.capability_preflight import CapabilityPreflightError


@dataclass(frozen=True)
class ConditionalCapability:
    """A tool present only when its criterion resolves true."""

    name: str
    criterion: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.criterion.strip():
            raise CapabilityPreflightError("conditional capability requires a name and a criterion")


@dataclass(frozen=True)
class CapabilityPolicy:
    required: tuple[str, ...] = ()
    conditional: tuple[ConditionalCapability, ...] = ()
    allowed: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        seen: dict[str, str] = {}
        buckets = {
            "required": self.required,
            "conditional": tuple(c.name for c in self.conditional),
            "allowed": self.allowed,
            "forbidden": self.forbidden,
        }
        for category, names in buckets.items():
            for name in names:
                if not name.strip():
                    raise CapabilityPreflightError("capability names must not be blank")
                if name in seen:
                    raise CapabilityPreflightError(
                        f"capability {name} declared in both {seen[name]} and {category}"
                    )
                seen[name] = category


@dataclass(frozen=True)
class CapabilityPolicyReceipt:
    required: tuple[str, ...]
    allowed: tuple[str, ...]
    actual: tuple[str, ...]
    conditional_present: tuple[str, ...] = ()


def verify_capability_policy(
    policy: CapabilityPolicy,
    actual: Sequence[ToolCapability],
    *,
    criteria: Mapping[str, bool],
) -> CapabilityPolicyReceipt:
    """Fail-closed verification of a dynamically-built actual tool surface.

    ``criteria`` resolves each conditional tool's criterion key to a boolean so
    the verifier can decide must-present (true) vs must-absent (false). Missing
    criteria keys fail closed. Unresolved categories, an unexpected tool, a
    missing required tool, a present forbidden tool, or a conditional tool on
    the wrong side of its criterion all raise ``CapabilityPreflightError``.
    """
    actual_names = tuple(item.name for item in actual)
    if len(set(actual_names)) != len(actual_names):
        raise CapabilityPreflightError("duplicate capability declaration")
    actual_set = set(actual_names)

    declared = (
        set(policy.required)
        | {c.name for c in policy.conditional}
        | set(policy.allowed)
        | set(policy.forbidden)
    )
    unexpected = sorted(actual_set - declared)
    if unexpected:
        raise CapabilityPreflightError(
            "unexpected Harness capabilities: " + ", ".join(unexpected)
        )

    missing_required = sorted(set(policy.required) - actual_set)
    if missing_required:
        raise CapabilityPreflightError(
            "required Harness capabilities are missing: " + ", ".join(missing_required)
        )

    forbidden_present = sorted(set(policy.forbidden) & actual_set)
    if forbidden_present:
        raise CapabilityPreflightError(
            "forbidden Harness capabilities are present: " + ", ".join(forbidden_present)
        )

    conditional_present: list[str] = []
    for conditional in policy.conditional:
        if conditional.criterion not in criteria:
            raise CapabilityPreflightError(
                f"unresolved conditional criterion: {conditional.criterion}"
            )
        satisfied = criteria[conditional.criterion]
        if satisfied and conditional.name not in actual_set:
            raise CapabilityPreflightError(
                f"conditional capability {conditional.name} must be present"
            )
        if not satisfied and conditional.name in actual_set:
            raise CapabilityPreflightError(
                f"conditional capability {conditional.name} must be absent"
            )
        if satisfied:
            conditional_present.append(conditional.name)

    return CapabilityPolicyReceipt(
        required=policy.required,
        allowed=policy.allowed,
        actual=actual_names,
        conditional_present=tuple(conditional_present),
    )


# ---------------------------------------------------------------------------
# Concrete assistant-surface policy — the single source of truth for the
# research-assistant tool face. The runtime builds ``actual`` from this same
# contract, and preflight verifies it against this declaration.
# ---------------------------------------------------------------------------

# Tools the controlled research assistant must always expose when operable: the
# read side, planning and file inspection. A missing one means the assistant
# cannot run under control.
_ASSISTANT_REQUIRED = (
    "search_sources",
    "read_paper_metadata",
    "read_managed_blocks",
    "read_run_status",
    "search_arxiv",
    "write_todos",
    "ls",
    "read_file",
    "glob",
    "grep",
)
# The controlled canonical-write channel is CONDITIONAL on a workspace being
# bound to the session (V1 requires an anchor paper). When bound they must be
# present; when not, they must be absent — a fresh session with no paper still
# gets the read-only assistant surface rather than a blocked preflight.
_ASSISTANT_WORKSPACE_TOOLS = (
    "read_workspace_state",
    "update_subquestions",
    "add_evidence",
    "update_research_map",
    "update_research_plan",
)
# Available only when an importer (managed-material pipeline) is present, or a
# workspace is bound (the controlled write channel).
_ASSISTANT_CONDITIONAL = tuple(
    ConditionalCapability(name, "workspace_present") for name in _ASSISTANT_WORKSPACE_TOOLS
) + (ConditionalCapability("import_supporting_paper", "importer_present"),)
# Permitted but never required. Raw file writes are guarded by PathScopedBackend
# against canonical paths; their absence is not a preflight failure.
_ASSISTANT_ALLOWED = ("write_file", "edit_file")
# Must never materialize: raw execution, subagent offload and deletion bypass
# the controlled write channel entirely.
_ASSISTANT_FORBIDDEN = ("execute", "task", "delete", "shell")


def assistant_capability_policy() -> CapabilityPolicy:
    return CapabilityPolicy(
        required=_ASSISTANT_REQUIRED,
        conditional=_ASSISTANT_CONDITIONAL,
        allowed=_ASSISTANT_ALLOWED,
        forbidden=_ASSISTANT_FORBIDDEN,
    )


# Local paper Q&A (single-paper ask): the read surface is required, but the
# literature search tools are merely *allowed* (legally present in the actual
# inventory, but bound out of the agent so the model cannot wander off). A
# missing search tool is not an error here — that is exactly the point of a
# paper-local ask.
_PAPER_LOCAL_REQUIRED = tuple(
    name for name in _ASSISTANT_REQUIRED if name not in {"search_sources", "search_arxiv"}
)
_PAPER_LOCAL_ALLOWED = ("search_sources", "search_arxiv", "search_web", "write_file", "edit_file")


def paper_local_capability_policy() -> CapabilityPolicy:
    return CapabilityPolicy(
        required=_PAPER_LOCAL_REQUIRED,
        conditional=_ASSISTANT_CONDITIONAL,
        allowed=_PAPER_LOCAL_ALLOWED,
        forbidden=_ASSISTANT_FORBIDDEN,
    )


def synthesis_capability_policy() -> CapabilityPolicy:
    """Safe read-only surface used after budget exhaustion."""
    return CapabilityPolicy(
        required=("read_paper_metadata", "read_managed_blocks", "read_run_status"),
        allowed=("write_todos", "ls", "read_file", "glob", "grep", "write_file", "edit_file"),
        forbidden=("search_sources", "search_arxiv", "search_web", "execute", "task", "delete", "shell"),
    )


def direct_capability_policy() -> CapabilityPolicy:
    """Low-cost answer surface: local reads only, no retrieval expansion."""
    return CapabilityPolicy(
        required=("read_paper_metadata", "read_managed_blocks", "read_run_status"),
        conditional=(ConditionalCapability("read_workspace_state", "workspace_present"),),
        allowed=("write_todos", "ls", "read_file", "glob", "grep", "write_file", "edit_file"),
        forbidden=(
            "search_sources", "search_arxiv", "search_web", "import_supporting_paper",
            "update_subquestions", "add_evidence", "update_research_map", "update_research_plan",
            "execute", "task", "delete", "shell",
        ),
    )


def paper_evidence_capability_policy() -> CapabilityPolicy:
    """Anchor-paper answer surface without external literature expansion."""
    return CapabilityPolicy(
        required=tuple(
            name for name in _ASSISTANT_REQUIRED
            if name not in {"search_sources", "search_arxiv"}
        ),
        conditional=_ASSISTANT_CONDITIONAL,
        allowed=_ASSISTANT_ALLOWED,
        forbidden=("search_sources", "search_arxiv", "search_web", "execute", "task", "delete", "shell"),
    )


def web_lookup_capability_policy() -> CapabilityPolicy:
    """Web-only answer surface; paper discovery and canonical writes are out."""
    return CapabilityPolicy(
        required=("search_web",),
        allowed=("write_todos", "ls", "read_file", "glob", "grep", "write_file", "edit_file"),
        forbidden=(
            "search_sources", "search_arxiv", "read_paper_metadata", "read_managed_blocks",
            "read_run_status",
            "import_supporting_paper",
            "update_subquestions", "add_evidence", "update_research_map", "update_research_plan",
            "execute", "task", "delete", "shell",
        ),
    )


_scope_policies = {
    "paper_local": paper_local_capability_policy,
    "paper_in_research_context": assistant_capability_policy,
    "research_synthesis": assistant_capability_policy,
    "cross_paper_compare": assistant_capability_policy,
    "ambiguous": assistant_capability_policy,
}


def capability_policy_for_mode(mode: str | None) -> CapabilityPolicy:
    """Pick the capability policy for a resolved mode (None / unknown ⇒ the full
    assistant surface, so nothing regresses without a resolved scope)."""
    if not mode:
        return assistant_capability_policy()
    builder = _scope_policies.get(mode)
    return builder() if builder is not None else assistant_capability_policy()
