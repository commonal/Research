"""Small interface for a replaceable, testable ResearchRAG implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from research_pulse.knowledge.models import ClaimType, DurableEvidenceAnchor, KnowledgeBundle


@dataclass(frozen=True)
class SearchRequest:
    query: str
    domain: str | None = None
    knowledge_ids: tuple[str, ...] = ()
    claim_types: tuple[ClaimType, ...] = ()
    limit: int = 8
    published_only: bool = True


@dataclass(frozen=True)
class EvidenceHit:
    """The only retrieval result the answer policy is allowed to consume."""

    chunk_id: str
    knowledge_id: str
    knowledge_version: str
    title: str
    text: str
    source_url: str
    anchor_id: str | None
    dense_score: float | None
    keyword_score: float | None
    fused_score: float
    claim_id: str | None = None
    claim_type: ClaimType | None = None
    source_anchors: tuple[DurableEvidenceAnchor, ...] = ()


@dataclass(frozen=True)
class IndexReceipt:
    """Deterministic identity returned after an idempotent index publish."""

    knowledge_id: str
    knowledge_version: str
    chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.knowledge_id.strip() or not self.knowledge_version.strip():
            raise ValueError("An index receipt needs a knowledge ID and version.")
        if not self.chunk_ids or any(not value.strip() for value in self.chunk_ids):
            raise ValueError("An index receipt needs non-empty chunk IDs.")
        if len(self.chunk_ids) != len(set(self.chunk_ids)):
            raise ValueError("Index receipt chunk IDs must be unique.")


class ResearchRAG(Protocol):
    """A deep seam: callers need only publish and retrieve verified assets."""

    def publish(self, bundle: KnowledgeBundle) -> IndexReceipt:
        """Index one complete published knowledge bundle idempotently."""

    def search(self, request: SearchRequest) -> Sequence[EvidenceHit]:
        """Return only active, source-addressable evidence hits."""
