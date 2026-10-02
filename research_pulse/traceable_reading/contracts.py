"""Framework-free contracts for source-linked MinerU reading."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


AnswerStatus = Literal["answered", "partially_answered", "not_stated", "unclear"]
SupportKind = Literal["direct", "synthesis", "reader_inference"]
ReferenceStatus = Literal["valid", "invalid"]
SemanticStatus = Literal["not_evaluated", "supported", "partially_supported", "unsupported", "unclear"]
AssetDecisionKind = Literal["inline", "reference", "omit"]
ExplanationRole = Literal["context", "intuition", "mechanism", "evidence", "interpretation", "limitation"]
NoteFacet = Literal["problem", "method", "experiments", "limitations"]


@dataclass(frozen=True)
class WriterPolicy:
    version: str
    paragraph_flow: tuple[str, ...]
    generic_purposes: tuple[str, ...]


DEFAULT_WRITER_POLICY = WriterPolicy(
    version="pedagogical-note-v3",
    paragraph_flow=("claim", "mechanism", "evidence", "interpretation", "boundary"),
    generic_purposes=("概括", "总结", "介绍", "概述", "summary", "overview", "description"),
)


@dataclass(frozen=True)
class WorkedExamplePlan:
    mode: Literal["source_example", "abstract_walkthrough"]
    setup: str
    steps: tuple[str, ...]
    takeaway: str
    finding_refs: tuple[str, ...]


@dataclass(frozen=True)
class ExperimentUnitPlan:
    unit_id: str
    claim: str
    comparison: str
    result: str
    meaning: str
    boundary: str
    finding_refs: tuple[str, ...]


@dataclass(frozen=True)
class AssetRepresentation:
    representation_type: Literal["image", "html", "markdown", "latex"]
    content: str = ""
    path: str = ""


@dataclass(frozen=True)
class AssetRef:
    asset_id: str
    kind: str
    path: str
    caption: str = ""
    block_id: str | None = None
    representations: tuple[AssetRepresentation, ...] = ()


PaperAsset = AssetRef


@dataclass(frozen=True)
class AssetDecision:
    asset_id: str
    decision: AssetDecisionKind
    representation_type: str | None
    explanation_role: ExplanationRole
    source_block_id: str
    finding_refs: tuple[str, ...] = ()
    reason: str = ""
    required: bool = True


@dataclass(frozen=True)
class MaterialBlock:
    block_id: str
    material_version: str
    order: int
    kind: str
    text: str
    page: int
    bbox: tuple[float, float, float, float]
    locator: str
    text_level: int = 0
    image_path: str | None = None
    caption: str = ""


@dataclass(frozen=True)
class SectionIndex:
    section_id: str
    title: str
    level: int
    start_order: int
    end_order: int
    page_start: int
    page_end: int
    parent_id: str | None = None


@dataclass(frozen=True)
class MaterialPackage:
    source_id: str
    source_url: str
    root: Path
    material_version: str
    markdown_path: Path
    content_list_path: Path
    image_root: Path


@dataclass(frozen=True)
class MaterialDocument:
    package: MaterialPackage
    blocks: tuple[MaterialBlock, ...]
    sections: tuple[SectionIndex, ...]
    assets: tuple[AssetRef, ...] = ()

    @property
    def source_id(self) -> str:
        return self.package.source_id

    @property
    def source_url(self) -> str:
        return self.package.source_url

    @property
    def material_version(self) -> str:
        return self.package.material_version

    def full_markdown(self) -> str:
        return self.package.markdown_path.read_text(encoding="utf-8")

    def original_title(self) -> str:
        import re

        match = re.search(r"(?m)^#\s+(.+?)\s*$", self.full_markdown())
        if match:
            return match.group(1).strip()
        first = next((block.text.strip() for block in self.blocks if block.kind == "title"), "")
        if not first:
            raise ValueError("material_missing_original_title")
        return first

    def block(self, block_id: str) -> MaterialBlock:
        try:
            return next(item for item in self.blocks if item.block_id == block_id)
        except StopIteration as exc:
            raise KeyError(block_id) from exc

    def section(self, section_id: str) -> SectionIndex:
        try:
            return next(item for item in self.sections if item.section_id == section_id)
        except StopIteration as exc:
            raise KeyError(section_id) from exc

    def blocks_for_section(self, section_id: str, *, neighbor_count: int = 1) -> tuple[MaterialBlock, ...]:
        section = self.section(section_id)
        start = max(0, section.start_order - neighbor_count)
        end = min(len(self.blocks) - 1, section.end_order + neighbor_count)
        return tuple(block for block in self.blocks if start <= block.order <= end)

    def locate(self, block_id: str) -> Mapping[str, Any]:
        block = self.block(block_id)
        return {
            "material_version": block.material_version,
            "locator": block.locator,
            "page": block.page,
            "bbox": list(block.bbox),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "package": {**asdict(self.package), "root": str(self.package.root), "markdown_path": str(self.package.markdown_path), "content_list_path": str(self.package.content_list_path), "image_root": str(self.package.image_root)},
            "blocks": [asdict(item) for item in self.blocks],
            "sections": [asdict(item) for item in self.sections],
            "assets": [asdict(item) for item in self.assets],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MaterialDocument":
        raw_package = dict(payload["package"])
        for key in ("root", "markdown_path", "content_list_path", "image_root"):
            raw_package[key] = Path(raw_package[key])
        package = MaterialPackage(**raw_package)
        blocks = tuple(MaterialBlock(**{**item, "bbox": tuple(item["bbox"])}) for item in payload["blocks"])
        sections = tuple(SectionIndex(**item) for item in payload["sections"])
        assets = tuple(AssetRef(**{**item, "representations": tuple(AssetRepresentation(**rep) for rep in item.get("representations", ()))}) for item in payload["assets"])
        return cls(package, blocks, sections, assets)


@dataclass(frozen=True)
class EvidenceRequirement:
    requirement_id: str
    description: str
    required: bool = True


@dataclass(frozen=True)
class ReadingObligation:
    obligation_id: str
    question: str
    purpose: str
    target_section_ids: tuple[str, ...]
    target_asset_ids: tuple[str, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]
    facet: Literal["problem", "method", "experiments", "limitations"]
    core: bool = True


@dataclass(frozen=True)
class PaperMap:
    paper_type: str
    problem: str
    approach: str
    experiments: str
    limitations: str
    important_asset_ids: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadingPlan:
    obligations: tuple[ReadingObligation, ...]


@dataclass(frozen=True)
class SurveyResult:
    paper_map: PaperMap
    reading_plan: ReadingPlan


@dataclass(frozen=True)
class ReadingPacket:
    packet_id: str
    material_version: str
    obligations: tuple[ReadingObligation, ...]
    blocks: tuple[MaterialBlock, ...]
    assets: tuple[AssetRef, ...] = ()


@dataclass(frozen=True)
class RequirementResult:
    requirement_id: str
    status: AnswerStatus
    finding_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    finding_id: str
    statement: str
    answer_quote: str
    requirement_id: str
    support_kind: SupportKind
    block_refs: tuple[str, ...]


@dataclass(frozen=True)
class AssetUse:
    asset_id: str
    explanation: str
    finding_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadingAnswer:
    answer_id: str
    obligation_id: str
    status: AnswerStatus
    answer: str
    findings: tuple[Finding, ...]
    requirement_results: tuple[RequirementResult, ...]
    asset_uses: tuple[AssetUse, ...] = ()
    asset_use_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceBlockRef:
    block_id: str
    material_version: str
    locator: str
    page: int
    bbox: tuple[float, float, float, float]
    excerpt: str


@dataclass(frozen=True)
class SourceSpan:
    span_id: str
    finding_id: str
    source_id: str
    material_version: str
    blocks: tuple[SourceBlockRef, ...]


@dataclass(frozen=True)
class EvidenceAssessment:
    reference_status: ReferenceStatus
    semantic_status: SemanticStatus = "not_evaluated"
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadingCoverage:
    obligation_statuses: Mapping[str, AnswerStatus]
    requirement_statuses: Mapping[str, AnswerStatus]
    core_complete: bool


@dataclass(frozen=True)
class SectionNotePlan:
    section_id: str
    facet: NoteFacet
    title: str
    reader_question: str
    direct_answer: str
    mechanism_sequence: tuple[str, ...]
    evidence_focus: tuple[str, ...]
    interpretation_goal: str
    boundary: str
    finding_refs: tuple[str, ...]
    asset_refs: tuple[str, ...] = ()
    worked_example: WorkedExamplePlan | None = None
    experiment_units: tuple[ExperimentUnitPlan, ...] = ()


@dataclass(frozen=True)
class NotePlan:
    policy_version: str
    sections: tuple[SectionNotePlan, ...]


@dataclass(frozen=True)
class NoteBlock:
    block_id: str
    kind: Literal["paragraph", "heading", "list", "figure", "table", "equation"]
    text: str
    finding_refs: tuple[str, ...] = ()
    asset_ref: str | None = None
    caption: str = ""
    explanation: str = ""
    explanation_role: ExplanationRole | None = None
    paragraph_purpose: str = ""
    plan_item_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoteSection:
    section_id: str
    title: str
    blocks: tuple[NoteBlock, ...]
    reader_question: str = ""


@dataclass(frozen=True)
class NoteDraft:
    title: str
    sections: tuple[NoteSection, ...]
    original_title: str = ""


@dataclass(frozen=True)
class RunReceipt:
    source_id: str
    material_status: str
    survey_status: str
    reading_status: str
    source_link_status: ReferenceStatus
    semantic_evidence_status: SemanticStatus
    publication_status: str
    evidence_level: str
    rag_eligible: bool
    published_path: str | None = None
    evidence_index_path: str | None = None
    receipt_path: str | None = None
    safe_errors: tuple[str, ...] = field(default_factory=tuple)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)
