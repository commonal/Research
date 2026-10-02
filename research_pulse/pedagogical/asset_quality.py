"""Deterministic quality classification for assets before planning."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping, Sequence

from .renderer import PublishedAsset

AssetQualityStatus = Literal["usable", "degraded", "unavailable"]


@dataclass(frozen=True)
class AssetQualityAssessment:
    asset_id: str
    kind: str
    status: AssetQualityStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssetQualityReport:
    assessments: tuple[AssetQualityAssessment, ...]
    planner_candidates: tuple[Mapping[str, Any], ...]


class AssetQualityGate:
    """Classify extracted assets without calling an LLM or rejecting a paper."""

    def evaluate(
        self,
        assets: Mapping[str, PublishedAsset],
        candidates: Sequence[Mapping[str, Any]],
    ) -> AssetQualityReport:
        assessments: list[AssetQualityAssessment] = []
        planner_candidates: list[Mapping[str, Any]] = []
        for candidate in candidates:
            asset_id = str(candidate.get("asset_id") or "")
            kind = str(candidate.get("kind") or "")
            assessment = self._assess(asset_id, kind, assets.get(asset_id), candidate)
            assessments.append(assessment)
            annotated = dict(candidate)
            annotated["asset_quality"] = assessment.status
            annotated["asset_quality_reasons"] = list(assessment.reasons)
            annotated["renderable"] = bool(candidate.get("renderable")) and assessment.status == "usable"
            planner_candidates.append(annotated)
        return AssetQualityReport(tuple(assessments), tuple(planner_candidates))

    def _assess(
        self,
        asset_id: str,
        kind: str,
        asset: PublishedAsset | None,
        candidate: Mapping[str, Any],
    ) -> AssetQualityAssessment:
        if asset is None:
            return AssetQualityAssessment(asset_id, kind, "unavailable", ("missing_renderable_asset",))
        if kind == "table" and _looks_like_table_of_contents(asset, candidate):
            return AssetQualityAssessment(asset_id, kind, "unavailable", ("table_of_contents",))
        if kind == "table" and _has_unpreserved_spanning_cells(candidate):
            return AssetQualityAssessment(asset_id, kind, "degraded", ("spanning_cells_not_preserved",))
        if kind == "table" and _has_blank_header_cell(str(asset.markdown or "")):
            return AssetQualityAssessment(asset_id, kind, "degraded", ("blank_header_cell",))
        if kind == "table" and not _is_well_formed_markdown_table(str(asset.markdown or "")):
            return AssetQualityAssessment(asset_id, kind, "degraded", ("malformed_markdown_table",))
        if kind == "table" and _has_collapsed_measurements(str(asset.markdown or "")):
            return AssetQualityAssessment(asset_id, kind, "degraded", ("collapsed_measurements",))
        if kind == "table" and _has_shifted_cell_continuation(str(asset.markdown or "")):
            return AssetQualityAssessment(asset_id, kind, "degraded", ("shifted_cell_continuation",))
        if kind == "table" and _has_collapsed_key_items(str(asset.markdown or "")):
            return AssetQualityAssessment(asset_id, kind, "degraded", ("collapsed_key_items",))
        if kind == "formula":
            markdown = str(asset.markdown or "").strip()
            if not markdown and asset.image_path:
                return AssetQualityAssessment(
                    asset_id, kind, "unavailable", ("image_only_formula_not_renderable",)
                )
            if not markdown:
                return AssetQualityAssessment(asset_id, kind, "unavailable", ("missing_formula_content",))
            if not _balanced_latex(markdown):
                return AssetQualityAssessment(asset_id, kind, "degraded", ("unbalanced_latex",))
            if not _matched_latex_environments(markdown):
                return AssetQualityAssessment(asset_id, kind, "degraded", ("mismatched_latex_environment",))
        if kind == "figure":
            if not asset.image_path:
                return AssetQualityAssessment(asset_id, kind, "unavailable", ("missing_image_path",))
            if not asset.image_verified:
                return AssetQualityAssessment(
                    asset_id, kind, "unavailable", ("unverified_image_representation",)
                )
        return AssetQualityAssessment(asset_id, kind, "usable")


def _looks_like_table_of_contents(asset: PublishedAsset, candidate: Mapping[str, Any]) -> bool:
    caption = str(asset.caption or candidate.get("caption") or "").strip().casefold()
    if caption in {"contents", "table of contents", "目录", "目次"}:
        return True
    markdown = str(asset.markdown or "")
    dotted_leaders = len(re.findall(r"(?:\.\s*){4,}", markdown))
    numbered_sections = len(re.findall(r"(?m)^\|\s*[A-Z](?:\.\d+)?\s*\|", markdown))
    return dotted_leaders >= 2 and numbered_sections >= 2


def _has_unpreserved_spanning_cells(candidate: Mapping[str, Any]) -> bool:
    source_html = str(candidate.get("_source_table_html") or "")
    return bool(re.search(r"\b(?:rowspan|colspan)\s*=", source_html, flags=re.IGNORECASE))


def _has_blank_header_cell(markdown: str) -> bool:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if not lines:
        return False
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return any(not cell for cell in header[1:])


def _is_well_formed_markdown_table(markdown: str) -> bool:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 3 or any(not line.startswith("|") or not line.endswith("|") for line in lines):
        return False
    widths = [len(line.strip("|").split("|")) for line in lines]
    if len(set(widths)) != 1 or widths[0] < 2:
        return False
    separator = [cell.strip() for cell in lines[1].strip("|").split("|")]
    return all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)


def _balanced_latex(markdown: str) -> bool:
    if not (markdown.startswith("$$") and markdown.endswith("$$")):
        return False
    body = markdown[2:-2]
    depth = 0
    for index, char in enumerate(body):
        if char not in "{}" or (index > 0 and body[index - 1] == "\\"):
            continue
        depth += 1 if char == "{" else -1
        if depth < 0:
            return False
    return depth == 0


def _matched_latex_environments(markdown: str) -> bool:
    stack: list[str] = []
    for match in re.finditer(r"\\(begin|end)\{([^{}]+)\}", markdown):
        operation, environment = match.groups()
        if operation == "begin":
            stack.append(environment)
        elif not stack or stack.pop() != environment:
            return False
    return not stack


def _has_collapsed_measurements(markdown: str) -> bool:
    measurement = re.compile(r"\d+(?:\.\d+)?\s*(?:±|\\pm)\s*\d+(?:\.\d+)?")
    for line in markdown.splitlines()[2:]:
        for cell in line.strip().strip("|").split("|"):
            if len(measurement.findall(cell)) >= 2:
                return True
    return False


def _has_collapsed_key_items(markdown: str) -> bool:
    """Detect row keys merged by a failed rowspan/line reconstruction.

    A notation/key column should identify one logical item per row.  Two or
    more separate inline-math spans in that first cell indicate that multiple
    source rows were collapsed into one Markdown row.
    """
    for line in markdown.splitlines()[2:]:
        cells = line.strip().strip("|").split("|")
        if cells and len(re.findall(r"(?<!\\)\$[^$]+(?<!\\)\$", cells[0])) >= 2:
            return True
    return False


def _has_shifted_cell_continuation(markdown: str) -> bool:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in markdown.splitlines()[2:]
        if line.strip()
    ]
    for previous, current in zip(rows, rows[1:]):
        if len(previous) < 2 or len(current) != len(previous):
            continue
        if (
            re.search(r"[A-Za-z]{2,}-$", previous[-1])
            and current[0]
            and re.match(r"^[a-z]{2,}\b", current[-1])
        ):
            return True
    return False
