"""Resumable background production graph without raw-paper checkpoint state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from research_pulse.production.pipeline import PaperCandidate, ProductionService


class CandidateFinder(Protocol):
    def discover(
        self,
        *,
        topic: str,
        domain: str,
        limit: int,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[PaperCandidate]: ...


class ProductionState(TypedDict, total=False):
    run_id: str
    topic: str
    domain: str
    limit: int
    window_start: datetime | None
    window_end: datetime | None
    candidate_ids: list[str]
    receipts: list[dict[str, Any]]
    discovery_succeeded: bool


@dataclass(frozen=True)
class ProductionGraphDependencies:
    candidate_finder: CandidateFinder
    production_service: ProductionService


def build_production_graph(deps: ProductionGraphDependencies, *, checkpointer: Any = None):
    """Compile the scheduled/initialization path.

    The graph state has identifiers and small receipts only.  Parsing output,
    source fragments, and model prompts remain inside ``ProductionService``.
    """

    def run_batch(state: ProductionState) -> dict[str, Any]:
        candidates = deps.candidate_finder.discover(
            topic=state["topic"],
            domain=state["domain"],
            limit=state.get("limit", 3),
            window_start=state.get("window_start"),
            window_end=state.get("window_end"),
        )
        receipts = [deps.production_service.process(candidate) for candidate in candidates]
        # Only identities and receipts cross the LangGraph checkpoint boundary.
        # Candidate abstracts, PDF text, parser fragments, and model prompts all
        # live only inside ProductionService.process().
        return {
            "candidate_ids": [candidate.source_id for candidate in candidates],
            "receipts": [asdict(receipt) for receipt in receipts],
            "discovery_succeeded": True,
        }

    workflow = StateGraph(ProductionState)
    workflow.add_node("run_batch", run_batch)
    workflow.add_edge(START, "run_batch")
    workflow.add_edge("run_batch", END)
    return workflow.compile(checkpointer=checkpointer)
