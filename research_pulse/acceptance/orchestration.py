"""Bounded adapters that reuse the existing production and dialogue graphs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from research_pulse.production.pipeline import PaperCandidate


@dataclass
class CostBoundary:
    """Hard local budget: one production graph invocation and one scoped question."""

    production_calls: int = 0
    question_calls: int = 0

    def consume_production(self) -> None:
        self.production_calls += 1
        if self.production_calls > 1:
            raise RuntimeError("real acceptance permits only one production invocation")

    def consume_question(self) -> None:
        self.question_calls += 1
        if self.question_calls > 1:
            raise RuntimeError("real acceptance permits only one scoped question")


@dataclass
class SingleCandidateFinder:
    """Forward the one candidate selected from a live query exactly once."""

    candidate: PaperCandidate
    calls: int = 0

    def discover(
        self,
        *,
        topic: str,
        domain: str,
        limit: int,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[PaperCandidate]:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("single-candidate finder cannot be reused")
        if limit != 1:
            raise ValueError("single-candidate acceptance graph requires limit=1")
        if domain != self.candidate.domain:
            raise ValueError("selected candidate domain does not match the acceptance run")
        return [self.candidate]


def invoke_production_once(
    graph: Any,
    *,
    run_id: str,
    topic: str,
    domain: str,
    candidate: PaperCandidate,
    budget: CostBoundary,
) -> dict[str, Any]:
    budget.consume_production()
    result = graph.invoke(
        {"run_id": run_id, "topic": topic, "domain": domain, "limit": 1},
        {"configurable": {"thread_id": f"real-acceptance:{run_id}"}},
    )
    candidate_ids = result.get("candidate_ids")
    receipts = result.get("receipts")
    if candidate_ids != [candidate.source_id] or not isinstance(receipts, list) or len(receipts) != 1:
        raise RuntimeError("production graph escaped the single-candidate acceptance boundary")
    return result


class RejectingSupplementer:
    """A supplementer that proves acceptance never expands beyond one paper."""

    calls = 0

    def supplement(self, *, query: str, domain: str | None, reason: str):
        self.calls += 1
        raise RuntimeError("real acceptance does not permit literature supplementation")
