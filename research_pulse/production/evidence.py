"""Framework-free, transient evidence-block contracts for paper production."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol
import re


EvidenceKind = Literal["text", "formula", "table", "figure", "caption"]
ParseStatus = Literal["available", "degraded", "unparsed"]
LocatorCompleteness = Literal["exact", "partial", "section_only", "missing"]
EvidenceFacet = Literal["problem", "method", "experiment", "limitation"]


@dataclass(frozen=True)
class EvidenceCandidate:
    """One parser-produced block that exists only for the current run."""

    block_id: str
    kind: EvidenceKind
    text: str
    source_url: str
    parser: str
    parse_status: ParseStatus
    locator_completeness: LocatorCompleteness
    section_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    caption: str | None = None
    latex: str | None = None
    table_html: str | None = None
    image_path: str | None = None
    image_source_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.block_id.strip() or not self.parser.strip():
            raise ValueError("Evidence candidates require non-empty IDs and parser names.")
        if self.kind not in {"text", "formula", "table", "figure", "caption"}:
            raise ValueError("Evidence candidate kind is invalid.")
        if self.parse_status not in {"available", "degraded", "unparsed"}:
            raise ValueError("Evidence candidate parse status is invalid.")
        if self.locator_completeness not in {"exact", "partial", "section_only", "missing"}:
            raise ValueError("Evidence candidate locator completeness is invalid.")
        if not self.source_url.startswith(("https://", "http://")):
            raise ValueError("Evidence candidates require an HTTP(S) source URL.")
        if self.page_start is not None and self.page_start < 1:
            raise ValueError("Evidence candidate page_start must be positive.")
        if self.page_end is not None and (self.page_start is None or self.page_end < self.page_start):
            raise ValueError("Evidence candidate page_end requires a valid page_start.")
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("Evidence candidate bbox must contain four coordinates.")
        if self.locator_completeness == "exact" and self.page_start is None and self.bbox is None:
            raise ValueError("An exact EvidenceCandidate locator needs a page or bbox.")


@dataclass(frozen=True)
class EvidenceBlock:
    """A quality-classified candidate that may be proposed to the extractor."""

    candidate: EvidenceCandidate
    eligible_for_fact: bool
    supported_facets: tuple[EvidenceFacet, ...] = ()
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if len(self.supported_facets) != len(set(self.supported_facets)):
            raise ValueError("EvidenceBlock facets must be unique.")
        if any(facet not in {"problem", "method", "experiment", "limitation"} for facet in self.supported_facets):
            raise ValueError("EvidenceBlock facet is invalid.")
        if self.eligible_for_fact and self.candidate.parse_status != "available":
            raise ValueError("Only available candidates can be eligible facts.")
        if self.eligible_for_fact and not self.candidate.text.strip():
            raise ValueError("Eligible EvidenceBlocks require readable text.")
        if not self.eligible_for_fact and not (self.rejection_reason or "").strip():
            raise ValueError("Ineligible EvidenceBlocks require a stable rejection reason.")


class SupplementalEvidenceResolver(Protocol):
    """Optional, per-block fallback; it must not discover or parse another paper."""

    def resolve(self, candidate: EvidenceCandidate) -> EvidenceCandidate | None: ...


@dataclass(frozen=True)
class RejectingSupplementalEvidenceResolver:
    """Default no-cost seam used until a separately evaluated adapter is configured."""

    allowed_kinds: tuple[EvidenceKind, ...] = ("formula", "table", "figure", "caption")

    def resolve(self, candidate: EvidenceCandidate) -> EvidenceCandidate | None:
        if candidate.kind not in self.allowed_kinds:
            raise ValueError("Supplemental resolution is limited to degraded non-text blocks.")
        if candidate.parse_status == "available":
            raise ValueError("Supplemental resolution requires a degraded or unparsed block.")
        return None


def classify_candidate(candidate: EvidenceCandidate) -> EvidenceBlock:
    """Classify source cleanliness before an LLM can select the block."""

    normalized = " ".join(candidate.text.split())
    if candidate.parse_status != "available":
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason=f"parse_{candidate.parse_status}")
    if _is_reference_section(candidate.section_path) or _looks_like_author_or_reference_noise(normalized):
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="bibliographic_noise")
    if candidate.kind == "formula":
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="formula_not_auto_interpreted")
    if _looks_like_table_title_only(normalized):
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="table_title_only")
    if candidate.kind == "table" and not _has_self_contained_table_result(normalized):
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="table_result_incomplete")
    if candidate.kind == "figure" and not (candidate.caption or "").strip():
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="figure_caption_missing")
    return EvidenceBlock(
        candidate,
        eligible_for_fact=True,
        supported_facets=_supported_facets(candidate),
    )


def classify_candidates(candidates: tuple[EvidenceCandidate, ...]) -> tuple[EvidenceBlock, ...]:
    return tuple(classify_candidate(candidate) for candidate in candidates)


def classify_with_optional_supplement(
    candidate: EvidenceCandidate,
    *,
    resolver: SupplementalEvidenceResolver | None,
) -> EvidenceBlock:
    """Try one explicitly scoped fallback without weakening the original boundary."""

    initial = classify_candidate(candidate)
    if initial.eligible_for_fact or resolver is None or candidate.kind == "text":
        return initial

    # A fallback receives only a deliberately degraded view of this one block.
    degraded = replace(candidate, parse_status="degraded")
    try:
        supplemented = resolver.resolve(degraded)
    except Exception:
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="supplementation_failed")
    if supplemented is None:
        return initial
    if (
        supplemented.block_id != candidate.block_id
        or supplemented.source_url != candidate.source_url
        or supplemented.kind != candidate.kind
        or supplemented.parse_status != "available"
    ):
        return EvidenceBlock(candidate, eligible_for_fact=False, rejection_reason="supplementation_invalid_result")
    return classify_candidate(supplemented)


def _looks_like_author_or_reference_noise(text: str) -> bool:
    lowered = text.casefold()
    if lowered.startswith(("references", "bibliography")) or re.match(r"^\[?\d+\]?\s", text):
        return True
    author_markers = ("university", "department of", "institute of", "@", "corresponding author")
    body_markers = ("abstract", "we ", "this paper", "we propose", "we present")
    return any(marker in lowered for marker in author_markers) and not any(marker in lowered for marker in body_markers)


def _is_reference_section(section_path: tuple[str, ...]) -> bool:
    return any(part.strip().casefold() in {"references", "bibliography", "参考文献"} for part in section_path)


def _has_self_contained_table_result(text: str) -> bool:
    # A result needs a metric, at least two compared values, and enough labels
    # to name the metric and comparison objects inside this one projection.
    numbers = re.findall(r"(?<![\w.])\d+(?:\.\d+)?%?", text)
    labels = re.findall(r"[A-Za-z][A-Za-z0-9._-]*|[\u4e00-\u9fff]{2,}", text)
    return text.count("|") >= 6 and len(numbers) >= 2 and len(labels) >= 3


def _looks_like_table_title_only(text: str) -> bool:
    if not re.match(r"^(?:table|tab\.)\s+(?:[ivxlcdm]+|\d+)\b", text, flags=re.IGNORECASE):
        return False
    numeric_values = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", text)
    return "|" not in text and len(numeric_values) <= 1


def _supported_facets(candidate: EvidenceCandidate) -> tuple[EvidenceFacet, ...]:
    """Keep fact categories within the semantics a block type can support."""

    section = " ".join(candidate.section_path).casefold()
    searchable = " ".join((candidate.caption or "", candidate.text[:600])).casefold()
    if candidate.kind == "table":
        return ("experiment",)
    if candidate.kind == "figure":
        if any(marker in searchable for marker in ("architecture", "pipeline", "framework", "workflow")):
            return ("method",)
        return ("experiment",)
    if candidate.kind == "caption":
        return ("experiment",) if any(marker in searchable for marker in ("result", "accuracy", "benchmark")) else ("method",)
    if any(marker in section for marker in ("limitation", "limitations", "threat", "future work", "局限")):
        return ("limitation",)
    if any(marker in section for marker in ("experiment", "evaluation", "results", "benchmark", "ablation", "实验", "结果")):
        return ("experiment",)
    if any(marker in section for marker in ("method", "methodology", "approach", "architecture", "framework", "implementation", "方法")):
        return ("method",)
    if any(marker in section for marker in ("abstract", "introduction", "motivation", "background", "problem", "引言", "背景")):
        return ("problem",)
    if any(marker in searchable for marker in ("limitation", "limitations", "threat", "future work", "局限")):
        return ("limitation",)
    if any(marker in searchable for marker in ("experiment", "results", "evaluation", "benchmark", "实验", "结果")):
        return ("experiment",)
    if any(marker in searchable for marker in ("we propose", "our method", "our approach", "architecture", "pipeline", "方法")):
        return ("method",)
    if any(marker in searchable for marker in ("problem", "challenge", "addresses", "risk", "insufficient", "fails to", "问题", "挑战")):
        return ("problem",)
    return ()
