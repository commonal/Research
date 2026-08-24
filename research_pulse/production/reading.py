"""Problem-driven selective paper reading.

This module is deliberately framework-free.  A caller crosses one seam,
``PaperReader.read``; all question/target scheduling and reading-state
transitions stay inside the module.  ``TransportUnit`` is only a bounded
request container and never carries a paper-level conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence
import base64
import ast
import html as html_module
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from research_pulse.production.pipeline import PaperCandidate


QuestionStatus = Literal["unanswered", "partial", "supported", "conflicted"]
ArgumentStatus = Literal["hypothesis", "supported", "revised", "rejected"]
EvidenceType = Literal["source_fact", "mechanism_relation", "visual_interpretation", "agent_synthesis", "unknown"]
StopReason = Literal["coverage", "evidence_boundary", "budget", "no_progress", "failed"]
Facet = Literal["problem", "method", "experiment", "limitation"]
ReadingStrategy = Literal["full_paper", "target_fallback"]
READING_PROMPT_VERSION = "full-paper-v2"


@dataclass(frozen=True)
class PaperIRBlock:
    block_id: str
    kind: str
    section: str
    text: str
    order: int = 0
    caption: str | None = None
    latex: str | None = None
    table_html: str | None = None
    image_path: str | None = None
    safe_image: bool = False
    parse_status: str = "available"
    facets: tuple[Facet, ...] = ()
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.block_id.strip() or not self.text.strip():
            raise ValueError("PaperIRBlock requires a non-empty ID and text.")
        if self.order < 0:
            raise ValueError("PaperIRBlock order cannot be negative.")
        if self.kind == "figure" and self.image_path and not self.safe_image:
            raise ValueError("A figure image must be explicitly marked safe.")

    @property
    def is_visual(self) -> bool:
        return self.kind in {"figure", "image", "chart"}

    @property
    def is_complete_text(self) -> bool:
        return self.kind in {"paragraph", "text", "heading", "caption", "formula", "table"} and self.parse_status == "available"

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "section": self.section,
            "text": self.text,
            "order": self.order,
            "caption": self.caption,
            "latex": self.latex,
            "table_html": self.table_html,
            "image_path": self.image_path,
            "safe_image": self.safe_image,
            "parse_status": self.parse_status,
            "facets": list(self.facets),
            "references": list(self.references),
        }


@dataclass(frozen=True)
class CanonicalPaperIR:
    source_id: str
    title: str
    blocks: tuple[PaperIRBlock, ...]
    abstract: str = ""
    source_url: str = ""
    parser_version: str = "canonical-paper-ir-v1"

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.title.strip() or not self.blocks:
            raise ValueError("CanonicalPaperIR requires source identity, title, and blocks.")
        ids = [block.block_id for block in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("CanonicalPaperIR block IDs must be unique.")

    @property
    def ordered_blocks(self) -> tuple[PaperIRBlock, ...]:
        return tuple(sorted(self.blocks, key=lambda block: (block.order, block.block_id)))

    @property
    def block_by_id(self) -> Mapping[str, PaperIRBlock]:
        return {block.block_id: block for block in self.blocks}

    def navigation_blocks(self) -> tuple[PaperIRBlock, ...]:
        ordered = self.ordered_blocks
        selected: list[PaperIRBlock] = []
        groups: dict[str, list[PaperIRBlock]] = {}
        for block in ordered:
            groups.setdefault(block.section.casefold(), []).append(block)
        title_group = next((items for name, items in groups.items() if "task-conditioned" in name or "title" in name), [])
        if title_group:
            selected.append(max(title_group, key=lambda item: len(item.text)))
        selected.extend(item for item in ordered if "introduction" in item.section.casefold() and len(item.text) >= 40)
        for marker in ("method", "methodology", "experiment", "result", "limitation", "conclusion"):
            candidates = [item for item in ordered if marker in item.section.casefold() and len(item.text) >= 40]
            if candidates:
                selected.append(max(candidates, key=lambda item: len(item.text)))
        unique = {item.block_id: item for item in selected}
        return tuple(sorted(unique.values(), key=lambda item: item.order))[:20] or ordered[: min(12, len(ordered))]

    @classmethod
    def from_normalized(cls, *, source_id: str, title: str, blocks: Sequence[Any], source_url: str = "") -> "CanonicalPaperIR":
        """Adapt the existing normalized parser output without reparsing it."""

        converted: list[PaperIRBlock] = []
        for order, block in enumerate(blocks):
            kind = "paragraph" if getattr(block, "kind", "text") == "text" else str(getattr(block, "kind", "text"))
            facets = tuple(getattr(block, "supported_facets", ()) or ())
            converted.append(PaperIRBlock(
                block_id=str(block.block_id), kind=kind, section=" / ".join(getattr(block, "section_path", ()) or ()) or "body",
                text=str(block.text), order=order, caption=getattr(block, "caption", None), latex=getattr(block, "latex", None),
                table_html=getattr(block, "table_html", None), image_path=getattr(block, "image_path", None),
                safe_image=bool(getattr(block, "image_path", None)) and getattr(block, "parse_status", "available") == "available",
                parse_status=str(getattr(block, "parse_status", "available")), facets=facets,
            ))
        return cls(source_id=source_id, title=title, blocks=tuple(converted), source_url=source_url)


@dataclass(frozen=True)
class PaperSkeleton:
    paper_type: str
    section_roles: Mapping[str, str]
    candidate_contributions: tuple[str, ...]
    candidate_experiments: tuple[str, ...]
    cross_references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    provisional: bool = True

    def __post_init__(self) -> None:
        if not self.provisional:
            raise ValueError("PaperSkeleton is a provisional global orientation, not a final summary.")


@dataclass(frozen=True)
class ArgumentNode:
    node_id: str
    statement: str
    status: ArgumentStatus = "hypothesis"
    evidence_refs: tuple[str, ...] = ()
    revision_reason: str = ""
    supersedes: str | None = None


@dataclass(frozen=True)
class ArgumentMap:
    version: int
    nodes: tuple[ArgumentNode, ...]
    previous_version: int | None = None
    revision_record_id: str | None = None
    revision_reason: str = ""

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("ArgumentMap version cannot be negative.")
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("ArgumentMap node IDs must be unique.")
        if self.version == 0 and any(node.status != "hypothesis" for node in self.nodes):
            raise ValueError("ArgumentMap v0 nodes must remain hypotheses.")
        if self.version > 0 and self.previous_version is None:
            raise ValueError("A revised ArgumentMap must retain its previous version.")


@dataclass(frozen=True)
class ReadingQuestion:
    question_id: str
    text: str
    priority: Literal["high", "medium", "low"]
    status: QuestionStatus = "unanswered"
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.question_id.strip() or not self.text.strip():
            raise ValueError("ReadingQuestion requires an ID and text.")


@dataclass(frozen=True)
class ReadingTarget:
    target_id: str
    question_ids: tuple[str, ...]
    argument_node_ids: tuple[str, ...]
    task: str
    success_criteria: str
    failure_criteria: str
    status: Literal["pending", "completed", "blocked"] = "pending"

    def __post_init__(self) -> None:
        if not self.question_ids or not self.task.strip() or not self.success_criteria.strip() or not self.failure_criteria.strip():
            raise ValueError("ReadingTarget requires linked questions and explicit success/failure criteria.")


@dataclass(frozen=True)
class EvidenceBundle:
    target_id: str
    version: int
    source_block_ids: tuple[str, ...]
    selection_reasons: Mapping[str, str]
    expansion_reason: str | None = None
    added_block_ids: tuple[str, ...] = ()
    previous_version: int | None = None

    def __post_init__(self) -> None:
        if self.version < 1 or not self.source_block_ids:
            raise ValueError("EvidenceBundle needs a positive version and source blocks.")
        if len(self.source_block_ids) != len(set(self.source_block_ids)):
            raise ValueError("EvidenceBundle source block IDs must be unique.")
        if set(self.source_block_ids) != set(self.selection_reasons):
            raise ValueError("Every EvidenceBundle block needs a selection reason.")
        if self.version > 1 and (not self.expansion_reason or self.previous_version is None):
            raise ValueError("An expanded EvidenceBundle needs a reason and previous version.")


@dataclass(frozen=True)
class SourceFact:
    fact_id: str
    statement: str
    facet: Facet
    source_block_ids: tuple[str, ...]
    evidence_type: Literal["source_fact"] = "source_fact"


@dataclass(frozen=True)
class MechanismRelation:
    relation_id: str
    relation: str
    source_block_ids: tuple[str, ...]
    evidence_type: Literal["mechanism_relation"] = "mechanism_relation"


@dataclass(frozen=True)
class VisualInterpretation:
    visual_block_id: str
    interpretation: str
    target_id: str
    evidence_type: Literal["visual_interpretation"] = "visual_interpretation"


@dataclass(frozen=True)
class AgentSynthesis:
    synthesis_id: str
    statement: str
    source_block_ids: tuple[str, ...]
    evidence_type: Literal["agent_synthesis"] = "agent_synthesis"


@dataclass(frozen=True)
class Unknown:
    unknown_id: str
    statement: str
    priority: Literal["high", "medium", "low"]
    evidence_type: Literal["unknown"] = "unknown"
    anchor_terms: tuple[str, ...] = ()
    anchor_terms_declared: bool = False


@dataclass(frozen=True)
class EvidenceGap:
    gap_id: str
    statement: str
    requested_facets: tuple[Facet, ...] = ()


@dataclass(frozen=True)
class ReadingRecord:
    target_id: str
    question_ids: tuple[str, ...]
    bundle_version: int
    version: int
    source_block_ids: tuple[str, ...]
    source_facts: tuple[SourceFact, ...] = ()
    mechanism_relations: tuple[MechanismRelation, ...] = ()
    visual_interpretations: tuple[VisualInterpretation, ...] = ()
    agent_syntheses: tuple[AgentSynthesis, ...] = ()
    unknowns: tuple[Unknown, ...] = ()
    evidence_gaps: tuple[EvidenceGap, ...] = ()
    conflicts: tuple[str, ...] = ()
    previous_version: int | None = None

    def __post_init__(self) -> None:
        if self.version < 1 or self.bundle_version < 1:
            raise ValueError("ReadingRecord versions must be positive.")
        if self.version > 1 and self.previous_version is None:
            raise ValueError("An updated ReadingRecord must retain its previous version.")
        allowed = set(self.source_block_ids)
        if any(not set(item.source_block_ids).issubset(allowed) for item in (*self.source_facts, *self.mechanism_relations, *self.agent_syntheses)):
            raise ValueError("ReadingRecord evidence links must point into its bundle.")

    @property
    def has_high_priority_unknown(self) -> bool:
        return any(item.priority == "high" for item in self.unknowns)


@dataclass(frozen=True)
class TransportUnit:
    unit_id: str
    block_ids: tuple[str, ...]
    content: str
    start_order: int
    end_order: int

    def __post_init__(self) -> None:
        if not self.block_ids or not self.content.strip():
            raise ValueError("TransportUnit must contain bounded material.")


@dataclass(frozen=True)
class ReadingIntent:
    language: str = "zh-CN"
    depth: Literal["skim", "deep"] = "deep"
    max_targets: int = 8
    max_expansions_per_target: int = 2
    max_text_calls: int = 32
    max_vision_calls: int = 4
    max_source_chars_per_request: int = 18_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_targets <= 8 or self.max_expansions_per_target < 0:
            raise ValueError("Reading budgets exceed the bounded reader contract.")


@dataclass(frozen=True)
class ReadingDraft:
    markdown: str
    unresolved_boundaries: tuple[str, ...] = ()
    source_fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotePlanningInput:
    paper_title: str
    final_argument_map: ArgumentMap
    compressed_records: tuple[Mapping[str, Any], ...]
    must_preserve_facts: "MustPreserveFacts"
    unresolved_boundaries: tuple[str, ...]
    evidence_references: tuple[str, ...]


@dataclass(frozen=True)
class MustPreserveFact:
    """A typed, writer-facing fact whose wording and anchors must survive."""

    fact_id: str
    statement: str
    facet: Facet
    source_block_ids: tuple[str, ...]
    numeric_tokens: tuple[str, ...] = ()
    named_entities: tuple[str, ...] = ()
    mechanism_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class MustPreserveFacts:
    facts: tuple[MustPreserveFact, ...] = ()
    numeric_groups: tuple[tuple[str, ...], ...] = ()
    named_entities: tuple[str, ...] = ()
    mechanism_terms: tuple[str, ...] = ()
    source_block_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisualDecision:
    action: Literal["inspect", "skip", "inline", "reference", "omit"]
    reason: str
    block_id: str
    target_id: str
    requires_pixels: bool = False


@dataclass(frozen=True)
class SectionExplanationContract:
    """One ordered section's reader-facing explanation obligations."""

    section_id: str
    heading: str
    reader_question: str
    prerequisite_bridges: tuple[str, ...] = ()
    reasoning_steps: tuple[str, ...] = ()
    experiment_slots: tuple[str, ...] = ()
    asset_jobs: tuple[Mapping[str, Any], ...] = ()
    transition_in: str = ""
    transition_out: str = ""
    stop_conditions: tuple[str, ...] = ()
    evidence_handles: tuple[str, ...] = ()
    coverage_obligation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.section_id.strip() or not self.heading.strip() or not self.reader_question.strip():
            raise ValueError("SectionExplanationContract requires section identity and reader question.")
        if not self.reasoning_steps or not self.stop_conditions:
            raise ValueError("SectionExplanationContract requires reasoning steps and stop conditions.")


@dataclass(frozen=True)
class AssetDecision:
    asset_id: str
    decision: Literal["inline", "reference", "omit"]
    reason: str = ""
    explanation_job: str = ""


@dataclass(frozen=True)
class AssetPlan:
    decisions: tuple[AssetDecision, ...] = ()

    @property
    def inline_asset_ids(self) -> tuple[str, ...]:
        return tuple(item.asset_id for item in self.decisions if item.decision == "inline")


CoverageObligationKind = Literal["argument", "experiment", "fact", "limitation", "asset"]


@dataclass(frozen=True)
class CoverageObligation:
    obligation_id: str
    kind: CoverageObligationKind
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CoverageConflict:
    """Deterministic, writer-blocking coverage validation result."""

    missing: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    invented: tuple[str, ...] = ()
    inconsistent: tuple[str, ...] = ()

    @property
    def is_blocking(self) -> bool:
        return bool(self.missing or self.duplicates or self.invented or self.inconsistent)


@dataclass(frozen=True)
class CoverageLedger:
    """A deterministic one-owner index for all non-optional reading obligations."""

    obligations: tuple[CoverageObligation, ...]
    assignments: Mapping[str, str]

    @property
    def assigned_obligation_ids(self) -> tuple[str, ...]:
        return tuple(self.assignments)

    @property
    def obligation_count(self) -> int:
        return len(self.obligations)

    def section_for(self, obligation_id: str) -> str | None:
        return self.assignments.get(obligation_id)

    @classmethod
    def build(cls, paper_model: "PaperModel | Mapping[str, Any]", asset_plan: "AssetPlan | Mapping[str, Any]") -> "CoverageLedger | CoverageConflict":
        obligations = _coverage_obligations(paper_model, asset_plan)
        expected_ids = tuple(item.obligation_id for item in obligations)
        expected = set(expected_ids)
        assigned: list[tuple[str, str]] = []
        for section in _coverage_sections(paper_model):
            section_id = _value(section, "section_id", "id", default="")
            for obligation_id in _sequence(_value(section, "coverage_obligation_ids", "obligations", default=())):
                raw_id = str(obligation_id)
                normalized_id = _normalize_coverage_id(raw_id, expected_ids)
                assigned.append((normalized_id, section_id))

        assigned_ids = [item[0] for item in assigned]
        missing = tuple(item for item in expected_ids if item not in assigned_ids)
        duplicates = tuple(dict.fromkeys(item for item in assigned_ids if assigned_ids.count(item) > 1))
        invented = tuple(dict.fromkeys(item for item in assigned_ids if item not in expected))
        inconsistent: list[str] = []
        inline_ids = set(_asset_plan_inline_ids(asset_plan))
        for obligation_id, _section_id in assigned:
            if obligation_id.startswith("asset:") and obligation_id not in inline_ids:
                inconsistent.append(obligation_id)
        conflict = CoverageConflict(missing, duplicates, invented, tuple(dict.fromkeys(inconsistent)))
        if conflict.is_blocking:
            return conflict
        return cls(tuple(obligations), {obligation_id: section_id for obligation_id, section_id in assigned})


@dataclass(frozen=True)
class PaperModel:
    """Compact evidence-typed state produced from one complete ordered paper."""

    thesis: str
    argument_chain: Mapping[str, str]
    coverage: Mapping[str, Mapping[str, Any]]
    source_facts: tuple[SourceFact, ...]
    source_limitations: tuple[SourceFact, ...] = ()
    agent_syntheses: tuple[AgentSynthesis, ...] = ()
    material_unknowns: tuple[Unknown, ...] = ()
    definition_neighborhoods: tuple["DefinitionNeighborhood", ...] = ()
    visual_decisions: tuple[VisualDecision, ...] = ()
    visual_interpretations: tuple[VisualInterpretation, ...] = ()
    section_contracts: tuple[SectionExplanationContract, ...] = ()
    experiments: tuple[Mapping[str, Any], ...] = ()
    must_preserve_facts: tuple[Mapping[str, Any], ...] = ()
    limitations: tuple[Mapping[str, Any], ...] = ()

    @property
    def covers_core_argument(self) -> bool:
        required_slots = {"background", "problem", "prior_gap", "mechanism", "experiment", "boundary"}
        complete_slots = {
            name
            for name, value in self.coverage.items()
            if isinstance(value, Mapping) and str(value.get("status", "")).casefold() == "complete"
        }
        facets = {fact.facet for fact in self.source_facts}
        return required_slots.issubset(complete_slots) and facets == {"problem", "method", "experiment", "limitation"} and not any(
            item.priority == "high" for item in self.material_unknowns
        )


@dataclass(frozen=True)
class DefinitionNeighborhood:
    """A selected formula/table plus the local prose that gives it meaning."""

    object_block_id: str
    kind: Literal["formula", "table"]
    source_block_ids: tuple[str, ...]
    context: str
    object_content: str = ""
    defined_symbols: tuple[str, ...] = ()
    formula_symbols: tuple[str, ...] = ()
    table_rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ReadingReceipt:
    source_id: str
    status: Literal["completed", "bounded", "failed"]
    text_model: str
    vision_model: str | None
    strategy: ReadingStrategy = "target_fallback"
    resolved_text_model: str | None = None
    resolved_vision_model: str | None = None
    target_count: int = 0
    expansion_count: int = 0
    text_calls: int = 0
    vision_calls: int = 0
    planner_calls: int = 0
    writer_calls: int = 0
    full_read_calls: int = 0
    unknown_count: int = 0
    paper_ir_version: str = ""
    prompt_version: str = ""
    image_count: int = 0
    image_bytes: int = 0
    blind_review_status: Literal["not_run", "passed", "failed"] = "not_run"
    blind_review_failures: tuple[str, ...] = ()
    blind_review_calls: int = 0
    stop_reason: StopReason = "failed"
    fallbacks: tuple[str, ...] = ()
    degradations: tuple[str, ...] = ()
    budget: Mapping[str, int] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    section_repair_calls: int = 0
    writer_repair_calls: int = 0
    ledger_obligation_count: int = 0
    ledger_assigned_count: int = 0
    ledger_conflicts: tuple[str, ...] = ()
    asset_decisions: Mapping[str, str] = field(default_factory=dict)
    asset_decision_counts: Mapping[str, int] = field(default_factory=dict)
    length_observation: int | None = None
    unsupported_writer_claims: tuple[str, ...] = ()
    provider_failures: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class ReadingTrace:
    skeleton: PaperSkeleton
    questions: tuple[ReadingQuestion, ...]
    targets: tuple[ReadingTarget, ...]
    bundles: tuple[EvidenceBundle, ...]
    records: tuple[ReadingRecord, ...]
    argument_maps: tuple[ArgumentMap, ...]
    stop_reason: StopReason
    visual_decisions: tuple[VisualDecision, ...] = ()
    paper_model: PaperModel | None = None
    strategy: ReadingStrategy = "target_fallback"
    section_contracts: tuple[SectionExplanationContract, ...] = ()
    asset_plan: AssetPlan = field(default_factory=AssetPlan)
    coverage_ledger: CoverageLedger | None = None
    coverage_conflict: CoverageConflict | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ReadingResult:
    draft: ReadingDraft
    receipt: ReadingReceipt
    trace: ReadingTrace


@dataclass(frozen=True)
class SkimRequest:
    candidate: PaperCandidate
    paper_ir: CanonicalPaperIR
    navigation_block_ids: tuple[str, ...]


@dataclass(frozen=True)
class FullPaperReadRequest:
    candidate: PaperCandidate
    paper_ir: CanonicalPaperIR
    ordered_block_ids: tuple[str, ...]


@dataclass(frozen=True)
class TargetReadRequest:
    candidate: PaperCandidate
    target: ReadingTarget
    bundle: EvidenceBundle
    blocks: tuple[PaperIRBlock, ...]
    questions: tuple[ReadingQuestion, ...]


@dataclass(frozen=True)
class VisualReadRequest:
    candidate: PaperCandidate
    target: ReadingTarget
    block: PaperIRBlock
    caption: str
    neighboring_context: str


class ReadingModel(Protocol):
    text_model: str
    vision_model: str | None

    def read_full_paper(self, request: FullPaperReadRequest) -> Mapping[str, Any]: ...

    def skim(self, request: SkimRequest) -> Mapping[str, Any]: ...

    def read_target(self, request: TargetReadRequest) -> Mapping[str, Any]: ...

    def interpret_visual(self, request: VisualReadRequest) -> str: ...


@dataclass(frozen=True)
class StopDecision:
    should_stop: bool
    reason: StopReason | None
    unresolved_high_priority: tuple[str, ...] = ()


class StopController:
    """Circuit breaker: a budget boundary never upgrades unanswered questions."""

    def decide(
        self,
        questions: Sequence[ReadingQuestion],
        records: Sequence[ReadingRecord],
        *,
        target_count: int,
        expansion_count: int,
        intent: ReadingIntent,
        last_expansion_added_evidence: bool | None = None,
    ) -> StopDecision:
        unresolved = tuple(question.question_id for question in questions if question.priority == "high" and question.status != "supported")
        unresolved_any = tuple(question.question_id for question in questions if question.status != "supported")
        if not unresolved_any:
            return StopDecision(True, "coverage", ())
        if target_count >= intent.max_targets or expansion_count >= intent.max_targets * max(1, intent.max_expansions_per_target):
            return StopDecision(True, "budget", unresolved)
        if last_expansion_added_evidence is False:
            return StopDecision(True, "no_progress", unresolved)
        if records and all(record.evidence_gaps and not record.has_high_priority_unknown for record in records[-1:]):
            return StopDecision(True, "evidence_boundary", unresolved)
        return StopDecision(False, None, unresolved)


def apply_argument_patch(
    argument_map: ArgumentMap,
    *,
    node_id: str,
    status: ArgumentStatus,
    evidence_refs: tuple[str, ...],
    reason: str,
) -> ArgumentMap:
    """Append one inspectable map revision without overwriting its history."""

    if argument_map.version < 0 or not reason.strip():
        raise ValueError("An ArgumentMap patch needs a revision reason.")
    if not evidence_refs:
        raise ValueError("An ArgumentMap patch needs evidence or a recorded conflict reference.")
    found = False
    nodes: list[ArgumentNode] = []
    for node in argument_map.nodes:
        if node.node_id != node_id:
            nodes.append(node)
            continue
        found = True
        nodes.append(replace(node, status=status, evidence_refs=tuple(dict.fromkeys((*node.evidence_refs, *evidence_refs))), revision_reason=reason))
    if not found:
        raise KeyError(node_id)
    return ArgumentMap(argument_map.version + 1, tuple(nodes), argument_map.version, f"patch:{node_id}:v{argument_map.version + 1}", reason)


def visual_decision(block: PaperIRBlock, target: ReadingTarget) -> VisualDecision:
    """Decide visual relevance from the target and paper structure, not word overlap."""

    if not block.is_visual:
        return VisualDecision("skip", "不是视觉对象，使用文本路径。", block.block_id, target.target_id)
    if not block.image_path:
        return VisualDecision("skip", "视觉对象没有可读取的图片资产。", block.block_id, target.target_id)
    if not block.safe_image or block.parse_status != "available":
        return VisualDecision("skip", "图片不满足安全且可解析的视觉输入条件。", block.block_id, target.target_id)
    facet = _facet_for_question(re.sub(r"^验证[:：]\s*", "", target.task))
    structural_facet = _facet_for_section(block.section)
    if facet == "method" and structural_facet == "method":
        return VisualDecision("inspect", "方法目标需要确认图示中的组件、关系或信息流；不要求特定图号或论文词汇。", block.block_id, target.target_id)
    if facet == "experiment" and structural_facet == "experiment":
        return VisualDecision("inspect", "实验目标需要确认图示的比较或结果结构。", block.block_id, target.target_id)
    return VisualDecision("skip", "当前 target 的成功标准不需要该视觉对象，保留带理由的跳过记录。", block.block_id, target.target_id)


def route_block(block: PaperIRBlock, target: ReadingTarget) -> Literal["text", "vision", "skip"]:
    """Choose the least expensive modality based on explanatory need."""

    if block.is_visual:
        return "vision" if visual_decision(block, target).action == "inspect" else "skip"
    target_words = set(re.findall(r"[\w\u4e00-\u9fff]+", target.task.casefold()))
    block_words = set(re.findall(r"[\w\u4e00-\u9fff]+", (block.text + " " + block.section).casefold()))
    if target_words and not (target_words & block_words):
        # Text can still be selected by the EvidenceBundle's semantic section/cue ranking;
        # this routing guard only avoids sending clearly unrelated complete text.
        if _facet_for_question(target.task) != _facet_for_section(block.section):
            return "skip"
    return "text"


def source_fact_satisfies_facet(record: ReadingRecord, facet: Facet) -> bool:
    """Only eligible source facts can satisfy the existing facet gate."""

    return any(item.facet == facet and item.evidence_type == "source_fact" and item.source_block_ids for item in record.source_facts)


class DeepSeekPaperReadingModel:
    """Narrow production model port; the response model is recorded verbatim."""

    def __init__(self, *, text_model: str, api_key: str, vision_model: str | None = None, base_url: str = "https://api.deepseek.com/chat/completions") -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key is required server-side.")
        if not text_model.strip():
            raise ValueError("DeepSeek text model must be explicit.")
        self.text_model = text_model
        self.vision_model = vision_model
        self._api_key = api_key
        self._base_url = base_url
        self.resolved_text_model: str | None = None
        self.resolved_vision_model: str | None = None
        self.text_call_count = 0
        self.vision_call_count = 0
        self.planner_call_count = 0
        self.writer_call_count = 0
        self.blind_review_call_count = 0
        self.fallbacks: list[str] = []
        self.provider_failures: list[Mapping[str, Any]] = []
        self.last_full_paper_response: Mapping[str, Any] = {}
        self.last_visual_response: Mapping[str, Any] = {}

    @classmethod
    def from_environment(cls) -> "DeepSeekPaperReadingModel":
        key = os.getenv("DEEPSEEK_API_KEY", "")
        text_model = os.getenv("DEEPSEEK_READING_MODEL", "") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        vision_model = os.getenv("DEEPSEEK_READING_VISION_MODEL", "") or None
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions")
        return cls(text_model=text_model, api_key=key, vision_model=vision_model, base_url=base_url)

    def skim(self, request: SkimRequest) -> Mapping[str, Any]:
        return self._json_call("global_skim", self.text_model, _skim_prompt(request))

    def read_full_paper(self, request: FullPaperReadRequest) -> Mapping[str, Any]:
        response = self._json_call("full_paper_read", self.text_model, _full_paper_prompt(request))
        self.last_full_paper_response = response
        return response

    def repair_section_plan(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Repair only section ownership; never reread the paper or send images."""

        self.planner_call_count += 1
        return self._json_call(
            "section_planning_repair",
            self.text_model,
            json.dumps({
                "operation": "section_planning_repair",
                "task": "Repair only the section ownership contract for an already validated PaperModel. Do not summarize, reread, or add evidence.",
                "rules": [
                    "Return the same ordered section IDs unless a section is genuinely missing.",
                    "Assign every required_obligation_id exactly once to coverage_obligation_ids.",
                    "Do not invent obligation IDs, evidence handles, experiments, facts, limitations, or assets.",
                    "Use exact required IDs, including their type prefixes. A section may own multiple obligations and explain them naturally.",
                    "Do not add target calls, images, character quotas, paragraph quotas, or depth tiers.",
                ],
                "required_obligation_ids": request.get("required_obligation_ids", ()),
                "obligations": request.get("obligations", ()),
                "coverage_conflict": request.get("coverage_conflict", {}),
                "sections": request.get("sections", ()),
                "return": {"sections": [{"section_id": "same ID", "coverage_obligation_ids": ["exact required ID"]}]},
            }, ensure_ascii=False),
        )

    def read_target(self, request: TargetReadRequest) -> Mapping[str, Any]:
        return self._json_call("target_read", self.text_model, _target_prompt(request))

    def interpret_visual(self, request: VisualReadRequest) -> str:
        if not self.vision_model:
            raise RuntimeError("No configured DeepSeek vision model is available.")
        payload = self._json_call("visual_interpretation", self.vision_model, _visual_prompt(request), image=request.block.image_path)
        self.last_visual_response = dict(payload)
        self.resolved_vision_model = str(payload.get("_resolved_model", self.vision_model))
        for key in ("interpretation", "visual_interpretation", "summary", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if not payload.get("_provider_failure"):
            failure = {
                "operation": "visual_interpretation",
                "kind": "missing_field",
                "finish_reason": None,
                "content_chars": 0,
                "completion_tokens": None,
                "json_error_position": None,
            }
            self.provider_failures.append(failure)
            self.fallbacks.append("visual_interpretation:missing_field")
        return ""

    def probe(self) -> Mapping[str, Any]:
        """Make one bounded provider call and retain the model name returned by it."""

        return self._json_call("provider_probe", self.text_model, "Return JSON with {\"ok\": true}; do not add prose.")

    def plan_note(self, value: Mapping[str, Any]) -> Mapping[str, Any]:
        self.planner_call_count += 1
        response = self._json_call(
            "note_plan",
            self.text_model,
            json.dumps({
                "operation": "note_plan",
                "instruction": (
                    "Return JSON containing only an ordered section plan whose sections are sufficient for the supplied explanation contracts; do not impose section, paragraph, or character quotas. "
                    "Use the validated preservation_contract as a dynamic allow-list: retain each eligible fact, its exact numeric tokens, named entities, mechanism terms, experiment setup/comparison/results/boundary, and conclusion boundaries when supported. "
                    "Use complete definition neighborhoods for selected formulas and tables, and reserve an inline visual job only for an asset decision marked inline. "
                    "Do not invent missing meanings, metric-to-value relationships, visual details, evidence handles, or trace fields; do not return raw excerpts or repeat the preservation contract."
                ),
                "preservation_contract": _dynamic_writer_constraint_payload(value),
                "reading_state": value,
                "return_json_with_sections": {"sections": [{"name": "heading", "paragraph_goal": "concise goal", "coverage_obligation_ids": ["exact IDs"]}]},
            }, ensure_ascii=False),
        )
        if not response.get("sections"):
            self.fallbacks.append("note_planner_empty_response")
            return {"sections": [("一句话先说清楚", "当前阅读状态没有返回可用的章节规划。"), ("证据边界", "模型规划响应为空，保留为待复核边界。")]}
        return response

    def write_note(self, value: Mapping[str, Any]) -> str:
        self.writer_call_count += 1
        coverage_ledger = value.get("coverage_ledger", {})
        if not isinstance(coverage_ledger, Mapping):
            coverage_ledger = {}
        compact_plan = {
            "section_contracts": _sequence(value.get("section_contracts", ())),
            "coverage_assignments": coverage_ledger.get("assignments", {}),
            "unresolved_boundaries": _sequence(value.get("unresolved_boundaries", ())),
        }
        response = self._json_call(
            "note_write",
            self.text_model,
            json.dumps({
                "operation": "note_write",
                "instruction": (
                    "Write one fluent, human-readable Chinese paper note from the supplied plan and validated reading state. Return exactly one structured section for every supplied SectionExplanationContract, preserving each section_id exactly once and in contract order; translate the visible heading into natural Chinese. "
                    "Treat preservation_contract as the complete dynamic allow-list: preserve every eligible fact and exact numeric token, retain supplied named mechanism labels when present (a Chinese explanation may follow) and do not translate away the original label on first use, keep every experiment metric attached to its setup/comparison and result context, and state conclusion boundaries and limitations without overclaiming. For every high/medium material_unknown, explicitly preserve its anchor_terms on first mention and say what the paper leaves unknown; never silently turn an unknown into a fact. "
                    "Define formula symbols only from definition_neighborhoods or validated source facts; keep table metrics attached to their original labels, columns, and values. Explain every inline asset in its ledger-assigned section, using only its validated visual interpretation for pixel details; reference and omit assets need no pixel description. Describe inline figures in prose and never emit Markdown image links, block IDs, asset IDs, local paths, or placeholder URLs because the Renderer owns image placement. "
                    "When an allow-listed table is used, preserve its relevant row labels, compared conditions, counts, percentages, and changes rather than summarizing away exact values. Omit unsupported definitions and claims rather than guessing. Return JSON exactly as {title: string, sections: [{section_id: string, heading: string, markdown: string}]}; section markdown must not contain another heading. Do not return an empty field, evidence handles, raw excerpts, provider payloads, or trace fields."
                ),
                "preservation_contract": _dynamic_writer_constraint_payload(value),
                "plan": compact_plan,
                "return_json_with_sections": {"title": "Chinese note title", "sections": [{"section_id": "exact contract section_id", "heading": "natural Chinese heading", "markdown": "section prose without a heading"}]},
                "do_not_emit_block_ids": True,
            }, ensure_ascii=False),
        )
        expected_section_ids = tuple(
            str(item.get("section_id", "")).strip()
            for item in _sequence(value.get("section_contracts", ()))
            if isinstance(item, Mapping) and str(item.get("section_id", "")).strip()
        )
        raw_sections = tuple(item for item in _sequence(response.get("sections", ())) if isinstance(item, Mapping))
        if raw_sections and expected_section_ids:
            by_id = {
                str(item.get("section_id", "")).strip(): item
                for item in raw_sections
                if str(item.get("section_id", "")).strip()
            }
            returned_ids = tuple(str(item.get("section_id", "")).strip() for item in raw_sections)
            if len(by_id) == len(raw_sections) and set(returned_ids) == set(expected_section_ids):
                title = re.sub(r"^#+\s*", "", str(response.get("title", "论文精读")).strip()) or "论文精读"
                parts = [f"# {title}"]
                for section_id in expected_section_ids:
                    item = by_id[section_id]
                    heading = re.sub(r"^#+\s*", "", str(item.get("heading", "")).strip())
                    body = re.sub(r"(?m)^\s*<!--\s*rp-section:[^>]+-->\s*$", "", str(item.get("markdown", "")).strip()).strip()
                    if not heading or not body:
                        break
                    parts.append(f"<!-- rp-section:{section_id} -->\n## {heading}\n\n{body}")
                else:
                    return "\n\n".join(parts) + "\n"
            self.fallbacks.append("note_writer_invalid_structured_sections")
            return ""
        markdown = str(response.get("markdown") or response.get("note") or response.get("content") or "").strip()
        if markdown:
            return markdown + "\n"
        self.fallbacks.append("note_writer_empty_response")
        return ""

    def repair_writer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Repair one evidenced omission without rereading or changing other sections."""

        missing = tuple(request.get("missing_obligations", ()))
        evidence = tuple(request.get("missing_obligation_evidence", ()))
        return self._json_call(
            "writer_repair",
            self.text_model,
            json.dumps({
                "operation": "writer_repair",
                "instruction": (
                    "Return only minimal section_patches for the affected sections; do not return or rewrite the complete note. "
                    "Return exactly one section_patch per missing obligation, in the same order as missing_obligations; each patch must contain exactly one obligation_id. "
                    "Each patch must name one exact existing Markdown heading from the supplied note in after_heading and provide only prose to append in append_markdown. Use only supplied source facts, definition neighborhoods, table rows, and visual interpretations; do not invent evidence, numbers, labels, or pixel details. "
                    "For a material_unknown obligation, explicitly say that the supplied detail is unknown, unspecified, or not established by the paper; do not turn it into a source fact. "
                    "Preserve each material_unknown anchor_term exactly once on first mention so the repaired boundary remains auditable across translation. "
                    "For a must_preserve_fact obligation, preserve every exact_numeric_token with its supplied condition, comparison, metric, and statement meaning. "
                    "For an inline_formula obligation, preserve the supplied equation as math and explain the supported relationship using only its definition_context and defined_symbols. "
                    "For an inline_visual obligation, add a natural prose explanation of the supplied visual interpretation in its affected section and preserve every supplied required_label on first mention. "
                    "Address every listed obligation exactly once before returning the complete note. "
                    "Explain inline visuals in prose only. Do not emit Markdown image links, block IDs, asset IDs, local paths, or placeholder URLs; the Renderer owns image placement."
                ),
                "markdown": request.get("markdown", ""),
                "missing_obligations": missing,
                "missing_obligation_evidence": evidence,
                "required_completion_count": len(missing),
                "affected_sections": request.get("affected_sections", ()),
                "affected_section_headings": request.get("affected_section_headings", ()),
                "unsupported_writer_claims": request.get("unsupported_writer_claims", ()),
                "required_json_shape": {"section_patches": [{"section_id": "exact affected section id", "after_heading": "exact existing Markdown heading", "obligation_ids": ["exactly one missing obligation id"], "append_markdown": "minimal prose containing every required value for that obligation"}]},
            }, ensure_ascii=False),
        )

    def review_note(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Blindly test whether the final note answers its reader contracts."""

        self.blind_review_call_count += 1
        return self._json_call(
            "blind_note_review",
            self.text_model,
            json.dumps({
                "operation": "blind_note_review",
                "instruction": (
                    "Act as a technical reader who has not opened the paper. Use only the Chinese note. "
                    "Answer every supplied section question from the note; do not use outside knowledge or repair the prose. "
                    "Return an empty answer and list the section_id when the note does not contain enough information. "
                    "For every supplied coverage obligation, return one obligation_assessment with supported=true only when this section of the note explains it sufficiently for a technical reader; otherwise return supported=false. "
                    "Do not restate the obligation, copy long quotes, use outside knowledge, or treat mere keyword presence as sufficient explanation."
                ),
                "note": request.get("markdown", ""),
                "questions": request.get("questions", ()),
                "return": {
                    "answers": [{"section_id": "exact section id", "answer": "answer from note only"}],
                    "unanswered_section_ids": ["exact section id"],
                    "obligation_assessments": [{"obligation_id": "exact obligation id", "supported": True}],
                },
            }, ensure_ascii=False),
        )

    def _json_call(self, operation: str, model: str, prompt: str, image: str | None = None) -> Mapping[str, Any]:
        prompt = "Return json only.\n" + prompt
        content: Any = prompt
        if image:
            path = Path(image)
            if not path.is_file():
                raise FileNotFoundError(path)
            content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}}]
        attempts = 2 if operation == "full_paper_read" and image is None else 1
        for attempt in range(attempts):
            if image is None:
                self.text_call_count += 1
            else:
                self.vision_call_count += 1
            active_content = content
            if attempt:
                active_content = (
                    f"{prompt}\n"
                    "The previous response was not complete valid JSON. Retry once with the same required keys, "
                    "but keep every string and array compact, reference stable IDs instead of repeating prose, "
                    "and close the JSON object before the output limit."
                )
            request = Request(
                self._base_url,
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": active_content}],
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                    "temperature": 0.1,
                    "max_tokens": 12_000 if operation == "full_paper_read" else (8_192 if operation == "note_write" else 4_096),
                    "stream": False,
                }).encode(),
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=90) as response:
                    payload = json.loads(response.read().decode())
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"DeepSeek request failed with HTTP {error.code}: {detail}") from error
            choice = payload.get("choices", [{}])[0]
            actual = payload.get("model") or choice.get("model") or model
            self.resolved_text_model = str(actual) if image is None else self.resolved_text_model
            raw_content = choice.get("message", {}).get("content", "")
            if not isinstance(raw_content, str):
                raw_content = ""
            try:
                parsed = json.loads(_strip_json_fence(raw_content))
            except json.JSONDecodeError as error:
                finish_reason = choice.get("finish_reason")
                kind = "truncated_json" if finish_reason == "length" else "invalid_json"
                usage = payload.get("usage", {})
                completion_tokens = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
                failure = {
                    "operation": operation,
                    "kind": kind,
                    "finish_reason": finish_reason,
                    "content_chars": len(raw_content),
                    "completion_tokens": completion_tokens,
                    "json_error_position": error.pos,
                }
                self.provider_failures.append(failure)
                if attempt + 1 < attempts:
                    self.fallbacks.append(f"{operation}:{kind}_retry")
                    continue
                self.fallbacks.append(f"{operation}:{kind}")
                return {"_fallback": kind, "_provider_failure": failure, "_resolved_model": str(actual)}
            if not isinstance(parsed, dict):
                raise ValueError(f"DeepSeek {operation} response must be an object")
            return {**parsed, "_resolved_model": str(actual)}
        raise AssertionError("Provider retry loop exhausted without a result.")


@dataclass(frozen=True)
class DeterministicReadingModel:
    """Stable fake adapter for contract and integration tests."""

    text_model: str = "text-test-model"
    vision_model: str | None = "vision-test-model"
    skim_response: Mapping[str, Any] = field(default_factory=dict)
    target_responses: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def skim(self, request: SkimRequest) -> Mapping[str, Any]:
        return self.skim_response

    def read_target(self, request: TargetReadRequest) -> Mapping[str, Any]:
        return self.target_responses.get(request.target.target_id, {})

    def interpret_visual(self, request: VisualReadRequest) -> str:
        return f"视觉对象 {request.block.block_id} 对当前目标的关系仍需结合正文核对。"


class PaperReader:
    """Deep module hiding global skim, target loop, evidence and map updates."""

    def __init__(self, model: ReadingModel, *, note_planner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None, writer: Callable[[Mapping[str, Any]], str] | None = None) -> None:
        self.model = model
        # When the model supplies its own note planner/writer (e.g. the DeepSeek
        # paper-reading model), default to them.  This keeps `PaperReader(model)`
        # usable without the caller wiring lambdas, and avoids the broken template
        # fallback that tried to unpack dict section-contracts as (name, text).
        self.note_planner = note_planner or (model.plan_note if hasattr(model, "plan_note") else None)
        self.writer = writer or (model.write_note if hasattr(model, "write_note") else None)

    def _prepare_coverage(
        self,
        paper_model: PaperModel,
        *,
        allow_repair: bool = True,
    ) -> tuple[PaperModel, AssetPlan, CoverageLedger | None, CoverageConflict | None, int]:
        asset_plan = _asset_plan_from_paper_model(paper_model)
        paper_model = _ensure_section_contracts(paper_model, asset_plan)
        built = CoverageLedger.build(paper_model, asset_plan)
        repair_calls = 0
        if isinstance(built, CoverageConflict) and allow_repair and hasattr(self.model, "repair_section_plan"):
            repair_calls = 1
            response = self.model.repair_section_plan({
                "paper_model": _jsonable(paper_model),
                "coverage_conflict": _jsonable(built),
                "required_obligation_ids": tuple(item.obligation_id for item in _coverage_obligations(paper_model, asset_plan)),
                "obligations": _compact_coverage_obligations(paper_model, asset_plan),
                "asset_plan": _jsonable(asset_plan),
                "sections": _jsonable(_coverage_sections(paper_model)),
            })
            if isinstance(response, Mapping):
                contracts = _validated_section_contracts(response)
                if contracts:
                    paper_model = replace(paper_model, section_contracts=contracts)
                    built = CoverageLedger.build(paper_model, asset_plan)
            if isinstance(built, CoverageConflict):
                repaired_contracts = _deterministic_section_repair(paper_model, asset_plan)
                if repaired_contracts:
                    paper_model = replace(paper_model, section_contracts=repaired_contracts)
                    built = CoverageLedger.build(paper_model, asset_plan)
        if isinstance(built, CoverageConflict):
            return paper_model, asset_plan, None, built, repair_calls
        return paper_model, asset_plan, built, None, repair_calls

    def _blocked_full_paper_result(
        self,
        candidate: PaperCandidate,
        canonical_paper_ir: CanonicalPaperIR,
        reading_intent: ReadingIntent,
        started: str,
        paper_model: PaperModel,
        asset_plan: AssetPlan,
        conflict: CoverageConflict,
        section_repair_calls: int,
        text_calls: int,
        vision_calls: int,
        visual_decisions: Sequence[VisualDecision] = (),
    ) -> ReadingResult:
        skeleton, questions, argument_map = self._make_orientation(canonical_paper_ir, {"coverage": paper_model.coverage, "argument_chain": paper_model.argument_chain})
        completed = datetime.now(timezone.utc).isoformat()
        obligation_count = len(_coverage_obligations(paper_model, asset_plan))
        image_count, image_bytes = _receipt_image_usage(canonical_paper_ir, visual_decisions, paper_model.visual_interpretations)
        receipt = ReadingReceipt(
            source_id=candidate.source_id,
            status="failed",
            text_model=getattr(self.model, "text_model", type(self.model).__name__),
            vision_model=getattr(self.model, "vision_model", None),
            strategy="full_paper",
            resolved_text_model=getattr(self.model, "resolved_text_model", None) or getattr(self.model, "text_model", None),
            resolved_vision_model=getattr(self.model, "resolved_vision_model", None) or getattr(self.model, "vision_model", None),
            text_calls=getattr(self.model, "text_call_count", text_calls),
            vision_calls=getattr(self.model, "vision_call_count", vision_calls),
            planner_calls=getattr(self.model, "planner_call_count", section_repair_calls),
            writer_calls=getattr(self.model, "writer_call_count", 0),
            full_read_calls=1,
            unknown_count=len(paper_model.material_unknowns),
            paper_ir_version=canonical_paper_ir.parser_version,
            prompt_version=READING_PROMPT_VERSION,
            image_count=image_count,
            image_bytes=image_bytes,
            stop_reason="failed",
            fallbacks=tuple(getattr(self.model, "fallbacks", ())),
            degradations=("coverage_conflict",),
            budget={"max_targets": reading_intent.max_targets, "max_expansions_per_target": reading_intent.max_expansions_per_target},
            started_at=started,
            completed_at=completed,
            section_repair_calls=section_repair_calls,
            ledger_obligation_count=obligation_count,
            ledger_assigned_count=0,
            ledger_conflicts=_coverage_conflict_labels(conflict),
            asset_decisions={item.asset_id: item.decision for item in asset_plan.decisions},
            asset_decision_counts=_asset_decision_counts(asset_plan),
            provider_failures=tuple(getattr(self.model, "provider_failures", ())),
        )
        trace = ReadingTrace(
            skeleton,
            tuple(questions),
            (),
            (),
            (),
            (argument_map,),
            "failed",
            tuple(visual_decisions),
            paper_model=paper_model,
            strategy="full_paper",
            section_contracts=paper_model.section_contracts,
            asset_plan=asset_plan,
            coverage_conflict=conflict,
        )
        return ReadingResult(ReadingDraft(markdown="", unresolved_boundaries=tuple(item.statement for item in paper_model.material_unknowns)), receipt, trace)

    def _provider_failed_result(
        self,
        candidate: PaperCandidate,
        canonical_paper_ir: CanonicalPaperIR,
        reading_intent: ReadingIntent,
        started: str,
        response: Mapping[str, Any],
        text_calls: int,
    ) -> ReadingResult:
        """Stop a failed full-paper call without inventing evidence-gap targets."""

        completed = datetime.now(timezone.utc).isoformat()
        fallback = str(response.get("_fallback", "provider_failure")).strip() or "provider_failure"
        operation = "full_paper_read"
        failure = response.get("_provider_failure")
        if isinstance(failure, Mapping):
            operation = str(failure.get("operation", operation)).strip() or operation
        fallback_label = f"{operation}:{fallback}"
        model_fallbacks = tuple(getattr(self.model, "fallbacks", ()))
        fallbacks = model_fallbacks if fallback_label in model_fallbacks else (*model_fallbacks, fallback_label)
        skeleton, questions, argument_map = self._make_orientation(canonical_paper_ir, {})
        degradation = "paper_model_contract" if fallback == "invalid_contract" else "provider_format_error"
        receipt = ReadingReceipt(
            source_id=candidate.source_id,
            status="failed",
            text_model=getattr(self.model, "text_model", type(self.model).__name__),
            vision_model=getattr(self.model, "vision_model", None),
            strategy="full_paper",
            resolved_text_model=getattr(self.model, "resolved_text_model", None) or getattr(self.model, "text_model", None),
            resolved_vision_model=getattr(self.model, "resolved_vision_model", None) or getattr(self.model, "vision_model", None),
            text_calls=getattr(self.model, "text_call_count", text_calls),
            vision_calls=getattr(self.model, "vision_call_count", 0),
            planner_calls=getattr(self.model, "planner_call_count", 0),
            writer_calls=getattr(self.model, "writer_call_count", 0),
            full_read_calls=1,
            unknown_count=0,
            paper_ir_version=canonical_paper_ir.parser_version,
            prompt_version=READING_PROMPT_VERSION,
            asset_decision_counts={"inline": 0, "reference": 0, "omit": 0},
            stop_reason="failed",
            fallbacks=fallbacks,
            degradations=(degradation,),
            budget={"max_targets": reading_intent.max_targets, "max_expansions_per_target": reading_intent.max_expansions_per_target},
            started_at=started,
            completed_at=completed,
            provider_failures=tuple(getattr(self.model, "provider_failures", ())),
        )
        trace = ReadingTrace(
            skeleton,
            questions,
            (),
            (),
            (),
            (argument_map,),
            "failed",
            strategy="full_paper",
        )
        return ReadingResult(ReadingDraft(markdown=""), receipt, trace)

    def _write_and_validate_note(
        self,
        candidate: PaperCandidate,
        argument_map: ArgumentMap,
        records: Sequence[ReadingRecord],
        questions: Sequence[ReadingQuestion],
        *,
        paper_model: PaperModel,
        asset_plan: AssetPlan,
        coverage_ledger: CoverageLedger | None,
    ) -> tuple[str, tuple[str, ...], int]:
        markdown = self._write_note(
            candidate,
            argument_map,
            records,
            questions,
            paper_model=paper_model,
            asset_plan=asset_plan,
            coverage_ledger=coverage_ledger,
        )
        markdown = _remove_cross_section_numeric_repetition(markdown, paper_model, coverage_ledger)
        writer_empty = not markdown.strip()
        markdown, rejected_symbols = _guard_unsupported_symbol_definitions(markdown, paper_model)
        markdown, rejected_tables = _guard_unsupported_table_claims(markdown, paper_model)
        markdown, rejected_asset_links = _guard_unsupported_asset_links(markdown)
        missing_obligations = _unexpressed_writer_obligations(markdown, paper_model, asset_plan, coverage_ledger)
        rejected = tuple([
            *(("empty_writer_output",) if writer_empty else ()),
            *(f"unsupported_writer_claim:{symbol}" for symbol in rejected_symbols),
            *(f"unsupported_writer_claim:table:{block_id}" for block_id in rejected_tables),
            *(f"unsupported_writer_claim:asset_link:{target}" for target in rejected_asset_links),
            *(f"unexpressed_obligation:{obligation_id}" for obligation_id in missing_obligations),
        ])
        repair_calls = 0
        if rejected and not writer_empty and hasattr(self.model, "repair_writer"):
            section_contract_by_id = {section.section_id: section for section in paper_model.section_contracts}
            # At most two bounded rounds.  The second round exists only for
            # obligations a provider omitted from its first section response;
            # it is not an open-ended "repair until green" loop.
            for _repair_round in range(2):
                if not missing_obligations:
                    break
                obligation_sections = {
                    obligation_id: _writer_obligation_section_id(obligation_id, paper_model, coverage_ledger)
                    for obligation_id in missing_obligations
                }
                obligations_by_section: dict[str, list[str]] = {}
                for obligation_id in missing_obligations:
                    section_id = obligation_sections.get(obligation_id)
                    if section_id and section_id in section_contract_by_id:
                        obligations_by_section.setdefault(section_id, []).append(obligation_id)
                round_calls = 0
                for section_id, section_obligations in obligations_by_section.items():
                    contract = section_contract_by_id[section_id]
                    ordinal = next(index for index, item in enumerate(paper_model.section_contracts) if item.section_id == section_id)
                    actual_heading = _markdown_heading_for_contract(markdown, contract.heading, ordinal, len(paper_model.section_contracts), section_id)
                    response = self.model.repair_writer({
                        "markdown": _markdown_section_for_contract(markdown, contract.heading, ordinal, len(paper_model.section_contracts), section_id),
                        "unsupported_writer_claims": tuple(f"unexpressed_obligation:{item}" for item in section_obligations),
                        "missing_obligations": tuple(section_obligations),
                        "missing_obligation_evidence": _writer_repair_evidence(
                            section_obligations,
                            paper_model,
                            asset_plan,
                            coverage_ledger,
                        ),
                        "affected_sections": (section_id,),
                        "affected_section_headings": ({"section_id": section_id, "markdown_heading": actual_heading},),
                        "coverage_ledger": _jsonable(coverage_ledger),
                        "paper_model": _jsonable(paper_model),
                        "asset_plan": _jsonable(asset_plan),
                        "visual_interpretations": _jsonable(paper_model.visual_interpretations),
                        "definition_neighborhoods": _jsonable(paper_model.definition_neighborhoods),
                    })
                    repair_calls += 1
                    round_calls += 1
                    if not isinstance(response, Mapping):
                        continue
                    repaired, _section_applied_ids = _apply_writer_section_patches(
                        markdown,
                        response.get("section_patches", ()),
                        section_obligations,
                        section_headings={section_id: actual_heading} if actual_heading else {},
                        obligation_sections=obligation_sections,
                    )
                    if isinstance(repaired, str) and repaired.strip():
                        markdown = repaired
                if not round_calls:
                    break
                markdown, rejected_symbols = _guard_unsupported_symbol_definitions(markdown, paper_model)
                markdown, rejected_tables = _guard_unsupported_table_claims(markdown, paper_model)
                markdown, rejected_asset_links = _guard_unsupported_asset_links(markdown)
                missing_obligations = _unexpressed_writer_obligations(
                    markdown,
                    paper_model,
                    asset_plan,
                    coverage_ledger,
                )
                rejected = tuple([
                    *(f"unsupported_writer_claim:{symbol}" for symbol in rejected_symbols),
                    *(f"unsupported_writer_claim:table:{block_id}" for block_id in rejected_tables),
                    *(f"unsupported_writer_claim:asset_link:{target}" for target in rejected_asset_links),
                    *(f"unexpressed_obligation:{obligation_id}" for obligation_id in missing_obligations),
                ])
        return markdown, rejected, repair_calls

    def _blind_review_note(
        self,
        markdown: str,
        paper_model: PaperModel,
        coverage_ledger: CoverageLedger,
    ) -> tuple[Literal["not_run", "passed", "failed"], tuple[str, ...], int]:
        if not markdown.strip() or not hasattr(self.model, "review_note"):
            return "not_run", (), 0
        failures: list[str] = []
        calls = 0
        compact_obligations = {
            item["obligation_id"]: item["label"]
            for item in _compact_coverage_obligations(paper_model, AssetPlan())
        }
        for index, section in enumerate(paper_model.section_contracts):
            section_obligations = tuple(
                {
                    "obligation_id": obligation.obligation_id,
                    "kind": obligation.kind,
                    "label": compact_obligations.get(obligation.obligation_id, ""),
                }
                for obligation in coverage_ledger.obligations
                if coverage_ledger.section_for(obligation.obligation_id) == section.section_id
            )
            section_markdown = _markdown_section_for_contract(
                markdown,
                section.heading,
                index,
                len(paper_model.section_contracts),
                section.section_id,
            )
            response = self.model.review_note({
                "markdown": section_markdown,
                "questions": ({
                "section_id": section.section_id,
                "heading": section.heading,
                "reader_question": section.reader_question,
                "stop_conditions": section.stop_conditions,
                "coverage_obligations": section_obligations,
                },),
            })
            calls += 1
            if not isinstance(response, Mapping) or response.get("_fallback") or response.get("_provider_failure"):
                failures.append(section.section_id)
                continue
            answered = {
                str(item.get("section_id", "")).strip()
                for item in _sequence(response.get("answers", ()))
                if isinstance(item, Mapping) and str(item.get("answer", "")).strip()
            }
            declared_missing = {
                str(item).strip() for item in _sequence(response.get("unanswered_section_ids", ())) if str(item).strip()
            }
            if section.section_id not in answered or section.section_id in declared_missing:
                failures.append(section.section_id)
            obligation_quotes = {
                str(item.get("obligation_id", "")).strip(): str(item.get("supporting_quote", "")).strip()
                for item in _sequence(response.get("obligation_evidence", ()))
                if isinstance(item, Mapping) and str(item.get("obligation_id", "")).strip()
            }
            supported_assessments = {
                str(item.get("obligation_id", "")).strip()
                for item in _sequence(response.get("obligation_assessments", ()))
                if isinstance(item, Mapping)
                and str(item.get("obligation_id", "")).strip()
                and item.get("supported") is True
            }
            failures.extend(
                item["obligation_id"]
                for item in section_obligations
                if item["obligation_id"] not in supported_assessments
                and (
                    not obligation_quotes.get(item["obligation_id"])
                    or obligation_quotes[item["obligation_id"]] not in section_markdown
                )
            )
        unique_failures = tuple(dict.fromkeys(failures))
        return (("failed", unique_failures, calls) if unique_failures else ("passed", (), calls))

    def read(self, candidate: PaperCandidate, canonical_paper_ir: CanonicalPaperIR, reading_intent: ReadingIntent) -> ReadingResult:
        if candidate.source_id != canonical_paper_ir.source_id:
            raise ValueError("Candidate and Canonical PaperIR source IDs must match.")
        started = datetime.now(timezone.utc).isoformat()
        text_calls = vision_calls = 0
        fallbacks: list[str] = []
        degradations: list[str] = []
        paper_model: PaperModel | None = None
        asset_plan = AssetPlan()
        coverage_ledger: CoverageLedger | None = None
        coverage_conflict: CoverageConflict | None = None
        section_repair_calls = 0
        full_response: Mapping[str, Any] = {}
        full_visual_decisions: tuple[VisualDecision, ...] = ()
        if hasattr(self.model, "read_full_paper"):
            full_response = self.model.read_full_paper(FullPaperReadRequest(
                candidate,
                canonical_paper_ir,
                tuple(block.block_id for block in canonical_paper_ir.ordered_blocks),
            ))
            text_calls += 1
            if full_response.get("_fallback") or full_response.get("_provider_failure"):
                return self._provider_failed_result(
                    candidate,
                    canonical_paper_ir,
                    reading_intent,
                    started,
                    full_response,
                    text_calls,
                )
            paper_model = _validated_paper_model(full_response, canonical_paper_ir)
            if not paper_model.section_contracts:
                return self._provider_failed_result(
                    candidate,
                    canonical_paper_ir,
                    reading_intent,
                    started,
                    {
                        "_fallback": "invalid_contract",
                        "_provider_failure": {
                            "operation": "full_paper_read",
                            "kind": "invalid_contract",
                        },
                    },
                    text_calls,
                )
            paper_model, full_visual_decisions, used_vision = self._inspect_full_paper_visuals(candidate, canonical_paper_ir, paper_model)
            vision_calls += used_vision
            if paper_model.covers_core_argument:
                paper_model, asset_plan, coverage_ledger, coverage_conflict, section_repair_calls = self._prepare_coverage(paper_model)
                if coverage_conflict is not None:
                    return self._blocked_full_paper_result(
                        candidate,
                        canonical_paper_ir,
                        reading_intent,
                        started,
                        paper_model,
                        asset_plan,
                        coverage_conflict,
                        section_repair_calls,
                        text_calls,
                        vision_calls,
                        full_visual_decisions,
                    )
                skeleton, questions, argument_map = self._make_orientation(canonical_paper_ir, full_response)
                supported_questions = tuple(replace(question, status="supported") for question in questions)
                markdown, rejected_claims, writer_repair_calls = self._write_and_validate_note(
                    candidate,
                    argument_map,
                    (),
                    supported_questions,
                    paper_model=paper_model,
                    asset_plan=asset_plan,
                    coverage_ledger=coverage_ledger,
                )
                degradations.extend(rejected_claims)
                blind_review_status, blind_review_failures, blind_review_calls = (
                    self._blind_review_note(markdown, paper_model, coverage_ledger) if not rejected_claims else ("not_run", (), 0)
                )
                if blind_review_failures:
                    degradations.extend(f"blind_review:{section_id}" for section_id in blind_review_failures)
                markdown = _strip_section_markers(markdown)
                completed = datetime.now(timezone.utc).isoformat()
                image_count, image_bytes = _receipt_image_usage(canonical_paper_ir, full_visual_decisions, paper_model.visual_interpretations)
                source_fact_ids = tuple(fact.fact_id for fact in paper_model.source_facts)
                draft = ReadingDraft(markdown=markdown, source_fact_ids=source_fact_ids)
                receipt = ReadingReceipt(
                    source_id=candidate.source_id,
                    status="failed" if degradations else "completed",
                    text_model=getattr(self.model, "text_model", type(self.model).__name__),
                    vision_model=getattr(self.model, "vision_model", None),
                    strategy="full_paper",
                    resolved_text_model=getattr(self.model, "resolved_text_model", None) or getattr(self.model, "text_model", None),
                    resolved_vision_model=getattr(self.model, "resolved_vision_model", None) or getattr(self.model, "vision_model", None),
                    target_count=0,
                    expansion_count=0,
                    text_calls=getattr(self.model, "text_call_count", text_calls),
                    vision_calls=getattr(self.model, "vision_call_count", vision_calls),
                    planner_calls=getattr(self.model, "planner_call_count", section_repair_calls),
                    writer_calls=getattr(self.model, "writer_call_count", 0),
                    full_read_calls=1,
                    unknown_count=len(paper_model.material_unknowns),
                    paper_ir_version=canonical_paper_ir.parser_version,
                    prompt_version=READING_PROMPT_VERSION,
                    image_count=image_count,
                    image_bytes=image_bytes,
                    blind_review_status=blind_review_status,
                    blind_review_failures=blind_review_failures,
                    blind_review_calls=blind_review_calls,
                    stop_reason="coverage",
                    fallbacks=tuple(getattr(self.model, "fallbacks", ())),
                    degradations=tuple(degradations),
                    budget={"max_targets": reading_intent.max_targets, "max_expansions_per_target": reading_intent.max_expansions_per_target},
                    started_at=started,
                    completed_at=completed,
                    section_repair_calls=section_repair_calls,
                    writer_repair_calls=writer_repair_calls,
                    ledger_obligation_count=coverage_ledger.obligation_count,
                    ledger_assigned_count=len(coverage_ledger.assigned_obligation_ids),
                    asset_decisions={item.asset_id: item.decision for item in asset_plan.decisions},
                    asset_decision_counts=_asset_decision_counts(asset_plan),
                    length_observation=len(markdown),
                    unsupported_writer_claims=tuple(degradations),
                    provider_failures=tuple(getattr(self.model, "provider_failures", ())),
                )
                trace = ReadingTrace(
                    skeleton,
                    supported_questions,
                    (),
                    (),
                    (),
                    (argument_map,),
                    "coverage",
                    tuple(full_visual_decisions),
                    paper_model=paper_model,
                    strategy="full_paper",
                    section_contracts=paper_model.section_contracts,
                    asset_plan=asset_plan,
                    coverage_ledger=coverage_ledger,
                )
                return ReadingResult(draft=draft, receipt=receipt, trace=trace)
        if paper_model is not None:
            skim = full_response
        else:
            skim = self._skim(candidate, canonical_paper_ir)
            text_calls += 1 if hasattr(self.model, "skim") else 0
        skeleton, questions, argument_map = self._make_orientation(canonical_paper_ir, skim)
        targets: list[ReadingTarget] = []
        bundles: list[EvidenceBundle] = []
        records: list[ReadingRecord] = []
        maps: list[ArgumentMap] = [argument_map]
        current_questions = list(_paper_model_gap_questions(paper_model, questions) if paper_model is not None else questions)
        expansion_count = 0
        last_added: bool | None = None
        stop_reason: StopReason = "failed"
        visual_decisions: list[VisualDecision] = list(paper_model.visual_decisions) if paper_model is not None else []

        for index, question in enumerate(current_questions[: reading_intent.max_targets]):
            target = self._select_target(question, skeleton, argument_map, index)
            targets.append(target)
            bundle = self._initial_bundle(target, canonical_paper_ir, question)
            bundles.append(bundle)
            visual_decisions.extend(
                visual_decision(canonical_paper_ir.block_by_id[block_id], target)
                for block_id in bundle.source_block_ids
                if canonical_paper_ir.block_by_id[block_id].is_visual
            )
            record, used_text, used_vision = self._read_record(candidate, target, bundle, canonical_paper_ir, current_questions)
            text_calls += used_text
            vision_calls += used_vision
            records.append(record)
            if record.evidence_gaps and expansion_count < reading_intent.max_expansions_per_target:
                expanded = self._expand_bundle(target, bundle, record, canonical_paper_ir)
                if expanded is not None:
                    bundles.append(expanded)
                    expansion_count += 1
                    visual_decisions.extend(
                        visual_decision(canonical_paper_ir.block_by_id[block_id], target)
                        for block_id in expanded.added_block_ids
                        if canonical_paper_ir.block_by_id[block_id].is_visual
                    )
                    expanded_record, used_text, used_vision = self._read_record(candidate, target, expanded, canonical_paper_ir, current_questions)
                    text_calls += used_text
                    vision_calls += used_vision
                    records.append(replace(expanded_record, version=record.version + 1, previous_version=record.version))
                    last_added = bool(expanded.added_block_ids)
                else:
                    last_added = False
            maps.append(self._revise_map(maps[-1], records[-1], target))
            current_questions = self._update_questions(current_questions, records[-1])
            decision = StopController().decide(current_questions, records, target_count=len(targets), expansion_count=expansion_count, intent=reading_intent, last_expansion_added_evidence=last_added)
            if decision.should_stop:
                stop_reason = decision.reason or "evidence_boundary"
                break
        if stop_reason == "failed":
            stop_reason = "budget" if len(targets) >= reading_intent.max_targets else "evidence_boundary"
        if paper_model is not None:
            paper_model = _merge_target_records_into_paper_model(paper_model, records, current_questions)
        final_map = maps[-1]
        writer_repair_calls = 0
        fallback_asset_plan = _asset_plan_from_paper_model(paper_model) if paper_model is not None else AssetPlan()
        fallback_ledger: CoverageLedger | None = None
        fallback_conflict: CoverageConflict | None = None
        if paper_model is not None:
            paper_model, fallback_asset_plan, fallback_ledger, fallback_conflict, section_repair_calls = self._prepare_coverage(paper_model, allow_repair=True)
            if fallback_conflict is not None:
                completed = datetime.now(timezone.utc).isoformat()
                target_visuals = tuple(item for record in records for item in record.visual_interpretations)
                image_count, image_bytes = _receipt_image_usage(
                    canonical_paper_ir,
                    visual_decisions,
                    tuple((*paper_model.visual_interpretations, *target_visuals)),
                )
                receipt = ReadingReceipt(
                    source_id=candidate.source_id,
                    status="failed",
                    text_model=getattr(self.model, "text_model", type(self.model).__name__),
                    vision_model=getattr(self.model, "vision_model", None),
                    strategy="target_fallback",
                    resolved_text_model=getattr(self.model, "resolved_text_model", None) or getattr(self.model, "text_model", None),
                    resolved_vision_model=getattr(self.model, "resolved_vision_model", None) or getattr(self.model, "vision_model", None),
                    target_count=len(targets),
                    expansion_count=expansion_count,
                    text_calls=getattr(self.model, "text_call_count", text_calls),
                    vision_calls=getattr(self.model, "vision_call_count", vision_calls),
                    planner_calls=getattr(self.model, "planner_call_count", section_repair_calls),
                    writer_calls=getattr(self.model, "writer_call_count", 0),
                    full_read_calls=1,
                    unknown_count=len(paper_model.material_unknowns) + sum(len(record.unknowns) for record in records),
                    paper_ir_version=canonical_paper_ir.parser_version,
                    prompt_version=READING_PROMPT_VERSION,
                    image_count=image_count,
                    image_bytes=image_bytes,
                    stop_reason="failed",
                    fallbacks=tuple((*fallbacks, *getattr(self.model, "fallbacks", ()))),
                    degradations=("coverage_conflict",),
                    budget={"max_targets": reading_intent.max_targets, "max_expansions_per_target": reading_intent.max_expansions_per_target},
                    started_at=started,
                    completed_at=completed,
                    section_repair_calls=section_repair_calls,
                    ledger_obligation_count=len(_coverage_obligations(paper_model, fallback_asset_plan)),
                    ledger_assigned_count=0,
                    ledger_conflicts=_coverage_conflict_labels(fallback_conflict),
                    asset_decisions={item.asset_id: item.decision for item in fallback_asset_plan.decisions},
                    asset_decision_counts=_asset_decision_counts(fallback_asset_plan),
                    provider_failures=tuple(getattr(self.model, "provider_failures", ())),
                )
                trace = ReadingTrace(
                    skeleton,
                    tuple(current_questions),
                    tuple(targets),
                    tuple(bundles),
                    tuple(records),
                    tuple(maps),
                    "failed",
                    tuple(visual_decisions),
                    paper_model=paper_model,
                    strategy="target_fallback",
                    section_contracts=paper_model.section_contracts,
                    asset_plan=fallback_asset_plan,
                    coverage_conflict=fallback_conflict,
                )
                return ReadingResult(
                    draft=ReadingDraft(
                        markdown="",
                        unresolved_boundaries=tuple(item.statement for item in paper_model.material_unknowns),
                    ),
                    receipt=receipt,
                    trace=trace,
                )
            markdown, rejected_claims, writer_repair_calls = self._write_and_validate_note(
                candidate,
                final_map,
                records,
                current_questions,
                paper_model=paper_model,
                asset_plan=fallback_asset_plan,
                coverage_ledger=fallback_ledger,
            )
            degradations.extend(rejected_claims)
            blind_review_status, blind_review_failures, blind_review_calls = (
                self._blind_review_note(markdown, paper_model, fallback_ledger) if not rejected_claims else ("not_run", (), 0)
            )
            if blind_review_failures:
                degradations.extend(f"blind_review:{section_id}" for section_id in blind_review_failures)
        else:
            markdown = self._write_note(candidate, final_map, records, current_questions)
            blind_review_status, blind_review_failures, blind_review_calls = "not_run", (), 0
        markdown = _strip_section_markers(markdown)
        paper_fact_ids = [fact.fact_id for fact in paper_model.source_facts] if paper_model is not None else []
        record_fact_ids = [fact.fact_id for record in records for fact in record.source_facts]
        source_fact_ids = tuple(dict.fromkeys((*paper_fact_ids, *record_fact_ids)))
        unresolved_boundaries = tuple(item.statement for item in paper_model.material_unknowns) if paper_model is not None else ()
        if records:
            unresolved_boundaries += tuple(item.statement for item in records[-1].unknowns)
        draft = ReadingDraft(markdown=markdown, unresolved_boundaries=unresolved_boundaries, source_fact_ids=source_fact_ids)
        completed = datetime.now(timezone.utc).isoformat()
        target_visuals = tuple(item for record in records for item in record.visual_interpretations)
        image_count, image_bytes = _receipt_image_usage(
            canonical_paper_ir,
            visual_decisions,
            tuple((*paper_model.visual_interpretations, *target_visuals)) if paper_model is not None else target_visuals,
        )
        receipt = ReadingReceipt(
            source_id=candidate.source_id,
            status="failed" if degradations else ("completed" if stop_reason == "coverage" else "bounded"),
            text_model=getattr(self.model, "text_model", type(self.model).__name__),
            vision_model=getattr(self.model, "vision_model", None),
            strategy="target_fallback",
            resolved_text_model=getattr(self.model, "resolved_text_model", None) or getattr(self.model, "text_model", None),
            resolved_vision_model=getattr(self.model, "resolved_vision_model", None) or getattr(self.model, "vision_model", None),
            target_count=len(targets), expansion_count=expansion_count,
            text_calls=getattr(self.model, "text_call_count", text_calls), vision_calls=getattr(self.model, "vision_call_count", vision_calls),
            planner_calls=getattr(self.model, "planner_call_count", 0), writer_calls=getattr(self.model, "writer_call_count", 0),
            full_read_calls=1 if paper_model is not None else 0,
            unknown_count=(len(paper_model.material_unknowns) if paper_model is not None else 0) + sum(len(record.unknowns) for record in records),
            paper_ir_version=canonical_paper_ir.parser_version,
            prompt_version=READING_PROMPT_VERSION,
            image_count=image_count,
            image_bytes=image_bytes,
            blind_review_status=blind_review_status,
            blind_review_failures=blind_review_failures,
            blind_review_calls=blind_review_calls,
            stop_reason=stop_reason, fallbacks=tuple((*fallbacks, *getattr(self.model, "fallbacks", ()))), degradations=tuple(degradations),
            budget={"max_targets": reading_intent.max_targets, "max_expansions_per_target": reading_intent.max_expansions_per_target},
            started_at=started, completed_at=completed,
            section_repair_calls=section_repair_calls,
            writer_repair_calls=writer_repair_calls,
            ledger_obligation_count=len(_coverage_obligations(paper_model, fallback_asset_plan)) if paper_model is not None else 0,
            ledger_assigned_count=len(fallback_ledger.assigned_obligation_ids) if fallback_ledger is not None else 0,
            ledger_conflicts=_coverage_conflict_labels(fallback_conflict) if fallback_conflict is not None else (),
            asset_decisions={item.asset_id: item.decision for item in fallback_asset_plan.decisions},
            asset_decision_counts=_asset_decision_counts(fallback_asset_plan),
            length_observation=len(markdown),
            unsupported_writer_claims=tuple(degradations),
            provider_failures=tuple(getattr(self.model, "provider_failures", ())),
        )
        trace = ReadingTrace(
            skeleton,
            tuple(current_questions),
            tuple(targets),
            tuple(bundles),
            tuple(records),
            tuple(maps),
            stop_reason,
            tuple(visual_decisions),
            paper_model=paper_model,
            strategy="target_fallback",
            section_contracts=paper_model.section_contracts if paper_model is not None else (),
            asset_plan=fallback_asset_plan,
            coverage_ledger=fallback_ledger,
            coverage_conflict=fallback_conflict,
        )
        return ReadingResult(draft=draft, receipt=receipt, trace=trace)

    def _skim(self, candidate: PaperCandidate, paper_ir: CanonicalPaperIR) -> Mapping[str, Any]:
        if hasattr(self.model, "skim"):
            return self.model.skim(SkimRequest(candidate, paper_ir, tuple(block.block_id for block in paper_ir.navigation_blocks())))
        return {}

    def _inspect_full_paper_visuals(
        self,
        candidate: PaperCandidate,
        paper_ir: CanonicalPaperIR,
        paper_model: PaperModel,
    ) -> tuple[PaperModel, tuple[VisualDecision, ...], int]:
        decisions: list[VisualDecision] = []
        interpretations = list(paper_model.visual_interpretations)
        vision_calls = 0
        ordered = list(paper_ir.ordered_blocks)
        for decision in paper_model.visual_decisions:
            block = paper_ir.block_by_id[decision.block_id]
            current = decision
            if decision.action == "inline" and decision.requires_pixels:
                if not block.is_visual or not block.image_path or not block.safe_image or block.parse_status != "available":
                    current = replace(decision, action="reference", reason=f"{decision.reason} 图片资产不可安全读取，降级为正文引用。", requires_pixels=False)
                else:
                    index = next(position for position, item in enumerate(ordered) if item.block_id == block.block_id)
                    target = ReadingTarget(
                        f"full-visual:{block.block_id}",
                        ("full-paper",),
                        (),
                        f"解释视觉对象对论文主线的不可替代信息：{decision.reason}",
                        "只解释像素相对图注和邻近正文新增的信息。",
                        "像素不清楚时保留视觉未知，不猜测。",
                    )
                    try:
                        interpretation = self.model.interpret_visual(VisualReadRequest(
                            candidate,
                            target,
                            block,
                            block.caption or block.text,
                            _neighbor_text(ordered, index),
                        ))
                    except (AttributeError, RuntimeError, FileNotFoundError):
                        interpretation = ""
                    if interpretation:
                        interpretations.append(VisualInterpretation(block.block_id, interpretation, target.target_id))
                        vision_calls += 1
                    else:
                        current = replace(decision, action="reference", reason=f"{decision.reason} 视觉读取失败，降级为正文引用。", requires_pixels=False)
            decisions.append(current)
        return replace(paper_model, visual_decisions=tuple(decisions), visual_interpretations=tuple(interpretations)), tuple(decisions), vision_calls

    def _make_orientation(self, paper_ir: CanonicalPaperIR, response: Mapping[str, Any]) -> tuple[PaperSkeleton, tuple[ReadingQuestion, ...], ArgumentMap]:
        sections = {block.section: "navigation" for block in paper_ir.navigation_blocks()}
        raw_roles = response.get("section_roles", sections)
        section_roles = {str(k): str(v) for k, v in raw_roles.items()} if isinstance(raw_roles, Mapping) else sections
        raw_refs = response.get("cross_references", {})
        cross_references = (
            {
                str(k): tuple(str(v) for v in values)
                for k, values in raw_refs.items()
                if isinstance(values, (list, tuple))
            }
            if isinstance(raw_refs, Mapping)
            else {}
        )
        skeleton = PaperSkeleton(
            paper_type=str(response.get("paper_type", "empirical method")),
            section_roles=section_roles,
            candidate_contributions=tuple(str(x) for x in response.get("candidate_contributions", ["核心方法需要通过正文验证"])[:5]),
            candidate_experiments=tuple(str(x) for x in response.get("candidate_experiments", ["实验是否验证核心机制"])[:5]),
            cross_references=cross_references,
        )
        defaults = [
            ("为什么要做这项工作？", "high"), ("具体问题是什么？", "high"), ("已有方法为什么不够？", "high"),
            ("核心机制如何回应问题？", "high"), ("哪些实验真正验证了机制？", "high"),
            ("结论边界和局限是什么？", "medium"),
        ]
        raw_questions = response.get("questions")
        questions = tuple(
            ReadingQuestion(f"q-{index + 1}", str(item.get("text", item)) if isinstance(item, dict) else str(item), item.get("priority", priority) if isinstance(item, dict) else priority, rationale="global skim")
            for index, (item, (_, priority)) in enumerate(zip(raw_questions or [x[0] for x in defaults], defaults))
        )
        if not 5 <= len(questions) <= 7:
            questions = tuple(ReadingQuestion(f"q-{index + 1}", text, priority, rationale="global skim") for index, (text, priority) in enumerate(defaults))
        roles = ("problem", "problem", "problem", "method", "experiment", "limitation")
        supplied_nodes = response.get("argument_nodes", ())
        supplied_statements = [
            str(item.get("statement", "")).strip()
            for item in supplied_nodes
            if isinstance(item, Mapping) and str(item.get("statement", "")).strip()
        ] if isinstance(supplied_nodes, (list, tuple)) else []
        statements = supplied_statements[:6]
        for index, role in enumerate(roles[len(statements):], start=len(statements)):
            statements.append(_orientation_claim(paper_ir, role, index))
        nodes = tuple(ArgumentNode(f"arg-{index + 1}", statement, "hypothesis") for index, statement in enumerate(statements[:6]))
        return skeleton, questions, ArgumentMap(version=0, nodes=nodes)

    def _select_target(self, question: ReadingQuestion, skeleton: PaperSkeleton, argument_map: ArgumentMap, index: int) -> ReadingTarget:
        node = argument_map.nodes[min(index, len(argument_map.nodes) - 1)].node_id
        facet = _facet_for_question(question.text)
        success = {
            "problem": "用回答问题背景和具体缺口的原子 source_fact 建立问题判断。",
            "method": "用原子 source_fact 说明论文命名的核心机制、关键组件及其如何回应问题；机制关系不能替代来源事实。",
            "experiment": "用原子 source_fact 说明实验设置、比较条件、指标、关键结果及其解释，并逐字保留来源中的关键名称和精确数字。",
            "limitation": "用原子 source_fact 说明结论边界、已知局限和不能由结果推出的内容。",
        }[facet]
        return ReadingTarget(f"t-{index + 1}", (question.question_id,), (node,), f"验证：{question.text}", success, "保留 unknown 和 evidence gap，不把缺证据写成结论。")

    def _initial_bundle(self, target: ReadingTarget, paper_ir: CanonicalPaperIR, question: ReadingQuestion) -> EvidenceBundle:
        readable = [block for block in paper_ir.ordered_blocks if _candidate_bundle_block(block)]
        preferred = _facet_for_question(question.text)
        query = f"{question.text} {target.task} {target.success_criteria}".casefold()
        terms = set(re.findall(r"[\w\u4e00-\u9fff]+", query))
        ranked = sorted(
            readable,
            key=lambda block: (
                -_target_block_score(block, preferred, query, terms),
                block.order,
            ),
        )
        selected: list[PaperIRBlock] = []
        if preferred == "method":
            seen_sections: set[str] = set()
            for block in ranked:
                section_key = block.section.casefold().strip()
                if (
                    _facet_for_section(block.section) == "method"
                    and not block.is_visual
                    and section_key not in seen_sections
                ):
                    selected.append(block)
                    seen_sections.add(section_key)
                if len(seen_sections) >= 3:
                    break
            _add_first_matching(
                selected,
                ranked,
                lambda block: block.is_visual and _facet_for_section(block.section) == "method",
            )
        elif preferred == "experiment":
            setup_cues = ("task", "dataset", "split", "sample", "training", "held-out", "evaluation setup", "protocol", "任务", "数据集", "划分", "设置")
            setup_candidates = [
                block for block in readable
                if (
                    _facet_for_section(block.section) == "experiment"
                    and block.kind == "paragraph"
                    and any(cue in block.text.casefold() for cue in setup_cues)
                )
            ]
            if setup_candidates:
                first_setup_section = setup_candidates[0].section.casefold().strip()
                same_section = [block for block in setup_candidates if block.section.casefold().strip() == first_setup_section]
                selected.extend(sorted(same_section, key=lambda block: (-len(block.text), block.order))[:2])
                _add_first_matching(
                    selected,
                    readable,
                    lambda block: (
                        block.kind == "table"
                        and block.section.casefold().strip() == first_setup_section
                    ),
                )
            _add_first_matching(
                selected,
                ranked,
                lambda block: block.kind == "table" and _facet_for_section(block.section) == "experiment",
            )
        for block in ranked:
            block_facet = _facet_for_section(block.section)
            structurally_relevant = (
                block_facet == preferred
                or (preferred == "method" and block.is_visual)
                or (preferred == "experiment" and block.kind == "table")
            )
            if not structurally_relevant:
                continue
            if block not in selected:
                selected.append(block)
            if len(selected) >= (4 if preferred in {"method", "experiment"} else 3):
                break
        if not selected and ranked:
            selected.append(ranked[0])
        selected = sorted(selected[: (4 if preferred in {"method", "experiment"} else 3)], key=lambda block: block.order)
        return EvidenceBundle(target.target_id, 1, tuple(block.block_id for block in selected), {block.block_id: _bundle_reason(block, preferred) for block in selected})

    def _expand_bundle(self, target: ReadingTarget, bundle: EvidenceBundle, record: ReadingRecord, paper_ir: CanonicalPaperIR) -> EvidenceBundle | None:
        existing = set(bundle.source_block_ids)
        desired = {facet for gap in record.evidence_gaps for facet in gap.requested_facets}
        candidates = [
            block for block in paper_ir.ordered_blocks
            if block.block_id not in existing and (
                not desired
                or set(block.facets) & desired
                or any(marker in block.section.casefold() for marker in ("experiment", "result", "limitation", "evaluation"))
            )
        ]
        if not candidates:
            return None
        added = candidates[:2]
        ids = bundle.source_block_ids + tuple(block.block_id for block in added)
        return EvidenceBundle(target.target_id, bundle.version + 1, ids, {**bundle.selection_reasons, **{block.block_id: "expanded for recorded evidence gap" for block in added}}, "recorded evidence gap", tuple(block.block_id for block in added), bundle.version)

    def _read_record(self, candidate: PaperCandidate, target: ReadingTarget, bundle: EvidenceBundle, paper_ir: CanonicalPaperIR, questions: Sequence[ReadingQuestion]) -> tuple[ReadingRecord, int, int]:
        blocks = tuple(paper_ir.block_by_id[item] for item in bundle.source_block_ids)
        response = self.model.read_target(TargetReadRequest(candidate, target, bundle, blocks, tuple(questions))) if hasattr(self.model, "read_target") else {}
        facts: list[SourceFact] = []
        relations: list[MechanismRelation] = []
        syntheses: list[AgentSynthesis] = []
        unknowns: list[Unknown] = []
        gaps: list[EvidenceGap] = []
        for item in response.get("source_facts", []) if isinstance(response, Mapping) else []:
            if isinstance(item, Mapping) and item.get("statement"):
                ids = _response_source_ids(item, bundle.source_block_ids)
                statement = _atomic_target_fact(str(item["statement"]), target, ids, paper_ir)
                facet = str(item.get("facet", _facet_for_question(target.task)))
                if facet not in {"problem", "method", "experiment", "limitation"}:
                    facet = _facet_for_question(re.sub(r"^验证[:：]\s*", "", target.task))
                if statement and ids:
                    facts.append(SourceFact(str(item.get("fact_id", f"fact-{target.target_id}-model-{len(facts)}")), statement, facet, ids))
        if response.get("mechanism_relations"):
            for index, item in enumerate(response["mechanism_relations"]):
                if isinstance(item, Mapping):
                    ids = _response_source_ids(item, bundle.source_block_ids)
                    relation = str(item.get("relation", "")).strip()
                    if relation and ids and set(ids).issubset(set(bundle.source_block_ids)):
                        relations.append(MechanismRelation(f"relation-{target.target_id}-{index}", relation, ids))
        for index, block in enumerate(blocks):
            if route_block(block, target) == "vision" and block.image_path and block.safe_image:
                try:
                    interpretation = self.model.interpret_visual(VisualReadRequest(candidate, target, block, block.caption or "", _neighbor_text(blocks, index)))
                    if interpretation:
                        response = dict(response)
                        response.setdefault("visual_interpretations", []).append(VisualInterpretation(block.block_id, interpretation, target.target_id))
                except (AttributeError, RuntimeError):
                    pass
        if isinstance(response, Mapping):
            for index, item in enumerate(response.get("agent_syntheses", ())):
                if isinstance(item, Mapping) and item.get("statement"):
                    ids = tuple(item.get("source_block_ids", ()))
                    if set(ids).issubset(set(bundle.source_block_ids)):
                        syntheses.append(AgentSynthesis(f"synthesis-{target.target_id}-{index}", str(item["statement"]), ids))
            for index, item in enumerate(response.get("unknowns", ())):
                if isinstance(item, Mapping):
                    statement = str(item.get("statement", "")).strip()
                    if statement:
                        unknowns.append(Unknown(f"unknown-{target.target_id}-{index}", statement, item.get("priority", "high")))
            for index, item in enumerate(response.get("evidence_gaps", ())):
                if isinstance(item, Mapping):
                    statement = str(item.get("statement", "")).strip()
                    if statement:
                        gaps.append(EvidenceGap(f"gap-{target.target_id}-{index}", statement, tuple(item.get("requested_facets", ()))))
            conflicts = tuple(str(item) for item in response.get("conflicts", ()) if str(item).strip())
        else:
            conflicts = ()
        relevant_facts = [fact for fact in facts if _fact_matches_target(fact, target)]
        relations = [relation for relation in relations if relation.relation.strip() and relation.source_block_ids]
        if not relevant_facts and not relations:
            unknowns.append(Unknown(f"unknown-{target.target_id}-default", "当前材料尚不足以验证该目标。", "high"))
            gaps.append(EvidenceGap(f"gap-{target.target_id}-default", "需要跨章节证据验证目标。", ("method", "experiment")))
        visual_values: list[VisualInterpretation] = []
        if isinstance(response, Mapping):
            visual_ids = {block.block_id for block in blocks if block.is_visual}
            for item in response.get("visual_interpretations", ()):
                if isinstance(item, VisualInterpretation):
                    if item.visual_block_id in visual_ids:
                        visual_values.append(item)
                elif isinstance(item, Mapping):
                    block_id = str(item.get("visual_block_id", item.get("block_id", "")))
                    interpretation = str(item.get("interpretation", "")).strip()
                    if block_id in visual_ids and interpretation:
                        visual_values.append(VisualInterpretation(block_id, interpretation, target.target_id))
        visuals = tuple(visual_values)
        return ReadingRecord(target.target_id, tuple(item.question_id for item in questions if item.question_id in target.question_ids), bundle.version, 1, bundle.source_block_ids, tuple(relevant_facts), tuple(relations), visuals, tuple(syntheses), tuple(unknowns), tuple(gaps), conflicts), 1, sum(route_block(block, target) == "vision" for block in blocks)

    def _revise_map(self, previous: ArgumentMap, record: ReadingRecord, target: ReadingTarget | None = None) -> ArgumentMap:
        target_node_ids = target.argument_node_ids if target else ((f"arg-{record.target_id.rsplit('-', 1)[-1]}",) if record.target_id.rsplit("-", 1)[-1].isdigit() else ())
        evidence_refs = (f"record:{record.target_id}:v{record.version}",) + tuple(fact.fact_id for fact in record.source_facts) + tuple(relation.relation_id for relation in record.mechanism_relations)
        if not evidence_refs:
            evidence_refs = (f"record:{record.target_id}:v{record.version}",)
        has_evidence = bool(record.source_facts or record.mechanism_relations)
        if record.conflicts:
            status: ArgumentStatus = "revised"
            reason = "本轮 ReadingRecord 保留冲突，暂不把原假设升级为无条件结论。"
        elif has_evidence and not record.has_high_priority_unknown and not record.evidence_gaps:
            status = "supported"
            reason = "本轮 target 的原子来源事实和机制关系满足成功标准。"
        elif has_evidence:
            status = "revised"
            reason = "本轮证据部分回答 target，但仍保留 unknown 或 evidence gap。"
        else:
            status = "revised"
            reason = "本轮未找到回答 target 的来源事实，记录证据边界并要求后续扩展。"
        nodes: list[ArgumentNode] = []
        for node in previous.nodes:
            if node.node_id not in target_node_ids:
                nodes.append(node)
                continue
            supporting_text = record.source_facts[0].statement if record.source_facts else (record.mechanism_relations[0].relation if record.mechanism_relations else "证据仍不足")
            statement = f"{node.statement}；本轮 target 证据：{supporting_text}"
            nodes.append(replace(node, statement=statement, status=status, evidence_refs=tuple(dict.fromkeys((*node.evidence_refs, *evidence_refs))), revision_reason=reason, supersedes=f"{node.node_id}@v{previous.version}"))
        return ArgumentMap(previous.version + 1, tuple(nodes), previous.version, f"record-{record.target_id}-v{record.version}", reason)

    def _update_questions(self, questions: Sequence[ReadingQuestion], record: ReadingRecord) -> tuple[ReadingQuestion, ...]:
        supported_questions = {
            question.question_id
            for question in questions
            if question.question_id in record.question_ids
            and any(_fact_matches_question(fact, question.text) for fact in record.source_facts)
            and not record.has_high_priority_unknown
            and not record.evidence_gaps
        }
        return tuple(replace(question, status="supported" if question.question_id in supported_questions else ("partial" if question.question_id in record.question_ids else question.status)) for question in questions)

    def _write_note(
        self,
        candidate: PaperCandidate,
        argument_map: ArgumentMap,
        records: Sequence[ReadingRecord],
        questions: Sequence[ReadingQuestion],
        *,
        paper_model: PaperModel | None = None,
        asset_plan: AssetPlan | None = None,
        coverage_ledger: CoverageLedger | None = None,
        coverage_conflict: CoverageConflict | None = None,
    ) -> str:
        unresolved = tuple(question.text for question in questions if question.status != "supported") + tuple(
            item.statement for item in paper_model.material_unknowns
        ) if paper_model else tuple(question.text for question in questions if question.status != "supported")
        compressed = tuple({
            "target_id": record.target_id,
            "question_ids": record.question_ids,
            "source_facts": tuple({"fact_id": fact.fact_id, "statement": fact.statement, "facet": fact.facet, "source_block_ids": fact.source_block_ids} for fact in record.source_facts[:6]),
            "mechanism_relations": tuple({"relation_id": relation.relation_id, "relation": relation.relation, "source_block_ids": relation.source_block_ids} for relation in record.mechanism_relations[:4]),
            "visual_interpretations": tuple({"visual_block_id": item.visual_block_id, "interpretation": item.interpretation} for item in record.visual_interpretations[:4]),
            "unknowns": tuple({"statement": item.statement, "priority": item.priority} for item in record.unknowns[:4]),
            "evidence_gaps": tuple({"statement": item.statement, "requested_facets": item.requested_facets} for item in record.evidence_gaps[:4]),
        } for record in records)
        evidence_references = tuple(sorted({block_id for record in records for block_id in record.source_block_ids}))
        must_preserve = _build_must_preserve_facts(records)
        if paper_model is not None:
            full_record = ReadingRecord(
                "full-paper",
                (),
                1,
                1,
                tuple(sorted({block_id for fact in (*paper_model.source_facts, *paper_model.source_limitations) for block_id in fact.source_block_ids})),
                source_facts=tuple((*paper_model.source_facts, *paper_model.source_limitations)),
            )
            must_preserve = _build_must_preserve_facts((*records, full_record), paper_model.must_preserve_facts)
        note_input = NotePlanningInput(candidate.title, argument_map, compressed, must_preserve, unresolved, evidence_references)
        payload = _jsonable(note_input)
        if paper_model is not None:
            payload["paper_model"] = _jsonable(paper_model)
            resolved_asset_plan = asset_plan or _asset_plan_from_paper_model(paper_model)
            payload.update(_coverage_payload(paper_model, resolved_asset_plan, coverage_ledger, coverage_conflict))
        if paper_model is not None and paper_model.section_contracts:
            # The full-paper read already planned ordered explanatory sections
            # in the same call that built the PaperModel. Reusing that typed
            # state keeps the v2 route direct and avoids a second lossy planner.
            plan = {
                "strategy": "section_explanation_contracts",
                "sections": _jsonable(paper_model.section_contracts),
            }
        elif self.note_planner:
            plan = self.note_planner(_jsonable(payload))
        else:
            plan = {"sections": [("一句话先说清楚", "这篇论文的核心问题和方法仍需结合证据理解。"), ("证据边界", "未解决的问题已保留为阅读边界。" if any(item.status != "supported" for item in questions) else "核心问题已有结构化证据支持。") ]}
        if not isinstance(plan, Mapping):
            plan = {"sections": ()}
        # The fixed planner may choose section wording, but it cannot drop the
        # typed preservation contract before the fixed writer sees it.
        plan = {
            **dict(plan),
            "must_preserve_facts": payload["must_preserve_facts"],
            "compressed_records": payload["compressed_records"],
            "record_fact_ids": tuple(fact["fact_id"] for fact in payload["must_preserve_facts"]["facts"]),
        }
        if self.writer:
            writer_payload = {
                "note_plan": plan,
                "paper_model": payload.get("paper_model"),
                "section_contracts": payload.get("section_contracts", ()),
                "coverage_ledger": payload.get("coverage_ledger"),
                "coverage_conflict": payload.get("coverage_conflict"),
                "definition_neighborhoods": payload.get("definition_neighborhoods", ()),
                "asset_plan": payload.get("asset_plan", {"decisions": ()}),
                "visual_interpretations": payload.get("visual_interpretations", ()),
                "evidence_allow_list": payload.get("evidence_allow_list", payload["evidence_references"]),
                "must_preserve_facts": payload["must_preserve_facts"],
                "compressed_records": payload["compressed_records"],
                "unresolved_boundaries": payload["unresolved_boundaries"],
                "evidence_references": payload["evidence_references"],
            }
            return self.writer(writer_payload)
        # Last-resort template writer: handle both (name, text) tuples (planner
        # output) and Mapping sections (section_contracts), so the reader never
        # crashes on an unpacking error even for models without their own writer.
        def _section_heading(item: Any) -> tuple[str, str]:
            if isinstance(item, Mapping):
                heading = str(item.get("heading") or item.get("section_id") or "section")
                body = str(item.get("reader_question") or item.get("text") or "")
                return heading, body
            return str(item[0]), str(item[1])
        return "# " + candidate.title + "\n\n" + "\n\n".join(f"## {name}\n\n{text}" for name, text in (_section_heading(item) for item in plan.get("sections", ()))) + "\n"


def _add_first_matching(selected: list[PaperIRBlock], blocks: Sequence[PaperIRBlock], predicate: Callable[[PaperIRBlock], bool]) -> None:
    for block in blocks:
        if block not in selected and predicate(block):
            selected.append(block)
            return


def _orientation_claim(paper_ir: CanonicalPaperIR, role: Facet, index: int) -> str:
    candidates = [block for block in paper_ir.navigation_blocks() if _facet_for_section(block.section) == role and _eligible_reader_fact(block)]
    if not candidates:
        candidates = [block for block in paper_ir.ordered_blocks if _facet_for_section(block.section) == role and _eligible_reader_fact(block)]
    if candidates:
        text = re.sub(r"\s+", " ", candidates[0].text).strip()
        pieces = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+", text) if part.strip()]
        claim = " ".join(pieces[:2]) if pieces else text
        return claim[:720] if len(claim) > 720 else claim
    fallback = {
        "problem": "论文需要从来源材料中确认任务背景、具体风险与既有方法缺口。",
        "method": "论文提出的机制需要由跨章节来源事实说明其输入、审计和作用链。",
        "experiment": "实验论点需要把任务设计、比较表格和结果解释连接起来。",
        "limitation": "结论边界需要说明已验证范围以及不能由结果推出的内容。",
    }
    return fallback[role]


def _target_block_score(block: PaperIRBlock, preferred: Facet, query: str, terms: set[str]) -> int:
    text = f"{block.section} {block.caption or ''} {block.text} {block.table_html or ''}".casefold()
    score = 0
    if _facet_for_section(block.section) == preferred:
        score += 12
    text_terms = set(re.findall(r"[\w\u4e00-\u9fff]+", text))
    score += min(8, len(terms & text_terms))
    query_tokens = re.findall(r"[a-z0-9]+", query)
    query_bigrams = {f"{left} {right}" for left, right in zip(query_tokens, query_tokens[1:]) if len(left) + len(right) >= 5}
    score += min(12, 6 * sum(1 for phrase in query_bigrams if phrase in text))
    score += min(5, len(text) // 180)
    cue_groups = {
        "method": ("method", "mechanism", "architecture", "framework", "algorithm", "training", "implementation", "workflow"),
        "experiment": ("experiment", "evaluation", "result", "benchmark", "baseline", "metric", "ablation", "%"),
        "problem": ("background", "motivation", "challenge", "insufficient", "problem", "gap"),
        "limitation": ("limitation", "boundary", "threat", "unresolved", "future work", "out-of-distribution"),
    }
    score += sum(4 for cue in cue_groups.get(preferred, ()) if cue in text)
    if block.kind == "table" and preferred == "experiment":
        score += 8
    if block.is_visual and preferred == "method":
        score += 6
    return score


def _bundle_reason(block: PaperIRBlock, preferred: Facet) -> str:
    if block.is_visual:
        return "target-aware visual candidate for the method mechanism"
    if block.kind == "table":
        return "target-aware complete table for experiment success criteria"
    if _facet_for_section(block.section) == preferred:
        return f"target-aware {preferred} section evidence"
    return "cross-section context linked by target cues"


def _fact_matches_target(fact: SourceFact, target: ReadingTarget) -> bool:
    task = re.sub(r"^验证[:：]\s*", "", target.task)
    return _fact_matches_text(fact, f"{task} {target.success_criteria}")


def _response_source_ids(item: Mapping[str, Any], bundle_ids: Sequence[str]) -> tuple[str, ...]:
    raw = item.get("source_block_ids", item.get("source_block_id", ()))
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        return ()
    ids = tuple(str(value) for value in raw if str(value).strip())
    return ids if ids and set(ids).issubset(set(bundle_ids)) else ()


def _fact_matches_question(fact: SourceFact, question: str) -> bool:
    return _fact_matches_text(fact, question)


def _fact_matches_text(fact: SourceFact, text: str) -> bool:
    expected = _facet_for_question(text)
    if fact.facet != expected:
        return False
    statement = fact.statement.casefold()
    terms = set(re.findall(r"[a-z][a-z-]{3,}|[\u4e00-\u9fff]{2,}", text.casefold()))
    facet_cues = {
        "problem": ("problem", "challenge", "gap", "motivation", "require", "need", "insufficient", "问题", "挑战", "不足"),
        "method": ("method", "mechanism", "architecture", "algorithm", "workflow", "audit", "verifier", "机制", "架构", "算法", "审计"),
        "experiment": ("table", "result", "evaluation", "metric", "baseline", "ablation", "experiment", "实验", "结果", "%"),
        "limitation": ("limit", "boundary", "threat", "unresolved", "局限", "边界"),
    }
    return bool((terms & set(re.findall(r"[a-z][a-z-]{3,}|[\u4e00-\u9fff]{2,}", statement))) or any(cue in statement for cue in facet_cues[expected]))


def _atomic_target_fact(statement: str, target: ReadingTarget, source_block_ids: tuple[str, ...], paper_ir: CanonicalPaperIR) -> str | None:
    normalized = re.sub(r"\s+", " ", statement).strip()
    if not normalized:
        return None
    source_text = " ".join(paper_ir.block_by_id[item].text for item in source_block_ids if item in paper_ir.block_by_id)
    pieces = [item.strip() for item in re.split(r"(?<=[.!?。！？])\s+", normalized) if item.strip()]
    if normalized == source_text.strip() or len(normalized) > 600:
        preferred = _facet_for_question(target.task)
        cues = {
            "method": ("method", "mechanism", "architecture", "algorithm", "workflow", "audit", "verifier"),
            "experiment": ("table", "task", "dataset", "evaluation", "result", "metric", "baseline", "ablation", "%"),
            "problem": ("background", "motivation", "challenge", "problem", "insufficient", "gap"),
            "limitation": ("boundary", "threat", "limit", "unresolved", "out-of-distribution"),
        }[preferred]
        candidates = [piece for piece in pieces if any(cue in piece.casefold() for cue in cues)]
        normalized = " ".join(candidates[:2] or pieces[:1])
    if len(normalized) > 600:
        normalized = normalized[:600].rsplit(" ", 1)[0]
    return normalized or None


def _build_must_preserve_facts(
    records: Sequence[ReadingRecord],
    declared_facts: Sequence[Mapping[str, Any]] = (),
) -> MustPreserveFacts:
    declarations = {
        str(item.get("fact_id", "")): item
        for item in declared_facts
        if isinstance(item, Mapping) and bool(item.get("eligible", True)) and str(item.get("fact_id", "")).strip()
    }
    preserved: list[MustPreserveFact] = []
    numeric_groups: list[tuple[str, ...]] = []
    named_entities: set[str] = set()
    mechanism_terms: set[str] = set()
    for record in records:
        for fact in record.source_facts:
            numeric = tuple(dict.fromkeys(re.findall(r"\d[\d,]*(?:\.\d+)?%?", fact.statement)))
            declaration = declarations.get(fact.fact_id, {})
            declared_statement = str(declaration.get("statement", "")).strip() if declaration else ""
            declaration_is_grounded = not declared_statement or declared_statement == fact.statement
            if declaration_is_grounded:
                declared_numeric = tuple(
                    str(token) for token in _sequence(declaration.get("numeric_tokens", ()))
                    if str(token).strip() and re.sub(r"[\s,]+", "", str(token)) in re.sub(r"[\s,]+", "", fact.statement)
                )
                numeric = tuple(dict.fromkeys((*numeric, *declared_numeric)))
            if numeric:
                numeric_groups.append(numeric)
            generic_entities = tuple(
                token for token in re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*\b", fact.statement)
                if sum(character.isupper() for character in token) >= 2
                or (any(character.isdigit() for character in token) and any(character.isupper() for character in token))
            )
            declared_entities = tuple(
                str(token) for token in _sequence(declaration.get("named_entities", ()))
                if declaration_is_grounded and str(token).strip() and str(token).casefold() in fact.statement.casefold()
            )
            entities = tuple(dict.fromkeys((*generic_entities, *declared_entities)))
            named_entities.update(entities)
            terms = tuple(dict.fromkeys(
                str(term) for term in _sequence(declaration.get("mechanism_terms", ()))
                if declaration_is_grounded and str(term).strip() and str(term).casefold() in fact.statement.casefold()
            ))
            mechanism_terms.update(terms)
            preserved.append(MustPreserveFact(fact.fact_id, fact.statement, fact.facet, fact.source_block_ids, numeric, entities, terms))
    return MustPreserveFacts(tuple(preserved), tuple(numeric_groups), tuple(sorted(named_entities)), tuple(sorted(mechanism_terms)), tuple(sorted({item for fact in preserved for item in fact.source_block_ids})))


def _value(item: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        for key in keys:
            if key in item:
                return item[key]
    for key in keys:
        if hasattr(item, key):
            return getattr(item, key)
    return default


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(value)
    return ()


def _coverage_sections(paper_model: PaperModel | Mapping[str, Any]) -> tuple[Any, ...]:
    sections = _value(paper_model, "section_contracts", "sections", default=())
    if isinstance(sections, Mapping):
        return tuple({"section_id": key, **(value if isinstance(value, Mapping) else {})} for key, value in sections.items())
    return _sequence(sections)


def _prefixed_obligation_id(prefix: str, raw_id: Any) -> str:
    value = str(raw_id).strip()
    return value if value.startswith(prefix + ":") else f"{prefix}:{value}"


def _items_for_obligation(paper_model: PaperModel | Mapping[str, Any], group: str) -> tuple[Any, ...]:
    value = _value(paper_model, group, default=())
    if group == "argument_chain" and isinstance(value, Mapping):
        return tuple({"node_id": key, "statement": item} for key, item in value.items())
    return _sequence(value)


def _asset_plan_items(asset_plan: AssetPlan | Mapping[str, Any]) -> tuple[Any, ...]:
    if isinstance(asset_plan, AssetPlan):
        return asset_plan.decisions
    return _sequence(_value(asset_plan, "decisions", "assets", default=()))


def _asset_plan_inline_ids(asset_plan: AssetPlan | Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        _prefixed_obligation_id("asset", _value(item, "asset_id", "handle", "block_id", default=""))
        for item in _asset_plan_items(asset_plan)
        if str(_value(item, "decision", "action", default="")).casefold() == "inline"
    )


def _coverage_obligations(
    paper_model: PaperModel | Mapping[str, Any],
    asset_plan: AssetPlan | Mapping[str, Any],
) -> tuple[CoverageObligation, ...]:
    obligations: list[CoverageObligation] = []
    for group, prefix, id_keys in (
        ("argument_chain", "argument", ("node_id", "argument_id", "key", "id")),
        ("experiments", "experiment", ("experiment_id", "key", "id")),
        ("must_preserve_facts", "fact", ("fact_id", "key", "id")),
        ("limitations", "limitation", ("limitation_id", "key", "fact_id", "id")),
    ):
        items = _items_for_obligation(paper_model, group)
        if group == "must_preserve_facts":
            items = tuple(item for item in items if bool(_value(item, "eligible", default=True)))
        for index, item in enumerate(items):
            raw_id = next((_value(item, key, default=None) for key in id_keys if _value(item, key, default=None) not in (None, "")), index)
            obligations.append(CoverageObligation(_prefixed_obligation_id(prefix, raw_id), prefix, _as_mapping(item)))
    for item in _asset_plan_items(asset_plan):
        if str(_value(item, "decision", "action", default="")).casefold() == "inline":
            raw_id = _value(item, "asset_id", "handle", "block_id", default="")
            obligations.append(CoverageObligation(_prefixed_obligation_id("asset", raw_id), "asset", _as_mapping(item)))
    return tuple(obligations)


def _compact_coverage_obligations(
    paper_model: PaperModel | Mapping[str, Any],
    asset_plan: AssetPlan | Mapping[str, Any],
) -> tuple[Mapping[str, str], ...]:
    compact: list[Mapping[str, str]] = []
    for obligation in _coverage_obligations(paper_model, asset_plan):
        payload = obligation.payload
        raw_label = next((
            _value(payload, key, default="")
            for key in ("statement", "reason", "explanation_job", "interpretation", "setup", "comparison", "results")
            if _value(payload, key, default="") not in (None, "", (), [])
        ), "")
        label = re.sub(r"\s+", " ", str(raw_label)).strip()
        compact.append({
            "obligation_id": obligation.obligation_id,
            "kind": obligation.kind,
            "label": label[:360],
        })
    return tuple(compact)


def _as_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if hasattr(item, "__dict__"):
        return {key: value for key, value in vars(item).items() if not key.startswith("_")}
    return {"value": item}


def _validated_section_contracts(response: Mapping[str, Any]) -> tuple[SectionExplanationContract, ...]:
    raw_sections = response.get("sections", response.get("section_contracts", ()))
    contracts: list[SectionExplanationContract] = []
    required_fields = {
        "section_id",
        "heading",
        "reader_question",
        "prerequisite_bridges",
        "reasoning_steps",
        "experiment_slots",
        "asset_jobs",
        "transition_in",
        "transition_out",
        "stop_conditions",
        "evidence_handles",
        "coverage_obligation_ids",
    }
    forbidden_length_fields = {"target_characters", "target_words", "paragraph_count", "depth_tier"}
    for index, item in enumerate(_sequence(raw_sections)):
        if not isinstance(item, Mapping):
            return ()
        if not required_fields.issubset(item) or forbidden_length_fields.intersection(item):
            return ()
        section_id = str(item.get("section_id", "")).strip()
        heading = str(item.get("heading", "")).strip()
        reader_question = str(item.get("reader_question", "")).strip()
        reasoning_steps = tuple(str(value).strip() for value in _sequence(item.get("reasoning_steps", ())) if str(value).strip())
        stop_conditions = tuple(str(value) for value in _sequence(item.get("stop_conditions", ())))
        evidence_handles = tuple(str(value).strip() for value in _sequence(item.get("evidence_handles", ())) if str(value).strip())
        if not section_id or not heading or not reader_question or not reasoning_steps or not stop_conditions or not evidence_handles:
            return ()
        contracts.append(SectionExplanationContract(
            section_id=section_id,
            heading=heading,
            reader_question=reader_question,
            prerequisite_bridges=tuple(str(value) for value in _sequence(item.get("prerequisite_bridges", ()))),
            reasoning_steps=reasoning_steps,
            experiment_slots=tuple(str(value) for value in _sequence(item.get("experiment_slots", ()))),
            asset_jobs=tuple(_as_mapping(value) for value in _sequence(item.get("asset_jobs", ()))),
            transition_in=str(item.get("transition_in", "")),
            transition_out=str(item.get("transition_out", "")),
            stop_conditions=stop_conditions,
            evidence_handles=evidence_handles,
            coverage_obligation_ids=tuple(str(value) for value in _sequence(item.get("coverage_obligation_ids", item.get("obligations", ())))),
        ))
    return tuple(contracts)


def _validated_paper_model(response: Mapping[str, Any], paper_ir: CanonicalPaperIR) -> PaperModel:
    response = _paper_model_response_body(response)
    block_map = paper_ir.block_by_id

    def source_ids(item: Mapping[str, Any]) -> tuple[str, ...]:
        raw = item.get("source_block_ids", item.get("source_block_id", ()))
        if isinstance(raw, str):
            raw = (raw,)
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(dict.fromkeys(str(value) for value in raw if str(value) in block_map))

    facts: list[SourceFact] = []
    for index, item in enumerate(response.get("source_facts", ()) if isinstance(response, Mapping) else ()):
        if not isinstance(item, Mapping):
            continue
        statement = str(item.get("statement", "")).strip()
        ids = source_ids(item)
        ids = _recover_exact_fact_source(statement, ids, paper_ir)
        ids = _complete_numeric_evidence(statement, ids, paper_ir)
        facet = str(item.get("facet", ""))
        if statement and ids and facet in {"problem", "method", "experiment", "limitation"} and _numeric_claim_supported(statement, ids, block_map):
            facts.append(SourceFact(str(item.get("fact_id", f"full-fact-{index + 1}")), statement, facet, ids))

    limitations: list[SourceFact] = []
    for index, item in enumerate(response.get("source_limitations", ()) if isinstance(response, Mapping) else ()):
        if not isinstance(item, Mapping):
            continue
        statement = str(item.get("statement", "")).strip()
        ids = source_ids(item)
        ids = _recover_exact_fact_source(statement, ids, paper_ir)
        ids = _complete_numeric_evidence(statement, ids, paper_ir)
        if statement and ids and _numeric_claim_supported(statement, ids, block_map):
            limitations.append(SourceFact(str(item.get("fact_id", f"full-limitation-{index + 1}")), statement, "limitation", ids))

    syntheses: list[AgentSynthesis] = []
    for index, item in enumerate(response.get("agent_syntheses", ()) if isinstance(response, Mapping) else ()):
        if not isinstance(item, Mapping):
            continue
        statement = str(item.get("statement", "")).strip()
        ids = source_ids(item)
        if statement and ids:
            syntheses.append(AgentSynthesis(str(item.get("synthesis_id", f"full-synthesis-{index + 1}")), statement, ids))

    unknowns: list[Unknown] = []
    for index, item in enumerate(response.get("material_unknowns", ()) if isinstance(response, Mapping) else ()):
        if isinstance(item, Mapping):
            statement = str(item.get("statement", "")).strip()
            priority = str(item.get("priority", "medium"))
        else:
            statement = str(item).strip()
            priority = "medium"
        if statement:
            declared_anchors = tuple(
                str(value).strip()
                for value in _sequence(item.get("anchor_terms", ()))
                if isinstance(item, Mapping) and str(value).strip()
            )
            unknowns.append(Unknown(
                f"full-unknown-{index + 1}",
                statement,
                _bounded_unknown_priority(statement, priority),
                anchor_terms=declared_anchors or _infer_unknown_anchor_terms(statement),
                anchor_terms_declared=bool(declared_anchors),
            ))

    raw_chain = response.get("argument_chain", {}) if isinstance(response, Mapping) else {}
    argument_chain = {str(key): str(value) for key, value in raw_chain.items()} if isinstance(raw_chain, Mapping) else {}
    raw_coverage = response.get("coverage", {}) if isinstance(response, Mapping) else {}
    coverage = {str(key): dict(value) for key, value in raw_coverage.items() if isinstance(value, Mapping)} if isinstance(raw_coverage, Mapping) else {}
    experiments = tuple(_as_mapping(item) for item in _sequence(response.get("experiments", ())))
    must_preserve_facts = tuple(_as_mapping(item) for item in _sequence(response.get("must_preserve_facts", ())))
    if not must_preserve_facts:
        must_preserve_facts = tuple({
            "fact_id": fact.fact_id,
            "statement": fact.statement,
            "facet": fact.facet,
            "source_block_ids": fact.source_block_ids,
            "eligible": True,
        } for fact in facts)
    limitations_payload = tuple(_as_mapping(item) for item in _sequence(response.get("limitations", ())))
    if not limitations_payload:
        limitations_payload = tuple({
            "limitation_id": fact.fact_id,
            "statement": fact.statement,
            "source_block_ids": fact.source_block_ids,
        } for fact in limitations)
    return PaperModel(
        thesis=str(response.get("thesis", "")) if isinstance(response, Mapping) else "",
        argument_chain=argument_chain,
        coverage=coverage,
        source_facts=tuple(facts),
        source_limitations=tuple(limitations),
        agent_syntheses=tuple(syntheses),
        material_unknowns=tuple(unknowns),
        definition_neighborhoods=_definition_neighborhoods(response, paper_ir),
        visual_decisions=_full_paper_visual_decisions(response, paper_ir),
        section_contracts=_validated_section_contracts(response),
        experiments=experiments,
        must_preserve_facts=must_preserve_facts,
        limitations=limitations_payload,
    )


def _paper_model_response_body(response: Mapping[str, Any]) -> Mapping[str, Any]:
    if any(key in response for key in ("argument_chain", "source_facts", "coverage", "thesis")):
        return response
    for key in ("paper_model", "PaperModel", "result", "output", "return"):
        nested = response.get(key)
        if isinstance(nested, Mapping) and any(item in nested for item in ("argument_chain", "source_facts", "coverage", "thesis")):
            return nested
    return response


def _bounded_unknown_priority(statement: str, priority: str) -> Literal["high", "medium", "low"]:
    normalized = priority if priority in {"high", "medium", "low"} else "medium"
    local_detail_markers = (
        "exact definition", "exact method", "exact algorithm", "exact value", "exact composition",
        "not fully specified", "not fully explained", "not fully defined", "not detailed", "only brief", "implementation detail",
        "reason for", "specific reason", "exact reason",
    )
    core_markers = ("decisive", "core argument", "main conclusion", "cannot determine whether", "central mechanism")
    lowered = statement.casefold()
    if normalized == "high" and any(marker in lowered for marker in local_detail_markers) and not any(marker in lowered for marker in core_markers):
        return "medium"
    return normalized


def _paper_model_gap_questions(paper_model: PaperModel, orientation_questions: Sequence[ReadingQuestion]) -> tuple[ReadingQuestion, ...]:
    high_unknowns = [item.statement for item in paper_model.material_unknowns if item.priority == "high"]
    if high_unknowns:
        return tuple(ReadingQuestion(f"q-gap-{index + 1}", statement, "high", rationale="high-priority PaperModel gap") for index, statement in enumerate(high_unknowns[:8]))

    slot_questions = {
        "background": "Why does this research area matter for the paper's problem?",
        "problem": "What concrete problem does the paper establish?",
        "prior_gap": "Why are prior approaches insufficient?",
        "mechanism": "How does the core mechanism address the problem?",
        "experiment": "Which experiment provides the decisive evidence for the mechanism?",
        "boundary": "What do the results not establish?",
    }
    questions = [
        ReadingQuestion(f"q-gap-{len(slot_questions)}-{index + 1}", slot_questions[slot], "high", rationale=f"PaperModel coverage for {slot} is incomplete")
        for index, slot in enumerate(slot_questions)
        if not isinstance(paper_model.coverage.get(slot), Mapping) or str(paper_model.coverage[slot].get("status", "")).casefold() != "complete"
    ]
    present_facets = {fact.facet for fact in paper_model.source_facts}
    facet_questions = {
        "problem": "Which source fact establishes the concrete problem?",
        "method": "Which source fact establishes the core mechanism?",
        "experiment": "Which source fact establishes the decisive experimental result?",
        "limitation": "Which source fact establishes the conclusion boundary?",
    }
    for facet, text in facet_questions.items():
        if facet not in present_facets and all(_facet_for_question(item.text) != facet for item in questions):
            questions.append(ReadingQuestion(f"q-gap-facet-{facet}", text, "high", rationale=f"validated PaperModel lacks {facet} source fact"))
    return tuple(questions[:8] or orientation_questions[:1])


def _merge_target_records_into_paper_model(
    paper_model: PaperModel,
    records: Sequence[ReadingRecord],
    questions: Sequence[ReadingQuestion],
) -> PaperModel:
    recovered_facts = tuple(fact for record in records for fact in record.source_facts)
    merged_facts = tuple({fact.fact_id: fact for fact in (*paper_model.source_facts, *recovered_facts)}.values())
    supported = tuple(question for question in questions if question.status == "supported")
    supported_text = {question.text.strip().casefold() for question in supported}
    remaining_unknowns = tuple(
        item for item in paper_model.material_unknowns if item.statement.strip().casefold() not in supported_text
    )
    coverage = {key: dict(value) for key, value in paper_model.coverage.items()}
    facet_slots = {"problem": "problem", "method": "mechanism", "experiment": "experiment", "limitation": "boundary"}
    for question in supported:
        rationale_match = re.search(r"PaperModel coverage for ([a-z_]+) is incomplete", question.rationale)
        slot = rationale_match.group(1) if rationale_match else facet_slots.get(_facet_for_question(question.text))
        if slot in coverage:
            coverage[slot] = {**coverage[slot], "status": "complete", "missing": [], "reason": "resolved_by_target_evidence"}
    return replace(
        paper_model,
        source_facts=merged_facts,
        material_unknowns=remaining_unknowns,
        coverage=coverage,
    )


def _full_paper_visual_decisions(response: Mapping[str, Any], paper_ir: CanonicalPaperIR) -> tuple[VisualDecision, ...]:
    decisions: list[VisualDecision] = []
    for item in response.get("visual_candidates", ()):
        if not isinstance(item, Mapping):
            continue
        action = str(item.get("decision", "reference"))
        for block in _resolve_available_asset_blocks(str(item.get("block_id", "")), paper_ir):
            if block.kind not in {"formula", "table", "figure", "image", "chart"} or action not in {"inline", "reference", "omit"}:
                continue
            # A real raster figure cannot be meaningfully inline without inspecting
            # its pixels.  If caption/prose alone is sufficient, the provider must
            # choose reference instead of creating an internally contradictory
            # inline decision with requires_pixels=false.
            requires_pixels = block.is_visual and action == "inline"
            decisions.append(VisualDecision(
                action,
                str(item.get("reason", item.get("value", "No explanation value was supplied."))),
                block.block_id,
                "full-paper",
                requires_pixels,
            ))
    selected_block_ids = {item.block_id for item in decisions}
    # SectionExplanationContract.asset_jobs are explicit same-call planning
    # decisions.  Providers occasionally omit the duplicate candidate entry;
    # recover that declaration only when it resolves to a complete structured
    # object or a safe real image.  This does not infer a new asset from prose.
    for section in _sequence(response.get("sections", response.get("section_contracts", ()))):
        if not isinstance(section, Mapping):
            continue
        for job in _sequence(section.get("asset_jobs", ())):
            if not isinstance(job, Mapping):
                continue
            raw_id = str(job.get("asset_id", job.get("block_id", "")))
            for block in _resolve_available_asset_blocks(raw_id, paper_ir):
                usable_structured = block.kind in {"formula", "table"} and block.parse_status == "available"
                usable_visual = block.is_visual and block.parse_status == "available" and block.safe_image and bool(block.image_path)
                if block.block_id in selected_block_ids or not (usable_structured or usable_visual):
                    continue
                reason = str(job.get("job", job.get("reason", "Explain the selected asset in its assigned section.")))
                decisions.append(VisualDecision("inline", reason, block.block_id, "full-paper", block.is_visual))
                selected_block_ids.add(block.block_id)
    selected_ids = {item.block_id for item in decisions if item.action != "omit"}
    selected_method_visual = any(
        block_id in paper_ir.block_by_id
        and paper_ir.block_by_id[block_id].is_visual
        and _facet_for_section(paper_ir.block_by_id[block_id].section) == "method"
        for block_id in selected_ids
    )
    if not selected_method_visual:
        relation_cues = ("architecture", "framework", "pipeline", "workflow", "flow", "loop", "overview", "diagram", "process", "control", "架构", "框架", "流程", "闭环", "结构")
        candidates = [
            block for block in paper_ir.ordered_blocks
            if (
                block.is_visual
                and _facet_for_section(block.section) == "method"
                and block.image_path
                and block.safe_image
                and block.parse_status == "available"
                and any(cue in f"{block.caption or ''} {block.text}".casefold() for cue in relation_cues)
            )
        ]
        if candidates:
            block = candidates[0]
            decisions.append(VisualDecision(
                "inline",
                "方法结构图包含仅靠正文难以替代的组件关系或信息流，补入一个受成本约束的视觉解释槽位。",
                block.block_id,
                "full-paper",
                True,
            ))
    return tuple(decisions)


def _resolve_unique_provider_block_id(raw_block_id: str, paper_ir: CanonicalPaperIR) -> str | None:
    raw = raw_block_id.strip()
    if not raw:
        return None
    if raw in paper_ir.block_by_id:
        return raw
    suffix = f":{raw}"
    matches = [block_id for block_id in paper_ir.block_by_id if block_id.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def _resolve_available_asset_blocks(raw_block_id: str, paper_ir: CanonicalPaperIR) -> tuple[PaperIRBlock, ...]:
    """Resolve one explicit asset job to complete normalized material.

    Dual-parser inputs may retain an unparsed aggregate formula beside complete
    equation-level counterparts.  Recovery is allowed only when both objects
    share an explicit equation tag in the same section; no prose-only formula
    selection happens here.
    """

    block_id = _resolve_unique_provider_block_id(raw_block_id.removeprefix("asset:"), paper_ir)
    block = paper_ir.block_by_id.get(block_id or "")
    if block is None:
        return ()
    if block.kind not in {"formula", "table"} or block.parse_status == "available":
        return (block,)
    if block.kind != "formula":
        return ()
    tags = tuple(dict.fromkeys(re.findall(
        r"(?:\\tag\s*\{|\()\s*(\d+[a-z]?)\s*\}?\)?",
        block.latex or block.text,
        flags=re.IGNORECASE,
    )))
    for tag in tags:
        matches = tuple(
            candidate
            for candidate in paper_ir.ordered_blocks
            if candidate.kind == "formula"
            and candidate.parse_status == "available"
            and (
                candidate.section == block.section
                or candidate.section.casefold().startswith(block.section.casefold())
                or block.section.casefold().startswith(candidate.section.casefold())
            )
            and re.search(
                rf"(?:\\tag\s*\{{|\()\s*{re.escape(tag)}\s*\}}?\)?",
                candidate.latex or candidate.text,
                flags=re.IGNORECASE,
            )
        )
        if matches:
            return (matches[0],)
    return ()


def _asset_plan_from_paper_model(paper_model: PaperModel) -> AssetPlan:
    return AssetPlan(tuple(
        AssetDecision(
            asset_id=_prefixed_obligation_id("asset", decision.block_id),
            decision=decision.action if decision.action in {"inline", "reference", "omit"} else "reference",
            reason=decision.reason,
            explanation_job=decision.reason,
        )
        for decision in paper_model.visual_decisions
        if decision.action in {"inline", "reference", "omit"}
    ))


def _legacy_section_contract(paper_model: PaperModel, asset_plan: AssetPlan) -> tuple[SectionExplanationContract, ...]:
    obligations = _coverage_obligations(paper_model, asset_plan)
    if not obligations:
        return ()
    return (SectionExplanationContract(
        section_id="full-paper",
        heading="全文论证",
        reader_question="这篇论文解决了什么问题、如何验证以及结论边界是什么？",
        prerequisite_bridges=("从论文问题连接到方法和证据。",),
        reasoning_steps=("按论文顺序连接问题、机制、实验和边界。",),
        experiment_slots=tuple(item.obligation_id for item in obligations if item.kind == "experiment"),
        asset_jobs=tuple(item.payload for item in obligations if item.kind == "asset"),
        transition_in="",
        transition_out="",
        stop_conditions=("核心论证、证据和边界均已解释。",),
        evidence_handles=tuple(sorted({block_id for fact in paper_model.source_facts for block_id in fact.source_block_ids})),
        coverage_obligation_ids=tuple(item.obligation_id for item in obligations),
    ),)


def _ensure_section_contracts(paper_model: PaperModel, asset_plan: AssetPlan) -> PaperModel:
    if paper_model.section_contracts:
        return paper_model
    return replace(paper_model, section_contracts=_legacy_section_contract(paper_model, asset_plan))


def _coverage_conflict_labels(conflict: CoverageConflict) -> tuple[str, ...]:
    return tuple(
        [*(f"missing:{item}" for item in conflict.missing),
         *(f"duplicate:{item}" for item in conflict.duplicates),
         *(f"invented:{item}" for item in conflict.invented),
         *(f"inconsistent:{item}" for item in conflict.inconsistent)]
    )


def _normalize_coverage_id(raw_id: str, expected_ids: Sequence[str]) -> str:
    expected = set(expected_ids)
    if raw_id in expected:
        return raw_id
    aliases = tuple(item for item in expected_ids if item.rsplit(":", 1)[-1] == raw_id)
    if len(aliases) == 1:
        return aliases[0]
    raw_tail = re.split(r"[_:]", raw_id)[-1]
    aliases = tuple(item for item in expected_ids if re.split(r"[_:]", item)[-1] == raw_tail)
    return aliases[0] if len(aliases) == 1 else raw_id


def _deterministic_section_repair(paper_model: PaperModel, asset_plan: AssetPlan) -> tuple[SectionExplanationContract, ...]:
    """Assign existing obligations to existing sections without adding evidence."""

    sections = list(paper_model.section_contracts)
    obligations = _coverage_obligations(paper_model, asset_plan)
    if not sections or not obligations:
        return tuple(sections)
    expected_ids = tuple(item.obligation_id for item in obligations)
    by_id = {item.obligation_id: item for item in obligations}
    assigned: set[str] = set()
    repaired: list[list[str]] = []
    for section in sections:
        ids: list[str] = []
        for raw_id in section.coverage_obligation_ids:
            normalized = _normalize_coverage_id(raw_id, expected_ids)
            if normalized in by_id and normalized not in assigned:
                ids.append(normalized)
                assigned.add(normalized)
        repaired.append(ids)

    def score(index: int, obligation: CoverageObligation) -> tuple[int, int]:
        section = sections[index]
        lowered = f"{section.section_id} {section.heading} {section.reader_question}".casefold()
        evidence = set(section.evidence_handles)
        payload = obligation.payload
        source_ids = set(str(value) for value in _sequence(_value(payload, "source_block_ids", "evidence_handles", default=())))
        score_value = len(evidence & source_ids) * 20
        if obligation.kind == "experiment" and any(value.casefold() in lowered for value in ("experiment", "result", "evaluation", "实验", "结果")):
            score_value += 10
        if obligation.kind == "limitation" and any(value in lowered for value in ("limitation", "discussion", "boundary", "局限", "边界")):
            score_value += 10
        if obligation.kind == "asset":
            asset_id = str(_value(payload, "asset_id", "handle", "block_id", default=""))
            if any(asset_id and asset_id in str(job) for job in section.asset_jobs):
                score_value += 20
            asset_text = f"{_value(payload, 'reason', default='')} {_value(payload, 'explanation_job', default='')}".casefold()
            asset_terms = set(re.findall(r"[a-z][a-z-]{3,}|[\u4e00-\u9fff]{2,}", asset_text))
            section_terms = set(re.findall(r"[a-z][a-z-]{3,}|[\u4e00-\u9fff]{2,}", lowered))
            score_value += min(15, 5 * len(asset_terms & section_terms))
            semantic_routes = (
                (("method", "mechanism", "architecture", "workflow", "方法", "机制", "架构"), ("method", "mechanism", "architecture", "方法", "机制", "架构")),
                (("result", "experiment", "evaluation", "benchmark", "结果", "实验", "评估"), ("result", "experiment", "evaluation", "结果", "实验", "评估")),
            )
            for asset_cues, section_cues in semantic_routes:
                if any(cue in asset_text for cue in asset_cues) and any(cue in lowered for cue in section_cues):
                    score_value += 12
        return score_value, -index

    for obligation in obligations:
        if obligation.obligation_id in assigned:
            continue
        target_index = max(range(len(sections)), key=lambda index: score(index, obligation))
        repaired[target_index].append(obligation.obligation_id)
        assigned.add(obligation.obligation_id)
    return tuple(replace(section, coverage_obligation_ids=tuple(repaired[index])) for index, section in enumerate(sections))


def _coverage_payload(paper_model: PaperModel, asset_plan: AssetPlan, ledger: CoverageLedger | None, conflict: CoverageConflict | None) -> dict[str, Any]:
    return {
        "paper_model": _jsonable(paper_model),
        "section_contracts": _jsonable(paper_model.section_contracts),
        "coverage_ledger": _jsonable(ledger) if ledger is not None else None,
        "coverage_conflict": _jsonable(conflict) if conflict is not None else None,
        "definition_neighborhoods": _jsonable(paper_model.definition_neighborhoods),
        "asset_plan": _jsonable(asset_plan),
        "visual_interpretations": _jsonable(paper_model.visual_interpretations),
        "evidence_allow_list": tuple(sorted({block_id for fact in (*paper_model.source_facts, *paper_model.source_limitations) for block_id in fact.source_block_ids})),
        "unresolved_boundaries": tuple(item.statement for item in paper_model.material_unknowns),
    }


def _receipt_image_usage(
    paper_ir: CanonicalPaperIR,
    decisions: Sequence[VisualDecision],
    interpretations: Sequence[VisualInterpretation],
) -> tuple[int, int]:
    interpreted_ids = {item.visual_block_id for item in interpretations if item.interpretation.strip()}
    used_ids = tuple(dict.fromkeys(
        item.block_id
        for item in decisions
        if item.block_id in interpreted_ids and item.action in {"inspect", "inline"} and item.requires_pixels
    ))
    total_bytes = 0
    for block_id in used_ids:
        block = paper_ir.block_by_id.get(block_id)
        if block is None or not block.image_path:
            continue
        try:
            path = Path(block.image_path)
            if path.is_file():
                total_bytes += path.stat().st_size
        except OSError:
            continue
    return len(used_ids), total_bytes


def _asset_decision_counts(asset_plan: AssetPlan) -> Mapping[str, int]:
    return {
        decision: sum(item.decision == decision for item in asset_plan.decisions)
        for decision in ("inline", "reference", "omit")
    }


def _unexpressed_writer_obligations(
    markdown: str,
    paper_model: PaperModel,
    asset_plan: AssetPlan,
    ledger: CoverageLedger | None,
    satisfied_obligation_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Detect bounded omissions that have deterministic surface evidence.

    Semantic coverage remains the blind-review gate. Here we reject only
    omissions with deterministic surface evidence: an inspected inline figure,
    or a high/medium material unknown with a technical anchor that is never
    stated locally as uncertain. This avoids pretending that ledger assignment
    alone proves that the final prose preserved the evidence boundary.
    """

    if ledger is None:
        return ()
    inline_ids = set(_asset_plan_inline_ids(asset_plan))
    satisfied = set(satisfied_obligation_ids)
    prose = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)

    def owned_prose(obligation_id: str) -> str:
        """Judge numeric/term preservation against the whole note.

        The writer is a model whose section layout varies run to run.  A
        per-marker-section slice can hide preserved experiment content behind an
        arbitrary section boundary, turning a faithful note into a false
        coverage failure.  This deterministic guard only answers "is the
        mandatory numeric/term content preserved somewhere in the note";
        per-section semantic coverage is the blind-review gate's job.
        """

        return prose

    missing: list[str] = []
    visual_interpretations = {
        item.visual_block_id: item.interpretation
        for item in paper_model.visual_interpretations
        if item.interpretation.strip()
    }
    interpreted_ids = set(visual_interpretations)
    neighborhoods = {item.object_block_id: item for item in paper_model.definition_neighborhoods}
    for decision in paper_model.visual_decisions:
        obligation_id = _prefixed_obligation_id("asset", decision.block_id)
        local_prose = owned_prose(obligation_id)
        has_figure_reference = bool(re.search(r"(?:图\s*\d*|图示|架构图|流程图|figure|diagram)", local_prose, flags=re.IGNORECASE))
        neighborhood = neighborhoods.get(decision.block_id)
        if (
            decision.action == "inline"
            and obligation_id in inline_ids
            and ledger.section_for(obligation_id) is not None
            and neighborhood is not None
            and neighborhood.kind == "formula"
            and not _formula_is_expressed(local_prose, neighborhood.object_content or neighborhood.context)
        ):
            missing.append(obligation_id)
            continue
        if (
            decision.action == "inline"
            and decision.requires_pixels
            and obligation_id in inline_ids
            and decision.block_id in interpreted_ids
            and ledger.section_for(obligation_id) is not None
            and not has_figure_reference
            and obligation_id not in satisfied
        ):
            missing.append(obligation_id)
            continue
        required_visual_labels = _visual_named_component_labels(visual_interpretations.get(decision.block_id, ""))
        if (
            decision.action == "inline"
            and obligation_id in inline_ids
            and ledger.section_for(obligation_id) is not None
            and required_visual_labels
            and any(label.casefold() not in local_prose.casefold() for label in required_visual_labels)
        ):
            missing.append(obligation_id)
    for obligation in ledger.obligations:
        if obligation.kind not in {"experiment", "fact", "limitation"}:
            continue
        if obligation.kind == "fact" and not bool(_value(obligation.payload, "eligible", default=True)):
            continue
        numeric_tokens, required_terms = _obligation_preservation_tokens(obligation.payload)
        if not (numeric_tokens or required_terms) or ledger.section_for(obligation.obligation_id) is None:
            continue
        local_prose = owned_prose(obligation.obligation_id)
        normalized_local_prose = re.sub(r"[\s,]+", "", local_prose)
        missing_number = any(re.sub(r"[\s,]+", "", token) not in normalized_local_prose for token in numeric_tokens)
        missing_term = any(term.casefold() not in local_prose.casefold() for term in required_terms)
        if missing_number or missing_term:
            missing.append(obligation.obligation_id)
    for unknown in paper_model.material_unknowns:
        if unknown.priority == "low":
            continue
        anchors = unknown.anchor_terms or _infer_unknown_anchor_terms(unknown.statement)
        obligation_id = f"unknown:{unknown.unknown_id}"
        if obligation_id in satisfied:
            continue
        explicit_named_anchors = tuple(
            anchor
            for anchor in unknown.anchor_terms
            if unknown.anchor_terms_declared and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,}", anchor)
        )
        local_prose = owned_prose(obligation_id)
        missing_named_anchor = any(anchor.casefold() not in local_prose.casefold() for anchor in explicit_named_anchors)
        if missing_named_anchor or (anchors and not _has_local_uncertainty(local_prose, anchors)):
            missing.append(obligation_id)
    return tuple(dict.fromkeys(missing))


def _visual_named_component_labels(interpretation: str) -> tuple[str, ...]:
    roles = {"broker", "router", "retriever", "encoder", "decoder", "orchestrator", "controller", "gateway", "judge", "critic"}
    ignored_qualifiers = {"the", "this", "that", "each", "both"}
    labels: list[str] = []
    for qualifier, role in re.findall(r"\b([A-Z][A-Za-z0-9_.-]*)\s+([A-Za-z][A-Za-z0-9_.-]*)\b", interpretation):
        if qualifier.casefold() not in ignored_qualifiers and role.casefold() in roles:
            labels.append(f"{qualifier} {role}")
    return tuple(dict.fromkeys(labels))


def _obligation_preservation_tokens(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    def strings(value: Any) -> tuple[str, ...]:
        if isinstance(value, Mapping):
            return tuple(text for item in value.values() for text in strings(item))
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return tuple(text for item in value for text in strings(item))
        text = str(value).strip()
        return (text,) if text else ()

    # Only key-content fields drive must-preserve enforcement: a claim's result
    # numbers/terms and the headline statement.  Setup / comparison / boundary /
    # interpretation numbers are experimental configuration, not the paper's
    # conclusion, so they are not forced into the note (the writer may summarize
    # them).
    content_fields = ("statement", "results", "result", "value")
    flattened = " ".join(
        text
        for field in content_fields
        for text in strings(_value(payload, field, default=()))
    )
    numeric_tokens = tuple(dict.fromkeys(re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?", flattened)))
    required_terms = tuple(dict.fromkeys(
        str(term).strip()
        for field in ("named_entities", "mechanism_terms", "required_terms")
        for term in _sequence(_value(payload, field, default=()))
        if str(term).strip()
    ))
    return numeric_tokens, required_terms


def _formula_is_expressed(markdown: str, formula: str) -> bool:
    """Require an inline formula obligation to survive as an actual relation.

    Merely naming symbols in prose is not enough.  The Writer must retain an
    equation-like expression with the formula's left-hand symbol and at least
    one right-hand symbol.  This deliberately checks structure rather than an
    exact LaTeX rendering so equivalent Chinese notes remain valid.
    """

    if "=" not in formula or "=" not in markdown:
        return False

    def tokens(value: str) -> tuple[str, ...]:
        normalized = value.replace("\\overline", "").replace("\\pmb", "").replace("\\mathbf", "")
        normalized = re.sub(r"\\(?:tag|mathrm|mathsf|operatorname)\s*\{[^{}]*\}", " ", normalized)
        normalized = re.sub(r"([A-Za-z])\s*_\s*\{?\s*([A-Za-z0-9-]+)\s*\}?", r"\1_\2", normalized)
        normalized = re.sub(r"[{}$\\]", " ", normalized)
        return tuple(dict.fromkeys(re.findall(r"(?<![A-Za-z_])[A-Za-z](?:_[A-Za-z0-9-]+)?(?![A-Za-z_])", normalized)))

    left, right = formula.split("=", 1)
    left_tokens = tokens(left)
    right_tokens = tokens(right)
    if not left_tokens or not right_tokens:
        return False
    equation_segments = tuple(
        segment
        for segment in re.split(r"[。！？!?\r\n]+", markdown)
        if "=" in segment
    )
    for segment in equation_segments:
        segment_tokens = set(tokens(segment))
        if any(item in segment_tokens for item in left_tokens) and any(item in segment_tokens for item in right_tokens):
            return True
    return False


def _technical_unknown_anchors(statement: str) -> tuple[str, ...]:
    """Return exact technical tokens that can survive Chinese prose safely."""

    anchors: list[str] = []
    anchors.extend(re.findall(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+(?:\([^)]{1,24}\))?", statement))
    anchors.extend(re.findall(r"\b[A-Z](?=\s+(?:term|factor|metric|variable|symbol)\b)", statement))
    anchors.extend(re.findall(r"\b[A-Za-z]+\d[A-Za-z0-9_.-]*(?:\([^)]{1,24}\))?", statement))
    return tuple(dict.fromkeys(anchor.strip() for anchor in anchors if anchor.strip()))


def _infer_unknown_anchor_terms(statement: str) -> tuple[str, ...]:
    """Infer compact translation-stable labels for a material unknown."""

    anchors = list(_technical_unknown_anchors(statement))
    anchors.extend(re.findall(r"\b[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+\b", statement))
    anchors.extend(re.findall(r"\b([A-Za-z][A-Za-z-]{3,})['’]s\s+(?=[A-Za-z-]+)", statement))
    phrase_patterns = (
        r"\b(?:[A-Za-z]+-dimensional)\s+(?:risk|authority|permission)\s+(?:vector|representation|components?)\b",
        r"\b(?:deterministic|runtime|static)\s+(?:verifiers?|audits?|scoring|labels?)\b",
        r"\b(?:reward|risk|authority|permission|task)\s+(?:weights?|vectors?|components?|envelopes?|metrics?|labels?)\b",
    )
    for pattern in phrase_patterns:
        anchors.extend(re.findall(pattern, statement, flags=re.IGNORECASE))
    return tuple(dict.fromkeys(anchor.strip() for anchor in anchors if anchor.strip()))


def _has_local_uncertainty(markdown: str, anchors: Sequence[str]) -> bool:
    uncertainty_cues = (
        "未说明", "没有说明", "未描述", "没有描述", "未给出", "没有给出", "不清楚", "未知", "尚未", "无法确定", "不能确定",
        "未明确", "没有明确", "不完整", "未完整", "未完全", "需确认", "需要确认", "证据不足", "材料不足",
        "not described", "not specified", "unspecified", "unclear", "unknown", "not established",
        "not fully", "missing", "cannot determine", "requires confirmation",
    )
    folded = markdown.casefold()
    for anchor in anchors:
        needle = anchor.casefold()
        start = 0
        while (index := folded.find(needle, start)) >= 0:
            local = folded[max(0, index - 180): index + len(needle) + 180]
            if any(cue in local for cue in uncertainty_cues):
                return True
            start = index + len(needle)
    alias_map = {
        "broker": ("broker", "代理"),
        "deterministic": ("deterministic", "确定性"),
        "verifier": ("verifier", "验证器"),
        "verifiers": ("verifier", "验证器"),
        "six-dimensional": ("six-dimensional", "六维"),
        "risk": ("risk", "风险"),
        "vector": ("vector", "向量"),
        "components": ("component", "分量", "组件"),
        "component": ("component", "分量", "组件"),
        "reward": ("reward", "奖励"),
        "weights": ("weight", "权重"),
        "weight": ("weight", "权重"),
        "authority": ("authority", "权限"),
        "envelope": ("envelope", "包络"),
        "kernel": ("kernel", "内核"),
        "fusion": ("fusion", "融合"),
        "recomputation": ("recomputation", "重计算"),
        "scaling": ("scaling", "扩展", "规模"),
        "engineering": ("engineering", "工程"),
        "challenges": ("challenge", "挑战"),
    }
    ignored = {"the", "a", "an", "of", "for", "and", "or", "exact", "specific", "method"}
    uncertain_segments = tuple(
        segment.casefold()
        for segment in re.split(r"[。！？!?\r\n]+", markdown)
        if any(cue in segment.casefold() for cue in uncertainty_cues)
    )
    for anchor in anchors:
        tokens = [token for token in re.findall(r"[a-z]+(?:-[a-z]+)?", anchor.casefold()) if token not in ignored]
        alias_groups = tuple(alias_map.get(token, (token,)) for token in tokens)
        if alias_groups and any(all(any(alias in segment for alias in group) for group in alias_groups) for segment in uncertain_segments):
            return True
    return False


def _writer_obligation_section_id(
    obligation_id: str,
    paper_model: PaperModel,
    ledger: CoverageLedger | None,
) -> str | None:
    if ledger is not None and (section_id := ledger.section_for(obligation_id)):
        return section_id
    if not obligation_id.startswith("unknown:"):
        return None
    cue_priorities = (
        ("limitation", "boundary", "unknown", "局限", "边界", "未知"),
        ("discussion", "conclusion", "讨论", "结论"),
    )
    for boundary_cues in cue_priorities:
        for section in reversed(paper_model.section_contracts):
            text = f"{section.section_id} {section.heading} {section.reader_question}".casefold()
            if any(cue in text for cue in boundary_cues):
                return section.section_id
    return paper_model.section_contracts[-1].section_id if paper_model.section_contracts else None


def _guard_unsupported_asset_links(markdown: str) -> tuple[str, tuple[str, ...]]:
    """Strip Writer-invented image targets; Renderer owns real asset URLs."""

    rejected: list[str] = []

    def remove(match: re.Match[str]) -> str:
        target = match.group("target").strip()
        rejected.append(target)
        return ""

    cleaned = re.sub(
        r"!\[[^\]]*\]\((?P<target>[^)]+)\)",
        remove,
        markdown,
    )
    return cleaned, tuple(dict.fromkeys(rejected))


def _markdown_section_for_contract(markdown: str, heading: str, ordinal: int, contract_count: int, section_id: str = "") -> str:
    """Return one final-note section without exposing unrelated sections to blind review."""

    if section_id:
        marker = re.search(rf"(?m)^\s*<!--\s*rp-section:{re.escape(section_id)}\s*-->\s*$", markdown)
        if marker is not None:
            following_marker = re.search(r"(?m)^\s*<!--\s*rp-section:[^>]+-->\s*$", markdown[marker.end():])
            end = marker.end() + following_marker.start() if following_marker else len(markdown)
            return markdown[marker.end():end].strip()
    heading_matches = tuple(re.finditer(r"(?m)^(?P<marks>#{1,6})\s+(?P<title>[^\r\n]+?)\s*$", markdown))
    if not heading_matches:
        return markdown
    normalized_heading = re.sub(r"\W+", "", heading, flags=re.UNICODE).casefold()
    selected_index: int | None = None
    for index, match in enumerate(heading_matches):
        normalized_title = re.sub(r"\W+", "", match.group("title"), flags=re.UNICODE).casefold()
        if normalized_title == normalized_heading or (
            normalized_heading and normalized_title
            and (normalized_heading in normalized_title or normalized_title in normalized_heading)
        ):
            selected_index = index
            break
    if selected_index is None:
        candidates = list(range(len(heading_matches)))
        if len(candidates) > contract_count and heading_matches[0].group("marks") == "#":
            candidates = candidates[1:]
        selected_index = candidates[min(ordinal, len(candidates) - 1)]
    selected = heading_matches[selected_index]
    level = len(selected.group("marks"))
    end = len(markdown)
    for following in heading_matches[selected_index + 1:]:
        if len(following.group("marks")) <= level:
            end = following.start()
            break
    return markdown[selected.start():end].strip()


def _markdown_heading_for_contract(markdown: str, heading: str, ordinal: int, contract_count: int, section_id: str = "") -> str:
    section = _markdown_section_for_contract(markdown, heading, ordinal, contract_count, section_id)
    match = re.match(r"(?P<heading>#{1,6}\s+[^\r\n]+)", section)
    return match.group("heading").strip() if match else ""


def _strip_section_markers(markdown: str) -> str:
    return re.sub(r"(?m)^\s*<!--\s*rp-section:[^>]+-->\s*\r?\n?", "", markdown)


def _apply_writer_section_patches(
    markdown: str,
    raw_patches: Any,
    allowed_obligation_ids: Sequence[str],
    *,
    section_headings: Mapping[str, str] | None = None,
    obligation_sections: Mapping[str, str | None] | None = None,
) -> tuple[str | None, tuple[str, ...]]:
    """Merge bounded repair prose under exact existing Markdown headings."""

    patched = markdown
    applied = 0
    applied_ids: list[str] = []
    allowed = set(allowed_obligation_ids)
    resolved_headings = section_headings or {}
    resolved_obligation_sections = obligation_sections or {}
    for item in _sequence(raw_patches):
        if not isinstance(item, Mapping):
            continue
        heading = str(item.get("after_heading", "")).strip()
        addition = str(item.get("append_markdown", "")).strip()
        obligation_ids = tuple(
            str(value)
            for value in _sequence(item.get("obligation_ids", ()))
            if str(value) in allowed
        )
        section_id = str(item.get("section_id", "")).strip()
        if section_id in resolved_headings:
            heading = resolved_headings[section_id]
        elif not re.search(rf"(?m)^{re.escape(heading)}\s*$", patched):
            inferred_sections = {
                resolved_obligation_sections.get(obligation_id)
                for obligation_id in obligation_ids
                if resolved_obligation_sections.get(obligation_id)
            }
            if len(inferred_sections) == 1:
                inferred_section = next(iter(inferred_sections))
                heading = resolved_headings.get(str(inferred_section), heading)
        if not heading.startswith("#") or not addition:
            continue
        heading_match = re.search(rf"(?m)^{re.escape(heading)}\s*$", patched)
        if heading_match is None:
            continue
        tail = patched[heading_match.end():]
        next_heading = re.search(r"(?m)^#{1,6}\s+", tail)
        section_end = next_heading.start() if next_heading else len(tail)
        section_body = tail[:section_end]
        following = tail[section_end:]
        merged_body = _merge_repair_paragraphs(section_body, addition)
        patched = (
            patched[:heading_match.end()].rstrip()
            + "\n\n"
            + merged_body.strip()
            + ("\n\n" if following else "\n")
            + following.lstrip("\r\n")
        )
        applied += 1
        for obligation_id in obligation_ids:
            if obligation_id.startswith("unknown:") and not re.search(
                r"(?:未|没有|未知|不清楚|无法|不能|尚未|不完全|not\s|unknown|unclear|unspecified|missing|cannot)",
                addition,
                flags=re.IGNORECASE,
            ):
                continue
            if obligation_id.startswith("asset:") and not re.search(
                r"(?:图\s*\d*|图示|架构图|流程图|figure|diagram)",
                addition,
                flags=re.IGNORECASE,
            ):
                continue
            applied_ids.append(obligation_id)
    return (patched if applied else None), tuple(dict.fromkeys(applied_ids))


def _merge_repair_paragraphs(section_body: str, addition: str) -> str:
    """Replace a shorter numeric-overlap paragraph; append genuinely new prose."""

    merged = section_body.strip()
    additions = tuple(part.strip() for part in re.split(r"\n\s*\n", addition) if part.strip())
    for new_paragraph in additions:
        visual_match = ""
        if _has_visual_reference(new_paragraph):
            visual_match = next(
                (
                    existing
                    for existing in (part.strip() for part in re.split(r"\n\s*\n", merged) if part.strip())
                    if _has_visual_reference(existing)
                ),
                "",
            )
        if visual_match:
            detail = re.sub(
                r"^\s*(?:图\s*\d+|figure\s*\d+)(?:\s*中的|\s*显示|\s*shows?)?\s*",
                "其中 ",
                new_paragraph,
                count=1,
                flags=re.IGNORECASE,
            )
            consolidated = visual_match.rstrip("。！？;； ") + "；" + detail.lstrip()
            merged = merged.replace(visual_match, consolidated, 1)
            continue
        new_numbers = _paragraph_numeric_fingerprint(new_paragraph)
        best_match = ""
        best_score = (0, 0.0)
        if len(new_numbers) >= 2:
            for existing in (part.strip() for part in re.split(r"\n\s*\n", merged) if part.strip()):
                existing_numbers = _paragraph_numeric_fingerprint(existing)
                shared = len(new_numbers & existing_numbers)
                overlap = shared / min(len(new_numbers), len(existing_numbers)) if existing_numbers else 0.0
                # Numeric overlap alone is not identity: separate experiments
                # often share task/episode counts.  Replacement is safe only
                # when the new repair actually contains every numeric anchor
                # from the shorter existing paragraph.
                subsumes_existing = bool(existing_numbers) and existing_numbers <= new_numbers
                if shared >= 2 and overlap >= 0.6 and subsumes_existing and (shared, overlap) > best_score:
                    best_match = existing
                    best_score = (shared, overlap)
        if best_match:
            merged = merged.replace(best_match, new_paragraph, 1)
        else:
            merged = (merged.rstrip() + "\n\n" + new_paragraph).strip()
    return merged


def _has_visual_reference(value: str) -> bool:
    return bool(re.search(r"(?:图\s*\d+|图示|架构图|流程图|figure\s*\d+|diagram)", value, flags=re.IGNORECASE))


def _paragraph_numeric_fingerprint(value: str) -> set[str]:
    return {
        re.sub(r"[\s,]+", "", token)
        for token in re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?", value)
    }


def _remove_cross_section_numeric_repetition(
    markdown: str,
    paper_model: PaperModel,
    ledger: CoverageLedger | None,
) -> str:
    """Remove only duplicated numeric prose outside its ledger-owned section."""

    if ledger is None or len(paper_model.section_contracts) < 2:
        return markdown
    if not all(
        re.search(rf"(?m)^\s*<!--\s*rp-section:{re.escape(contract.section_id)}\s*-->\s*$", markdown)
        for contract in paper_model.section_contracts
    ):
        return markdown
    section_entries: dict[str, tuple[int, str, tuple[tuple[int, int, str], ...]]] = {}
    for ordinal, contract in enumerate(paper_model.section_contracts):
        section_text = _markdown_section_for_contract(markdown, contract.heading, ordinal, len(paper_model.section_contracts), contract.section_id)
        section_start = markdown.find(section_text)
        if section_start < 0:
            continue
        paragraphs: list[tuple[int, int, str]] = []
        for match in re.finditer(r"(?ms)(?:^|\n\s*\n)(?P<paragraph>(?!#{1,6}\s).+?)(?=\n\s*\n|\Z)", section_text):
            paragraph = match.group("paragraph").strip()
            if paragraph:
                start = section_start + match.start("paragraph")
                paragraphs.append((start, start + len(match.group("paragraph")), paragraph))
        section_entries[contract.section_id] = (section_start, section_text, tuple(paragraphs))
    removals: set[tuple[int, int]] = set()
    for obligation in ledger.obligations:
        owner = ledger.section_for(obligation.obligation_id)
        if owner not in section_entries or obligation.kind not in {"experiment", "fact", "limitation"}:
            continue
        numeric_tokens, _ = _obligation_preservation_tokens(obligation.payload)
        obligation_numbers = {re.sub(r"[\s,]+", "", token) for token in numeric_tokens}
        if len(obligation_numbers) < 2:
            continue
        owner_paragraphs = {
            re.sub(r"\W+", "", paragraph, flags=re.UNICODE).casefold()
            for _, _, paragraph in section_entries[owner][2]
            if len(_paragraph_numeric_fingerprint(paragraph) & obligation_numbers) >= 2
        }
        if not owner_paragraphs:
            continue
        for section_id, (_, _, paragraphs) in section_entries.items():
            if section_id == owner:
                continue
            for start, end, paragraph in paragraphs:
                paragraph_numbers = _paragraph_numeric_fingerprint(paragraph)
                normalized_paragraph = re.sub(r"\W+", "", paragraph, flags=re.UNICODE).casefold()
                if len(paragraph_numbers & obligation_numbers) >= 2 and normalized_paragraph in owner_paragraphs:
                    removals.add((start, end))
    cleaned = markdown
    for start, end in sorted(removals, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def _writer_repair_evidence(
    obligation_ids: Sequence[str],
    paper_model: PaperModel,
    asset_plan: AssetPlan,
    ledger: CoverageLedger | None,
) -> tuple[Mapping[str, Any], ...]:
    asset_by_id = {item.asset_id: item for item in asset_plan.decisions}
    visual_by_id = {item.visual_block_id: item.interpretation for item in paper_model.visual_interpretations}
    unknown_by_id = {f"unknown:{item.unknown_id}": item for item in paper_model.material_unknowns}
    fact_by_id = {
        _prefixed_obligation_id("fact", _value(item, "fact_id", "id", default="")): item
        for item in paper_model.must_preserve_facts
        if bool(_value(item, "eligible", default=True))
    }
    ledger_obligations = {item.obligation_id: item for item in ledger.obligations} if ledger is not None else {}
    neighborhoods = {item.object_block_id: item for item in paper_model.definition_neighborhoods}
    evidence: list[Mapping[str, Any]] = []
    for obligation_id in obligation_ids:
        unknown = unknown_by_id.get(obligation_id)
        if unknown is not None:
            evidence.append({
                "obligation_id": obligation_id,
                "section_id": _writer_obligation_section_id(obligation_id, paper_model, ledger),
                "kind": "material_unknown",
                "statement": unknown.statement,
                "priority": unknown.priority,
                "anchor_terms": unknown.anchor_terms or _infer_unknown_anchor_terms(unknown.statement),
            })
            continue
        fact = fact_by_id.get(obligation_id)
        if fact is not None:
            statement = str(_value(fact, "statement", default="")).strip()
            required_terms = tuple(dict.fromkeys(
                str(term).strip()
                for field in ("named_entities", "mechanism_terms", "required_terms")
                for term in _sequence(_value(fact, field, default=()))
                if str(term).strip()
            ))
            evidence.append({
                "obligation_id": obligation_id,
                "section_id": ledger.section_for(obligation_id) if ledger is not None else None,
                "kind": "must_preserve_fact",
                "statement": statement,
                "exact_numeric_tokens": tuple(dict.fromkeys(re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?", statement))),
                "required_terms": required_terms,
                "source_block_ids": _sequence(_value(fact, "source_block_ids", default=())),
            })
            continue
        ledger_obligation = ledger_obligations.get(obligation_id)
        if ledger_obligation is not None and ledger_obligation.kind in {"argument", "experiment", "limitation"}:
            numeric_tokens, required_terms = _obligation_preservation_tokens(ledger_obligation.payload)
            evidence.append({
                "obligation_id": obligation_id,
                "section_id": ledger.section_for(obligation_id) if ledger is not None else None,
                "kind": ledger_obligation.kind,
                "payload": ledger_obligation.payload,
                "exact_numeric_tokens": numeric_tokens,
                "required_terms": required_terms,
            })
            continue
        asset = asset_by_id.get(obligation_id)
        if asset is None:
            continue
        block_id = obligation_id.removeprefix("asset:")
        neighborhood = neighborhoods.get(block_id)
        if neighborhood is not None and neighborhood.kind == "formula":
            evidence.append({
                "obligation_id": obligation_id,
                "section_id": ledger.section_for(obligation_id) if ledger is not None else None,
                "kind": "inline_formula",
                "explanation_job": asset.explanation_job,
                "formula": neighborhood.object_content or neighborhood.context,
                "defined_symbols": neighborhood.defined_symbols,
                "definition_context": neighborhood.context,
            })
            continue
        evidence.append({
            "obligation_id": obligation_id,
            "section_id": ledger.section_for(obligation_id) if ledger is not None else None,
            "kind": "inline_visual",
            "explanation_job": asset.explanation_job,
            "visual_interpretation": visual_by_id.get(block_id, ""),
            "required_labels": _visual_named_component_labels(visual_by_id.get(block_id, "")),
        })
    return tuple(evidence)


def _definition_neighborhoods(response: Mapping[str, Any], paper_ir: CanonicalPaperIR) -> tuple[DefinitionNeighborhood, ...]:
    selected_ids: set[str] = set()
    for item in response.get("visual_candidates", ()):
        if not isinstance(item, Mapping) or str(item.get("decision", "reference")) == "omit":
            continue
        for block in _resolve_available_asset_blocks(str(item.get("block_id", "")), paper_ir):
            if block.kind in {"formula", "table"} and block.parse_status == "available":
                selected_ids.add(block.block_id)
    for section in _sequence(response.get("sections", response.get("section_contracts", ()))):
        if not isinstance(section, Mapping):
            continue
        for job in _sequence(section.get("asset_jobs", ())):
            if not isinstance(job, Mapping):
                continue
            raw_id = str(job.get("asset_id", job.get("block_id", "")))
            for block in _resolve_available_asset_blocks(raw_id, paper_ir):
                if block.kind in {"formula", "table"} and block.parse_status == "available":
                    selected_ids.add(block.block_id)
    # A source-backed fact that directly depends on a formula or table also
    # needs its local definition neighborhood, even when the provider omitted
    # a separate visual-candidate entry.  This is a semantic allow-list, not
    # an instruction to inline the asset or to expand the note.
    for group in ("source_facts", "source_limitations", "must_preserve_facts"):
        for item in response.get(group, ()):
            if not isinstance(item, Mapping):
                continue
            raw_ids = item.get("source_block_ids", item.get("source_block_id", ()))
            if isinstance(raw_ids, str):
                raw_ids = (raw_ids,)
            for block_id in _sequence(raw_ids):
                for block in _resolve_available_asset_blocks(str(block_id), paper_ir):
                    if block.kind in {"formula", "table"} and block.parse_status == "available":
                        selected_ids.add(block.block_id)
    ordered = list(paper_ir.ordered_blocks)
    neighborhoods: list[DefinitionNeighborhood] = []
    for index, block in enumerate(ordered):
        if block.block_id not in selected_ids or block.kind not in {"formula", "table"}:
            continue
        neighbors: list[PaperIRBlock] = []
        for candidate in reversed(ordered[:index]):
            if candidate.section != block.section:
                continue
            if candidate.kind in {"caption", "footnote"}:
                neighbors.insert(0, candidate)
                continue
            if candidate.kind in {"paragraph", "text"}:
                neighbors.insert(0, candidate)
                break
            if candidate.kind in {"formula", "table", "figure", "image", "chart"}:
                continue
        neighbors.append(block)
        for candidate in ordered[index + 1:]:
            if candidate.section != block.section:
                continue
            if candidate.kind in {"caption", "footnote"}:
                neighbors.append(candidate)
                continue
            if candidate.kind in {"paragraph", "text"}:
                neighbors.append(candidate)
                break
            if candidate.kind in {"formula", "table", "figure", "image", "chart"}:
                continue
        context = "\n\n".join(
            value
            for item in neighbors
            for value in (item.caption or "", item.latex or item.table_html or item.text)
            if value
        )
        lhs_symbols = _formula_lhs_symbols(block.latex or block.text, context) if block.kind == "formula" else ()
        defined_symbols = tuple(dict.fromkeys((*_defined_formula_symbols(context), *lhs_symbols)))
        neighborhoods.append(DefinitionNeighborhood(
            object_block_id=block.block_id,
            kind=block.kind,
            source_block_ids=tuple(item.block_id for item in neighbors),
            context=context,
            object_content=block.latex or block.table_html or block.text,
            defined_symbols=defined_symbols,
            formula_symbols=_formula_symbols(block.latex or block.text) if block.kind == "formula" else (),
            table_rows=_table_rows(block) if block.kind == "table" else (),
        ))
    return tuple(neighborhoods)


def _defined_formula_symbols(context: str) -> tuple[str, ...]:
    normalized = _normalize_formula_subscripts(context)
    patterns = (
        r"(?:Here\s+)?([A-Z](?:_[A-Za-z]+)?)\s+(?:is|are|combines|penalizes|rewards|measures|marks|denotes|represents|captures|indicates)\b",
        r"(?:Let\s+|,\s*|and\s+)([A-Z](?:_[A-Za-z]+)?)\s+(?:denote\s+)?(?=[a-z])",
        r"([A-Z](?:_[A-Za-z]+)?)\s*(?:是|表示|代表|衡量|惩罚|奖励|标记)",
    )
    found = [match for pattern in patterns for match in re.findall(pattern, normalized)]
    for first, second in re.findall(
        r"\b([A-Z](?:_[A-Za-z]+)?)\s+and\s+([A-Z](?:_[A-Za-z]+)?)\s+(?:that\s+)?(?:represent|denote|measure|mark|capture|indicate)\b",
        normalized,
    ):
        found.extend((first, second))
    for group in re.findall(
        r"(?:parameters?|参数)\s*(?:are|is|为|包括|包含|with|:)?\s*\(([^)]{1,120})\)",
        normalized,
        flags=re.IGNORECASE,
    ):
        found.extend(re.findall(r"(?<![A-Za-z0-9_])([A-Z](?:_[A-Za-z]+)?)(?![A-Za-z0-9_])", group))
    for group in re.findall(
        r"\(([^)]{1,120})\)\s*(?:parameters?|参数)",
        normalized,
        flags=re.IGNORECASE,
    ):
        found.extend(re.findall(r"(?<![A-Za-z0-9_])([A-Z](?:_[A-Za-z]+)?)(?![A-Za-z0-9_])", group))
    return tuple(dict.fromkeys(found))


def _formula_lhs_symbols(formula: str, context: str) -> tuple[str, ...]:
    if not re.search(r"(?:is\s+(?:defined\s+as|given\s+by)|定义为|给出如下)", context, flags=re.IGNORECASE):
        return ()
    normalized = _normalize_formula_subscripts(formula)
    match = re.search(r"\b([A-Z](?:_[A-Za-z]+)?)\s*=", normalized)
    return (match.group(1),) if match else ()


def _formula_symbols(formula: str) -> tuple[str, ...]:
    normalized = _normalize_formula_subscripts(formula)
    normalized = re.sub(r"\\(?:mathrm|operatorname\*?|mathbb|mathbf|bf)\s*\{[^{}]*\}", "", normalized)
    return tuple(dict.fromkeys(re.findall(r"[A-Z](?:_[A-Za-z]+)?", normalized)))


def _normalize_formula_subscripts(value: str) -> str:
    normalized = re.sub(r"([A-Z])\s*_\s*\{\s*\\?([A-Za-z]+)\s*\}", r"\1_\2", value)
    return re.sub(r"([A-Z])\s*_\s*\\([A-Za-z]+)", r"\1_\2", normalized)


def _guard_unsupported_symbol_definitions(markdown: str, paper_model: PaperModel) -> tuple[str, tuple[str, ...]]:
    formula_symbols = {symbol for neighborhood in paper_model.definition_neighborhoods for symbol in neighborhood.formula_symbols}
    defined_symbols = {symbol for neighborhood in paper_model.definition_neighborhoods for symbol in neighborhood.defined_symbols}
    defined_symbols.update(_defined_formula_symbols("\n".join(fact.statement for fact in paper_model.source_facts)))
    unsupported = formula_symbols - defined_symbols
    # Index/horizon variables used only as formula bounds are structural
    # notation, not an author-defined metric.  They may remain in an equation
    # without a prose definition; an invented semantic explanation is still
    # rejected below when the Writer assigns one.
    for neighborhood in paper_model.definition_neighborhoods:
        context = neighborhood.context
        for symbol in tuple(unsupported):
            if re.search(rf"\b[a-z]\s*=\s*(?:0|1)\s*(?:,|\\ldots|\.{{2}}|…)+[^\n]*\b{re.escape(symbol)}\b", context, flags=re.IGNORECASE):
                unsupported.discard(symbol)
    if not unsupported:
        return markdown, ()
    definition_marker = re.compile(r"(?:表示|代表|是|为|意味着|衡量|惩罚|奖励|标记|denotes|represents|means|measures|penalizes|rewards|marks)", re.IGNORECASE)
    rejected: list[str] = []
    output_lines: list[str] = []
    for line in markdown.splitlines(keepends=True):
        kept: list[str] = []
        for sentence in re.split(r"(?<=[。！？!?])", line):
            normalized_sentence = _normalize_formula_subscripts(sentence)
            symbol = next((
                value
                for value in unsupported
                if re.search(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])", normalized_sentence)
                and definition_marker.search(sentence)
            ), None)
            if symbol is None:
                kept.append(sentence)
            elif symbol not in rejected:
                rejected.append(symbol)
        output_lines.append("".join(kept))
    return "".join(output_lines), tuple(rejected)


def _table_rows(block: PaperIRBlock) -> tuple[tuple[str, ...], ...]:
    rows: list[tuple[str, ...]] = []
    for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", block.table_html or "", flags=re.IGNORECASE | re.DOTALL):
        cells = tuple(
            re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", " ", raw_cell))).strip()
            for raw_cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", raw_row, flags=re.IGNORECASE | re.DOTALL)
        )
        if cells:
            rows.append(cells)
    if rows:
        return tuple(rows)
    for line in block.text.splitlines():
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        if len(cells) >= 2 and not all(set(cell) <= {"-", ":", " "} for cell in cells):
            rows.append(cells)
    return tuple(rows)


def _guard_unsupported_table_claims(markdown: str, paper_model: PaperModel) -> tuple[str, tuple[str, ...]]:
    table_neighborhoods = tuple(item for item in paper_model.definition_neighborhoods if item.kind == "table" and item.table_rows)
    if not table_neighborhoods:
        return markdown, ()
    rejected: list[str] = []
    output_lines: list[str] = []
    for line in markdown.splitlines(keepends=True):
        kept: list[str] = []
        for sentence in re.split(r"(?<=[。！？!?])", line):
            bad_table: str | None = None
            lowered = sentence.casefold()
            for neighborhood in table_neighborhoods:
                sentence_percentages = set(re.findall(r"\d[\d,]*(?:\.\d+)?%", sentence))
                metric_headers = tuple(
                    cell.strip().casefold()
                    for cell in neighborhood.table_rows[0][1:]
                    if cell.strip()
                )
                has_metric_claim = any(header in lowered for header in metric_headers) or bool(re.search(
                    r"(?:安全成功率?|任务成功率?|成功率|准确率|精确匹配|过度权限|超额权限|越权率|safe\s+success|accuracy|exact\s+match|over[- ]?privilege|excess[- ]?authority)",
                    sentence,
                    flags=re.IGNORECASE,
                ))
                if len(sentence_percentages) < 2 and not has_metric_claim:
                    continue
                labels = [row[0].strip() for row in neighborhood.table_rows if len(row) >= 2 and row[0].strip()]
                matches = sorted(
                    (lowered.find(label.casefold()), label, row)
                    for label, row in ((row[0].strip(), row) for row in neighborhood.table_rows if len(row) >= 2 and row[0].strip())
                    if lowered.find(label.casefold()) >= 0
                )
                for match_index, (position, _label, row) in enumerate(matches):
                    end = matches[match_index + 1][0] if match_index + 1 < len(matches) else len(sentence)
                    segment = sentence[position:end]
                    claimed = set(re.findall(r"\d[\d,]*(?:\.\d+)?%", segment))
                    allowed = set(re.findall(r"\d[\d,]*(?:\.\d+)?%", " ".join(row[1:])))
                    if claimed and not claimed.issubset(allowed):
                        bad_table = neighborhood.object_block_id
                        break
                if bad_table:
                    break
            if bad_table is None:
                kept.append(sentence)
            elif bad_table not in rejected:
                rejected.append(bad_table)
        output_lines.append("".join(kept))
    return "".join(output_lines), tuple(rejected)


def _numeric_claim_supported(statement: str, source_block_ids: Sequence[str], block_map: Mapping[str, PaperIRBlock]) -> bool:
    numbers = re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?", statement)
    if not numbers:
        return True
    source = " ".join(
        " ".join(filter(None, (block_map[block_id].text, block_map[block_id].caption or "", block_map[block_id].latex or "", block_map[block_id].table_html or "")))
        for block_id in source_block_ids
        if block_id in block_map
    )
    normalized_source = re.sub(r"[\s,]+", "", source)
    return all(re.sub(r"[\s,]+", "", token) in normalized_source for token in numbers)


def _recover_exact_fact_source(
    statement: str,
    source_block_ids: Sequence[str],
    paper_ir: CanonicalPaperIR,
) -> tuple[str, ...]:
    """Recover an invalid provider ID only from one uniquely exact source block."""

    ids = tuple(dict.fromkeys(source_block_ids))
    if ids or not statement.strip():
        return ids
    stopwords = {
        "about", "after", "also", "before", "from", "into", "than", "that", "the", "their",
        "this", "through", "uses", "using", "with", "and", "for", "one", "training",
    }

    def terms(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z][a-z0-9_.-]{2,}", value.casefold())
            if token not in stopwords
        }

    wanted = terms(statement)
    if len(wanted) < 3:
        return ids
    candidates: list[tuple[float, PaperIRBlock]] = []
    for block in paper_ir.ordered_blocks:
        if block.kind in {"heading", "caption"} or not _numeric_claim_supported(statement, (block.block_id,), paper_ir.block_by_id):
            continue
        overlap = len(wanted & terms(" ".join(filter(None, (block.text, block.caption or "", block.latex or "", block.table_html or ""))))) / len(wanted)
        if overlap >= 0.55:
            candidates.append((overlap, block))
    candidates.sort(key=lambda item: (-item[0], item[1].order))
    if not candidates:
        return ids
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return ids
    return (candidates[0][1].block_id,)


def _complete_numeric_evidence(
    statement: str,
    source_block_ids: Sequence[str],
    paper_ir: CanonicalPaperIR,
    *,
    max_distance: int = 12,
    max_added_blocks: int = 3,
) -> tuple[str, ...]:
    """Attach the smallest nearby prose needed to support a composite numeric fact.

    Tables often omit a denominator that the surrounding evaluation prose states.
    We keep the exact-number gate unchanged and only add nearby, durable blocks whose
    literal numeric tokens close that gap.
    """

    ids = tuple(dict.fromkeys(source_block_ids))
    block_map = paper_ir.block_by_id
    if not ids or _numeric_claim_supported(statement, ids, block_map):
        return ids
    numbers = tuple(dict.fromkeys(re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?%?", statement)))

    def normalized_block(block: PaperIRBlock) -> str:
        value = " ".join(filter(None, (block.text, block.caption or "", block.latex or "", block.table_html or "")))
        return re.sub(r"[\s,]+", "", value)

    def missing(current_ids: Sequence[str]) -> set[str]:
        source = "".join(normalized_block(block_map[block_id]) for block_id in current_ids if block_id in block_map)
        return {token for token in numbers if re.sub(r"[\s,]+", "", token) not in source}

    ordered = list(paper_ir.ordered_blocks)
    positions = {block.block_id: index for index, block in enumerate(ordered)}
    anchors = [positions[block_id] for block_id in ids if block_id in positions]
    if not anchors:
        return ids
    candidates = [
        block
        for block in ordered
        if block.block_id not in ids
        and block.kind in {"paragraph", "text", "caption", "table"}
        and min(abs(positions[block.block_id] - anchor) for anchor in anchors) <= max_distance
    ]
    candidates.sort(key=lambda block: min(abs(positions[block.block_id] - anchor) for anchor in anchors))
    completed = list(ids)
    for _ in range(max_added_blocks):
        before = missing(completed)
        if not before:
            break
        ranked = sorted(
            ((len(before - missing((*completed, block.block_id))), index, block) for index, block in enumerate(candidates)),
            key=lambda item: (-item[0], item[1]),
        )
        if not ranked or ranked[0][0] <= 0:
            break
        chosen = ranked[0][2]
        completed.append(chosen.block_id)
        candidates.remove(chosen)
    return tuple(completed) if _numeric_claim_supported(statement, completed, block_map) else ids


def _facet_for_section(section: str) -> Facet:
    value = section.casefold()
    if any(token in value for token in ("experiment", "evaluation", "result", "benchmark", "task and evaluation", "实验", "结果")): return "experiment"
    if any(token in value for token in ("method", "methodology", "approach", "architecture", "algorithm", "pipeline", "workflow", "system design", "execution", "training", "framework", "方法", "架构", "算法", "流程")): return "method"
    if any(token in value for token in ("limitation", "threat", "future", "局限")): return "limitation"
    return "problem"


def _facet_for_question(text: str) -> Facet:
    value = text.casefold()
    if any(token in value for token in ("实验", "验证", "结果", "评估", "指标", "experiment", "evaluation", "validate", "evidence", "result", "metric", "benchmark")):
        return "experiment"
    if any(token in value for token in ("机制", "方法", "如何", "控制流", "架构", "算法", "流程", "method", "mechanism", "implement", "architecture", "algorithm", "pipeline", "workflow")):
        return "method"
    if any(token in value for token in ("局限", "边界", "限制", "limitation", "limit")):
        return "limitation"
    return "problem"


def _eligible_reader_fact(block: PaperIRBlock) -> bool:
    """Keep title/byline/parser debris out of reading-state source facts."""

    if block.is_visual or block.kind in {"heading", "caption"} or not block.is_complete_text:
        return False
    text = re.sub(r"<[^>]+>", " ", block.text).strip()
    if len(text) < 40 or text in {"∗", "*"}:
        return False
    section = block.section.casefold()
    if any(marker in section for marker in ("title", "author", "affiliation")) and len(text) < 160:
        return False
    return True


def _candidate_bundle_block(block: PaperIRBlock) -> bool:
    if block.kind in {"heading", "caption"} or not block.text.strip() or len(block.text.strip()) < 20:
        return False
    if "task-conditioned" in block.section.casefold() and len(block.text.strip()) < 200:
        return False
    return True


def _neighbor_text(blocks: Sequence[PaperIRBlock], index: int) -> str:
    return " ".join(block.text for block in blocks[max(0, index - 1): index + 2])


def _strip_json_fence(value: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", value.strip(), flags=re.I)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, Mapping): return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)): return [_jsonable(item) for item in value]
    return value


def _dynamic_writer_constraint_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Build writer constraints from the validated reading state.

    This is deliberately data-driven: the adapter receives no paper-specific
    preservation vocabulary.  The source-backed facts, experiment records,
    definition neighborhoods, asset decisions, and conclusion boundaries are
    the only allow-list inputs for the planning and writing calls.
    """

    paper_model = value.get("paper_model", {})
    if not isinstance(paper_model, Mapping):
        paper_model = {}

    preservation = value.get("must_preserve_facts", {})
    if not isinstance(preservation, Mapping):
        preservation = {}
    facts = _sequence(preservation.get("facts"))
    if not facts:
        facts = _sequence(paper_model.get("must_preserve_facts"))

    exact_numeric_tokens: set[str] = set()
    named_entities: set[str] = set()
    mechanism_terms: set[str] = set()
    normalized_facts: list[dict[str, Any]] = []
    for item in facts:
        if not isinstance(item, Mapping) or not bool(item.get("eligible", True)):
            continue
        statement = str(item.get("statement", "")).strip()
        if not statement:
            continue
        numeric_tokens = tuple(str(token) for token in _sequence(item.get("numeric_tokens")) if str(token).strip())
        if not numeric_tokens:
            numeric_tokens = tuple(dict.fromkeys(re.findall(r"\d[\d,]*(?:\.\d+)?%?", statement)))
        entity_tokens = tuple(str(token) for token in _sequence(item.get("named_entities")) if str(token).strip())
        term_tokens = tuple(str(token) for token in _sequence(item.get("mechanism_terms")) if str(token).strip())
        exact_numeric_tokens.update(numeric_tokens)
        named_entities.update(entity_tokens)
        mechanism_terms.update(term_tokens)
        normalized_facts.append({
            "fact_id": str(item.get("fact_id", item.get("id", ""))),
            "facet": str(item.get("facet", "")),
            "statement": statement,
            "numeric_tokens": numeric_tokens,
            "named_entities": entity_tokens,
            "mechanism_terms": term_tokens,
            "source_block_ids": tuple(str(token) for token in _sequence(item.get("source_block_ids")) if str(token).strip()),
        })

    exact_numeric_tokens.update(str(token) for group in _sequence(preservation.get("numeric_groups")) for token in _sequence(group) if str(token).strip())
    named_entities.update(str(token) for token in _sequence(preservation.get("named_entities")) if str(token).strip())
    mechanism_terms.update(str(token) for token in _sequence(preservation.get("mechanism_terms")) if str(token).strip())

    experiments: list[dict[str, Any]] = []
    for item in _sequence(paper_model.get("experiments")):
        if not isinstance(item, Mapping):
            continue
        experiments.append({
            "experiment_id": str(item.get("experiment_id", item.get("id", ""))),
            "setup": _sequence(item.get("setup")),
            "comparison": _sequence(item.get("comparison")),
            "results": _sequence(item.get("results")),
            "interpretation": str(item.get("interpretation", "")),
            "boundary": str(item.get("boundary", "")),
            "source_block_ids": _sequence(item.get("source_block_ids")),
        })

    argument_chain = paper_model.get("argument_chain", {})
    if not isinstance(argument_chain, Mapping):
        argument_chain = {}
    raw_mechanisms = argument_chain.get("mechanisms")
    parsed_mechanisms: Sequence[Any] = _sequence(raw_mechanisms)
    if isinstance(raw_mechanisms, str) and raw_mechanisms.lstrip().startswith("["):
        try:
            parsed_mechanisms = ast.literal_eval(raw_mechanisms)
        except (SyntaxError, ValueError):
            parsed_mechanisms = ()
    for item in _sequence(parsed_mechanisms):
        statement = re.sub(r"\s+", " ", str(item)).strip()
        if not statement:
            continue
        label = statement.split(":", 1)[0] if ":" in statement else re.split(
            r"\b(?:with|using|where|that|which|and)\b",
            statement,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        label = label.strip(" ,:;")
        if label:
            mechanism_terms.add(label)
    conclusion_boundaries = [str(argument_chain.get("conclusion_scope", "")).strip()]
    conclusion_boundaries.extend(
        str(item.get("statement", "")).strip()
        for item in _sequence(paper_model.get("limitations"))
        if isinstance(item, Mapping) and str(item.get("statement", "")).strip()
    )
    conclusion_boundaries.extend(
        str(item).strip()
        for item in _sequence(value.get("unresolved_boundaries"))
        if str(item).strip()
    )

    return {
        "must_preserve_facts": normalized_facts,
        "exact_numeric_tokens": tuple(sorted(exact_numeric_tokens)),
        "named_entities": tuple(sorted(named_entities)),
        "named_mechanism_terms": tuple(sorted(mechanism_terms)),
        "experiment_evidence": experiments,
        "conclusion_boundaries": tuple(dict.fromkeys(item for item in conclusion_boundaries if item)),
        "formula_evidence": tuple(
            {
                "object_block_id": str(item.get("object_block_id", "")),
                "formula": str(item.get("object_content", "")),
                "definition_context": str(item.get("context", "")),
                "defined_symbols": _sequence(item.get("defined_symbols")),
                "formula_symbols": _sequence(item.get("formula_symbols")),
            }
            for item in _sequence(paper_model.get("definition_neighborhoods", value.get("definition_neighborhoods", ())))
            if isinstance(item, Mapping) and str(item.get("kind", "")) == "formula"
        ),
        "table_evidence": tuple(
            {
                "object_block_id": str(item.get("object_block_id", "")),
                "table_rows": _sequence(item.get("table_rows")),
            }
            for item in _sequence(paper_model.get("definition_neighborhoods", value.get("definition_neighborhoods", ())))
            if isinstance(item, Mapping) and str(item.get("kind", "")) == "table"
        ),
        "definition_neighborhood_ids": tuple(
            {
                "object_block_id": str(item.get("object_block_id", item.get("block_id", ""))),
                "kind": str(item.get("kind", "")),
            }
            for item in _sequence(paper_model.get("definition_neighborhoods", value.get("definition_neighborhoods", ())))
            if isinstance(item, Mapping)
        ),
        "asset_decisions": tuple(
            {
                "asset_id": str(item.get("asset_id", item.get("block_id", ""))),
                "decision": str(item.get("decision", item.get("action", ""))),
                "explanation_job": str(item.get("explanation_job", item.get("reason", ""))),
            }
            for item in _sequence(_value(value.get("asset_plan", {}), "decisions", default=()))
            if isinstance(item, Mapping)
        ),
        "visual_evidence": tuple(
            {
                "visual_block_id": str(item.get("visual_block_id", item.get("block_id", ""))),
                "interpretation": str(item.get("interpretation", "")),
            }
            for item in _sequence(paper_model.get("visual_interpretations", value.get("visual_interpretations", ())))
            if isinstance(item, Mapping)
        ),
    }


def _skim_prompt(request: SkimRequest) -> str:
    return json.dumps({"operation": "global_skim", "title": request.candidate.title, "task": "Return json with paper_type, section_roles, candidate_contributions, candidate_experiments, cross_references, and exactly 5-7 prioritized questions.", "blocks": [request.paper_ir.block_by_id[item].to_dict() for item in request.navigation_block_ids]}, ensure_ascii=False)


def _full_paper_prompt(request: FullPaperReadRequest) -> str:
    return json.dumps({
        "operation": "full_paper_read",
        "title": request.candidate.title,
        "task": "Read the complete ordered paper once and return a compact English PaperModel. Reconstruct the causal argument; do not summarize sections independently and do not echo the paper.",
        "rules": [
            "Always return every required top-level key in required_json_shape.",
            "Return at most 18 atomic source_facts, at most 6 source_limitations, at most 6 syntheses, at most 6 material_unknowns, and at most 7 visual_candidates.",
            "Keep every field atomic and compact, do not echo supplied blocks, and prefer stable IDs plus short source-backed statements over repeated prose.",
            "Every source fact and limitation cites exact supplied block IDs and preserves exact numbers, named entities, and mechanism terms found in the supplied material.",
            "When a dataset, task catalog, corpus, or evaluation set states both an overall total and split counts, preserve the overall total and every central split count together; do not retain only the subtotals.",
            "Extract the central training or evaluation setup, every key experiment and its metric-to-condition relationships, the named mechanism chain, and the conclusion boundary from the supplied material. Keep these as structured, source-backed fields rather than assuming domain-specific vocabulary.",
            "Do not define a formula symbol unless a cited prose or formula block defines it.",
            "When an eligible source fact depends on a supplied formula or structured table, include that object as a non-omitted candidate so its definition neighborhood and exact relationships remain available to the Writer; this does not require rendering it inline.",
            "Mark an unknown high priority only when a blind reader cannot answer why, what problem, how solved, decisive evidence, or conclusion boundary without it. Exact implementation details and unreported hyperparameters are medium or low.",
            "For each material_unknown, return one or more short anchor_terms copied exactly from the paper/model terminology so the Chinese Writer can preserve and validate that boundary across translation.",
            "Choose visual candidates by explanatory value, not completeness. Use inline only when the object carries indispensable meaning beyond its caption, structure, and neighboring prose; otherwise use reference or omit.",
            "If a central method figure explains component relationships, architecture, control flow, or an iterative loop that is necessary to understand the core mechanism, include that central method figure as a candidate even when the neighboring prose is also available.",
            "Formula and structured-table candidates set requires_pixels false. A real figure may set requires_pixels true only when pixels add indispensable information beyond its caption and prose.",
            "Every section asset_job must name an exact non-omitted visual_candidate. Formula and table asset jobs may reference only parse_status=available blocks; never assign an unparsed or partial structured object as an inline job.",
            "In the same full-paper planning response, return ordered sections with reader_question, prerequisite_bridges, reasoning_steps, experiment_slots, asset_jobs, transitions, stop_conditions, evidence_handles, and coverage_obligation_ids. Do not return null sections.",
            "In the same response, return every key experiment as an experiments item with a stable experiment_id, setup, comparison, results, interpretation, boundary, and source_block_ids. Return eligible must_preserve_facts with stable fact_id and eligible=true, and limitations with stable limitation_id. These are ledger inputs, not optional prose suggestions.",
            "Never use target character counts, paragraph counts, uniform depth labels, or a fixed length quota in sections or any completion rule.",
        ],
        "required_json_shape": {
            "thesis": "string",
            "argument_chain": {"background": "string", "concrete_problem": "string", "prior_gap": "string", "research_question": "string", "core_idea": "string", "mechanisms": ["string"], "experiment_logic": "string", "conclusion_scope": "string"},
            "coverage": {name: {"status": "complete|partial|uncertain", "reason": "string", "missing": ["string"]} for name in ("background", "problem", "prior_gap", "mechanism", "experiment", "boundary")},
            "source_facts": [{"fact_id": "string", "facet": "problem|method|experiment|limitation", "statement": "atomic source fact", "source_block_ids": ["exact supplied id"]}],
            "source_limitations": [{"fact_id": "string", "statement": "author-stated limitation", "source_block_ids": ["exact supplied id"]}],
            "agent_syntheses": [{"synthesis_id": "string", "statement": "cross-block interpretation", "source_block_ids": ["exact supplied id"]}],
            "experiments": [{"experiment_id": "stable key", "setup": ["source-backed setup"], "comparison": ["source-backed comparison"], "results": ["source-backed result"], "interpretation": "bounded interpretation", "boundary": "what it does not establish", "source_block_ids": ["exact supplied id"]}],
            "must_preserve_facts": [{"fact_id": "stable fact id", "statement": "source-backed fact", "eligible": True, "source_block_ids": ["exact supplied id"]}],
            "limitations": [{"limitation_id": "stable limitation id", "statement": "source-backed limitation", "source_block_ids": ["exact supplied id"]}],
            "sections": [{"section_id": "ordered stable id", "heading": "reader-facing heading", "reader_question": "question", "prerequisite_bridges": ["bridge"], "reasoning_steps": ["step"], "experiment_slots": ["experiment id"], "asset_jobs": [{"asset_id": "asset id", "job": "explain why it matters"}], "transition_in": "transition", "transition_out": "transition", "stop_conditions": ["observable completion"], "evidence_handles": ["exact supplied id"], "coverage_obligation_ids": ["argument/experiment/fact/limitation/asset id"]}],
            "visual_candidates": [{"block_id": "exact formula/table/figure id", "decision": "inline|reference|omit", "reason": "explanatory value", "requires_pixels": False}],
            "material_unknowns": [{"statement": "unresolved information", "priority": "high|medium|low", "anchor_terms": ["exact technical term"]}],
        },
        "full_ordered_paper": [request.paper_ir.block_by_id[item].to_dict() for item in request.ordered_block_ids],
    }, ensure_ascii=False)


def _target_prompt(request: TargetReadRequest) -> str:
    return json.dumps({"operation": "target_read", "task": "Return json with source_facts, mechanism_relations, agent_syntheses, visual_interpretations, unknowns, evidence_gaps, and conflicts. For every success criterion that is answered by prose or a complete table, include at least one source_facts item with an atomic statement of no more than two sentences, the correct facet, and one or more exact source_block_ids from the supplied blocks. A mechanism_relation or visual_interpretation cannot replace source_facts. If the supplied blocks do not answer the success criterion, return high-priority unknowns and evidence_gaps instead. Never copy an entire block into a source_fact and never invent a source block ID.", "target": _jsonable(request.target), "questions": _jsonable(request.questions), "blocks": [block.to_dict() for block in request.blocks]}, ensure_ascii=False)


def _visual_prompt(request: VisualReadRequest) -> str:
    return json.dumps({
        "operation": "visual_interpretation",
        "task": "Inspect the supplied image pixels and explain only the visual relationships that help answer the reading target and are not already recoverable from the caption or neighboring prose.",
        "rules": [
            "Return one compact interpretation grounded in visible components, arrows, axes, labels, or layout.",
            "Use the caption and neighboring context to identify the visual's role, but do not repeat them as if they came from pixels.",
            "If the pixels are unclear, say exactly what remains unclear instead of guessing.",
        ],
        "target": _jsonable(request.target),
        "caption": request.caption,
        "neighboring_context": request.neighboring_context,
        "block_id": request.block.block_id,
        "required_json_shape": {"interpretation": "pixel-grounded explanation string"},
    }, ensure_ascii=False)


def build_transport_units(bundle: EvidenceBundle, paper_ir: CanonicalPaperIR, max_chars: int = 18_000) -> tuple[TransportUnit, ...]:
    """Split one target bundle for transport only; no memo or conclusion fields."""

    if max_chars < 100:
        raise ValueError("Transport unit size is too small.")
    blocks = [paper_ir.block_by_id[item] for item in bundle.source_block_ids]
    units: list[TransportUnit] = []
    current: list[PaperIRBlock] = []
    size = 0
    for block in blocks:
        if current and size + len(block.text) > max_chars:
            units.append(TransportUnit(f"{bundle.target_id}:u{len(units) + 1}", tuple(item.block_id for item in current), "\n\n".join(item.text for item in current), current[0].order, current[-1].order))
            current, size = [], 0
        current.append(block); size += len(block.text)
    if current:
        units.append(TransportUnit(f"{bundle.target_id}:u{len(units) + 1}", tuple(item.block_id for item in current), "\n\n".join(item.text for item in current), current[0].order, current[-1].order))
    return tuple(units)
