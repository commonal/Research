from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest import TestCase

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from research_pulse.knowledge.models import KnowledgeAsset
from research_pulse.rag.contracts import EvidenceHit, SearchRequest
from research_pulse.workflows.interactive import (
    CitationOnlyAnswerGenerator,
    InteractiveGraphDependencies,
    SupplementationResult,
    build_interactive_graph,
)


ROOT = Path(__file__).resolve().parents[1]


class _MemoryRAG:
    def __init__(self, hits: list[EvidenceHit] | None = None) -> None:
        self.hits = hits or []

    def publish(self, asset: KnowledgeAsset) -> None:
        return None

    def search(self, request: SearchRequest) -> list[EvidenceHit]:
        return [
            hit
            for hit in self.hits
            if (request.domain is None or request.domain == "llm_agent_memory")
            and (not request.knowledge_ids or hit.knowledge_id in request.knowledge_ids)
        ]


class _Supplementer:
    def __init__(self, rag: _MemoryRAG, replacement_hits: list[EvidenceHit]) -> None:
        self.rag = rag
        self.replacement_hits = replacement_hits
        self.calls = 0

    def supplement(self, *, query: str, domain: str | None, reason: str) -> SupplementationResult:
        self.calls += 1
        self.rag.hits = self.replacement_hits
        return SupplementationResult(
            knowledge_gap_id="gap:test:1",
            status="completed",
            published_knowledge_ids=tuple(hit.knowledge_id for hit in self.replacement_hits),
        )


def _hit(*, suffix: str) -> EvidenceHit:
    return EvidenceHit(
        chunk_id=f"chunk:{suffix}",
        knowledge_id=f"kp:test:{suffix}",
        knowledge_version="2026-08-22T00:00:00Z",
        title=f"Test {suffix}",
        text=f"Evidence {suffix} for long-term agent memory.",
        source_url=f"https://example.com/{suffix}",
        anchor_id=f"anchor:{suffix}",
        dense_score=None,
        keyword_score=0.1,
        fused_score=0.1,
    )


class InteractiveGraphTests(TestCase):
    def test_sufficient_evidence_answers_without_interrupt(self) -> None:
        rag = _MemoryRAG([_hit(suffix="one"), _hit(suffix="two")])
        supplementer = _Supplementer(rag, [])
        graph = build_interactive_graph(
            InteractiveGraphDependencies(rag, supplementer, CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )

        result = graph.invoke(
            {"query": "What does the knowledge base say?", "domain": "llm_agent_memory"},
            {"configurable": {"thread_id": "sufficient"}},
        )

        self.assertIn("可定位证据", result["answer"])
        self.assertEqual(supplementer.calls, 0)

    def test_user_approval_resumes_and_retrieves_new_knowledge(self) -> None:
        rag = _MemoryRAG()
        supplementer = _Supplementer(rag, [_hit(suffix="one"), _hit(suffix="two")])
        graph = build_interactive_graph(
            InteractiveGraphDependencies(rag, supplementer, CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": "approved-gap"}}

        paused = graph.invoke({"query": "Need missing knowledge", "domain": "llm_agent_memory"}, config)
        resumed = graph.invoke(Command(resume={"approved": True}), config)

        self.assertIn("__interrupt__", paused)
        self.assertEqual(supplementer.calls, 1)
        self.assertEqual(resumed["knowledge_gap_id"], "gap:test:1")
        self.assertIn("Evidence one", resumed["answer"])

    def test_user_decline_abstains_without_supplementing(self) -> None:
        rag = _MemoryRAG()
        supplementer = _Supplementer(rag, [_hit(suffix="one"), _hit(suffix="two")])
        graph = build_interactive_graph(
            InteractiveGraphDependencies(rag, supplementer, CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": "declined-gap"}}

        graph.invoke({"query": "Need missing knowledge", "domain": "llm_agent_memory"}, config)
        result = graph.invoke(Command(resume="no"), config)

        self.assertEqual(supplementer.calls, 0)
        self.assertIn("证据不足", result["answer"])

    def test_approval_without_new_knowledge_abstains_instead_of_looping(self) -> None:
        rag = _MemoryRAG()
        supplementer = _Supplementer(rag, [])
        graph = build_interactive_graph(
            InteractiveGraphDependencies(rag, supplementer, CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": "empty-supplement"}}

        graph.invoke({"query": "Need missing knowledge", "domain": "llm_agent_memory"}, config)
        result = graph.invoke(Command(resume={"approved": True}), config)

        self.assertEqual(supplementer.calls, 1)
        self.assertIn("证据不足", result["answer"])
