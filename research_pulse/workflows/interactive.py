"""Interactive evidence-first RAG graph with an explicit knowledge-gap pause."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, Sequence, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from research_pulse.rag.contracts import EvidenceHit, ResearchRAG, SearchRequest
from research_pulse.rag.sufficiency import assess_sufficiency


class InteractiveState(TypedDict, total=False):
    query: str
    domain: str | None
    knowledge_ids: list[str]
    retrieved_chunks: list[dict[str, Any]]
    knowledge_insufficient: bool
    sufficiency_reason: str
    user_confirm: bool
    knowledge_gap_id: str
    supplementation_status: str
    answer: str


@dataclass(frozen=True)
class SupplementationResult:
    """Result returned by the production-graph adapter after user approval."""

    knowledge_gap_id: str
    status: Literal["completed", "no_evidence"]
    published_knowledge_ids: tuple[str, ...]


class KnowledgeSupplementer(Protocol):
    """Seam through which the interactive graph reuses the production graph."""

    def supplement(self, *, query: str, domain: str | None, reason: str) -> SupplementationResult: ...


class AnswerGenerator(Protocol):
    """An answer generator receives only already-approved evidence hits."""

    def generate(self, *, query: str, evidence: Sequence[EvidenceHit]) -> str: ...


@dataclass(frozen=True)
class InteractiveGraphDependencies:
    rag: ResearchRAG
    supplementer: KnowledgeSupplementer
    answer_generator: AnswerGenerator
    minimum_hits: int = 2


class CitationOnlyAnswerGenerator:
    """Deterministic development answerer that cannot invent unsupported facts."""

    def generate(self, *, query: str, evidence: Sequence[EvidenceHit]) -> str:
        lines = [f"针对“{query}”，当前知识库的可定位证据："]
        for hit in evidence:
            if hit.claim_type == "reading_question":
                continue
            label = "系统推断" if hit.claim_type == "agent_inference" else "论文事实"
            source_anchor_ids = ", ".join(anchor.anchor_id for anchor in hit.source_anchors)
            source_suffix = f" | 论文来源: {source_anchor_ids}" if source_anchor_ids else ""
            lines.append(
                f"- 【{label}】{hit.text} "
                f"[{hit.knowledge_id}@{hit.knowledge_version} | {hit.anchor_id}{source_suffix}]"
            )
        return "\n".join(lines)


def build_interactive_graph(deps: InteractiveGraphDependencies, *, checkpointer: Any = None):
    """Compile one resumable graph for the user-facing knowledge-base dialogue."""

    def retrieve(state: InteractiveState) -> dict[str, Any]:
        request = SearchRequest(
            query=state["query"],
            domain=state.get("domain"),
            knowledge_ids=tuple(state.get("knowledge_ids", [])),
        )
        hits = list(deps.rag.search(request))
        return {"retrieved_chunks": [_hit_to_state(hit) for hit in hits]}

    def assess(state: InteractiveState) -> dict[str, Any]:
        hits = _hits_from_state(state.get("retrieved_chunks", []))
        decision = assess_sufficiency(hits, minimum_hits=deps.minimum_hits)
        return {
            "knowledge_insufficient": not decision.sufficient,
            "sufficiency_reason": decision.reason,
        }

    def request_confirmation(state: InteractiveState) -> dict[str, Any]:
        response = interrupt(
            {
                "kind": "knowledge_gap_confirmation",
                "query": state["query"],
                "domain": state.get("domain"),
                "reason": state["sufficiency_reason"],
                "retrieved_hit_count": len(state.get("retrieved_chunks", [])),
                "message": "知识库缺少相关证据，是否检索并补充文献？",
            }
        )
        return {"user_confirm": _approved(response)}

    def supplement(state: InteractiveState) -> dict[str, Any]:
        result = deps.supplementer.supplement(
            query=state["query"],
            domain=state.get("domain"),
            reason=state["sufficiency_reason"],
        )
        if result.status not in {"completed", "no_evidence"}:
            raise RuntimeError("Knowledge supplementation returned an invalid status.")
        return {
            "knowledge_gap_id": result.knowledge_gap_id,
            "supplementation_status": result.status,
            "knowledge_ids": list(result.published_knowledge_ids),
        }

    def answer(state: InteractiveState) -> dict[str, Any]:
        hits = _hits_from_state(state.get("retrieved_chunks", []))
        return {"answer": deps.answer_generator.generate(query=state["query"], evidence=hits)}

    def abstain(state: InteractiveState) -> dict[str, Any]:
        return {
            "answer": (
                "当前知识库证据不足，未生成可能无来源的回答。"
                f"原因：{state['sufficiency_reason']}"
            )
        }

    def after_assessment(state: InteractiveState) -> str:
        return "request_confirmation" if state["knowledge_insufficient"] else "answer"

    def after_confirmation(state: InteractiveState) -> str:
        return "supplement" if state["user_confirm"] else "abstain"

    def after_supplement(state: InteractiveState) -> str:
        return "retrieve" if state.get("knowledge_ids") else "abstain"

    workflow = StateGraph(InteractiveState)
    workflow.add_node("retrieve", retrieve)
    workflow.add_node("assess", assess)
    workflow.add_node("request_confirmation", request_confirmation)
    workflow.add_node("supplement", supplement)
    workflow.add_node("answer", answer)
    workflow.add_node("abstain", abstain)
    workflow.add_edge(START, "retrieve")
    workflow.add_edge("retrieve", "assess")
    workflow.add_conditional_edges(
        "assess",
        after_assessment,
        {"request_confirmation": "request_confirmation", "answer": "answer"},
    )
    workflow.add_conditional_edges(
        "request_confirmation",
        after_confirmation,
        {"supplement": "supplement", "abstain": "abstain"},
    )
    workflow.add_conditional_edges(
        "supplement",
        after_supplement,
        {"retrieve": "retrieve", "abstain": "abstain"},
    )
    workflow.add_edge("answer", END)
    workflow.add_edge("abstain", END)
    return workflow.compile(checkpointer=checkpointer)


def _hit_to_state(hit: EvidenceHit) -> dict[str, Any]:
    return asdict(hit)


def _hits_from_state(values: Sequence[dict[str, Any]]) -> list[EvidenceHit]:
    from research_pulse.knowledge.models import DurableEvidenceAnchor

    hits = []
    for value in values:
        payload = dict(value)
        payload["source_anchors"] = tuple(
            item if isinstance(item, DurableEvidenceAnchor) else DurableEvidenceAnchor(**item)
            for item in payload.get("source_anchors", ())
        )
        hits.append(EvidenceHit(**payload))
    return hits


def _approved(response: object) -> bool:
    if isinstance(response, bool):
        return response
    if isinstance(response, str):
        return response.strip().casefold() in {"yes", "y", "true", "confirm", "approved"}
    if isinstance(response, dict):
        return bool(response.get("approved"))
    return False
