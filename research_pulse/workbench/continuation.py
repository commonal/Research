"""Bounded references used to continue a run without claiming thread restoration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType


@dataclass(frozen=True)
class StableEvidenceReference:
    stable_id: str
    read: bool
    resolvable: bool
    source_id: str | None = None

    def __post_init__(self) -> None:
        if not self.stable_id.strip():
            raise ValueError("stable evidence id must not be blank")


@dataclass(frozen=True)
class OperationReference:
    operation_id: str
    settled: bool
    receipt: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("operation id must not be blank")
        object.__setattr__(self, "receipt", MappingProxyType(dict(self.receipt)))


@dataclass(frozen=True)
class PersistedToolReference:
    tool_call_id: str
    tool_name: str
    status: str
    bounded_result_reference: str | None = None
    safe_message: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_call_id.strip() or not self.tool_name.strip() or not self.status.strip():
            raise ValueError("persisted tool reference identity is required")
        if self.safe_message is not None and len(self.safe_message) > 500:
            raise ValueError("persisted tool summary is too large")


@dataclass(frozen=True)
class SearchCandidateReference:
    """Bounded metadata from a prior search, never a citation claim."""

    source_id: str
    title: str
    abstract_preview: str = ""
    url: str = ""
    relevance: float = 0.0

    def __post_init__(self) -> None:
        if not self.source_id.strip() and not self.title.strip():
            raise ValueError("search candidate requires an identity")
        if len(self.title) > 300 or len(self.abstract_preview) > 500:
            raise ValueError("search candidate metadata is too large")

    def to_recovery_fact(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "abstract_preview": self.abstract_preview,
            "url": self.url,
            "relevance": self.relevance,
        }


@dataclass(frozen=True)
class ContinuationBundle:
    prior_attempt_id: str
    persisted_event_ids: tuple[str, ...]
    evidence: tuple[StableEvidenceReference, ...]
    workspace_revision: str | None
    operations: tuple[OperationReference, ...]
    tool_results: tuple[PersistedToolReference, ...] = ()
    search_candidates: tuple[SearchCandidateReference, ...] = ()
    recovery_strategy: str = "continue_from_persisted_results"

    def __post_init__(self) -> None:
        if not self.prior_attempt_id.strip():
            raise ValueError("prior attempt id must not be blank")
        if any(not item.strip() for item in self.persisted_event_ids):
            raise ValueError("persisted event ids must not be blank")
        if len(set(self.persisted_event_ids)) != len(self.persisted_event_ids):
            raise ValueError("persisted event ids must be unique")

    @property
    def inherited_evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.stable_id for item in self.evidence)

    @property
    def citation_allowlist(self) -> tuple[str, ...]:
        return tuple(
            item.stable_id for item in self.evidence if item.read and item.resolvable
        )

    @property
    def checkpoint_resume_allowed(self) -> bool:
        return bool(
            self.persisted_event_ids
            and self.workspace_revision
            and all(item.settled for item in self.operations)
            and all(item.resolvable for item in self.evidence)
        )

    def with_workspace_revision(self, revision: str | None) -> "ContinuationBundle":
        return replace(self, workspace_revision=revision)

    def with_operations(
        self, operations: tuple[OperationReference, ...]
    ) -> "ContinuationBundle":
        return replace(self, operations=operations)

    def with_evidence(
        self, evidence: tuple[StableEvidenceReference, ...]
    ) -> "ContinuationBundle":
        return replace(self, evidence=evidence)

    def to_recovery_facts(self) -> dict[str, object]:
        """Return a JSON-safe, reference-only payload for the next Attempt."""
        return {
            "prior_attempt_id": self.prior_attempt_id,
            "persisted_event_ids": list(self.persisted_event_ids),
            "evidence": [
                {
                    "stable_id": item.stable_id,
                    "read": item.read,
                    "resolvable": item.resolvable,
                    "source_id": item.source_id,
                }
                for item in self.evidence
            ],
            "citation_allowlist": list(self.citation_allowlist),
            "workspace_revision": self.workspace_revision,
            "operations": [
                {"operation_id": item.operation_id, "settled": item.settled, "receipt": dict(item.receipt)}
                for item in self.operations
            ],
            "tool_results": [
                {
                    "tool_call_id": item.tool_call_id,
                    "tool_name": item.tool_name,
                    "status": item.status,
                    "bounded_result_reference": item.bounded_result_reference,
                    "safe_message": item.safe_message,
                }
                for item in self.tool_results
            ],
            "search_candidates": [
                item.to_recovery_fact() for item in self.search_candidates
            ],
            "recovery_strategy": self.recovery_strategy,
            "resume_mode": "checkpoint" if self.checkpoint_resume_allowed else "continuation",
        }


class ContinuationAssembler:
    """Builds a continuation exclusively from durable, bounded facts."""

    def __init__(
        self, repository, *, evidence_resolver: Callable[[str], bool] = lambda _stable_id: True
    ) -> None:
        self._repository = repository
        self._evidence_resolver = evidence_resolver

    def assemble(self, prior_attempt_id: str, *, prior_status: str | None = None) -> ContinuationBundle:
        events = self._repository.list_attempt_events(prior_attempt_id)
        evidence: dict[str, StableEvidenceReference] = {}
        search_candidates: dict[str, SearchCandidateReference] = {}

        def collect_candidate(item: Mapping[str, object]) -> None:
            source_id = str(item.get("source_id") or item.get("arxiv_id") or "")
            title = str(item.get("title") or "")[:300]
            if not source_id and not title:
                return
            key = source_id or title
            try:
                relevance = float(item.get("relevance") or 0.0)
            except (TypeError, ValueError):
                relevance = 0.0
            search_candidates[key] = SearchCandidateReference(
                source_id=source_id,
                title=title,
                abstract_preview=str(item.get("abstract_preview") or "")[:500],
                url=str(item.get("url") or "")[:500],
                relevance=relevance,
            )

        for event in events:
            stable = event.payload.get("stable_ids", {})
            if not isinstance(stable, Mapping):
                continue
            if event.event_type == "source_discovered" and stable.get("source_id"):
                stable_id = str(stable["source_id"])
                evidence.setdefault(stable_id, StableEvidenceReference(
                    stable_id, read=False, resolvable=self._evidence_resolver(stable_id)
                ))
            if event.event_type == "context_read" and stable.get("block_id"):
                stable_id = str(stable["block_id"])
                evidence[stable_id] = StableEvidenceReference(
                    stable_id,
                    read=True,
                    resolvable=self._evidence_resolver(stable_id),
                    source_id=str(stable["source_id"]) if stable.get("source_id") else None,
                )
            if event.event_type == "tool_completed" and stable.get("tool_name") in {"search_arxiv", "search_sources"}:
                extras = event.payload.get("extras", ())
                if isinstance(extras, (list, tuple)):
                    for item in extras:
                        if not isinstance(item, Mapping):
                            continue
                        collect_candidate(item)
        prior_attempt = self._repository.get_coordinated_attempt(prior_attempt_id)
        if prior_attempt is not None:
            inherited = prior_attempt.input_snapshot.get("continuation", {})
            if isinstance(inherited, Mapping):
                candidates = inherited.get("search_candidates", ())
                if isinstance(candidates, (list, tuple)):
                    for item in candidates:
                        if isinstance(item, Mapping):
                            collect_candidate(item)
        outcomes = self._repository.list_tool_outcomes(prior_attempt_id)
        tool_results = tuple(PersistedToolReference(
            item.tool_call_id, item.tool_name, item.status.value,
            item.bounded_result_reference, item.safe_message,
        ) for item in outcomes)
        operations = []
        workspace_revision = None
        for item in self._repository.list_coordinated_operations(prior_attempt_id):
            receipt = item["receipt"]
            settled = item["status"] in {"committed", "failed"}
            operations.append(OperationReference(item["operation_id"], settled, receipt))
            if item["status"] == "committed" and "workspace_revision" in receipt:
                workspace_revision = str(receipt["workspace_revision"])
        recovery_strategy = "continue_from_persisted_results"
        if prior_status == "budget_exhausted":
            recovery_strategy = (
                "synthesize_from_persisted_evidence"
                if any(item.read and item.resolvable for item in evidence.values())
                else "continue_from_persisted_results"
            )
        return ContinuationBundle(
            prior_attempt_id=prior_attempt_id,
            persisted_event_ids=tuple(str(item.event_id) for item in events),
            evidence=tuple(evidence.values()),
            workspace_revision=workspace_revision,
            operations=tuple(operations),
            tool_results=tool_results,
            search_candidates=tuple(search_candidates.values()),
            recovery_strategy=recovery_strategy,
        )
