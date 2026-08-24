"""Bridges that let the conversation graph reuse the production graph safely."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from research_pulse.workflows.interactive import SupplementationResult


@dataclass(frozen=True)
class ProductionGraphSupplementer:
    """Turn one approved knowledge gap into a small, synchronous production run."""

    production_graph: Any
    limit: int = 2

    def supplement(self, *, query: str, domain: str | None, reason: str) -> SupplementationResult:
        gap_id = f"gap:{uuid4()}"
        result = self.production_graph.invoke(
            {
                "run_id": gap_id,
                "topic": query,
                "domain": domain or "general",
                "limit": self.limit,
            }
        )
        published_ids = tuple(
            receipt["knowledge_id"]
            for receipt in result.get("receipts", [])
            if receipt.get("status") == "published" and receipt.get("knowledge_id")
        )
        return SupplementationResult(
            knowledge_gap_id=gap_id,
            status="completed" if published_ids else "no_evidence",
            published_knowledge_ids=published_ids,
        )
