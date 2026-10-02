"""Deterministic PDF selection-to-normalized-block matching."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Sequence


_LIGATURES = str.maketrans(
    {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}
)


def normalize_selection_text(value: str) -> str:
    normalized = value.translate(_LIGATURES)
    # Browser selections often collapse a PDF line break to a space before
    # reaching the matcher ("office- aware"). Treat both forms as one word.
    normalized = re.sub(r"-\s+", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().casefold()


@dataclass(frozen=True)
class NormalizedBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)


@dataclass(frozen=True)
class PdfSelection:
    text: str
    page: int
    bbox: NormalizedBox | None = None


@dataclass(frozen=True)
class MatchableBlock:
    block_id: str
    text: str
    page: int
    bbox: NormalizedBox | None = None
    order: int = 0


@dataclass(frozen=True)
class SelectionMatch:
    status: Literal["resolved", "unresolved"]
    block_id: str | None
    method: Literal["text", "bbox"] | None = None


def match_selection_to_block(
    selection: PdfSelection,
    blocks: Sequence[MatchableBlock],
) -> SelectionMatch:
    query = normalize_selection_text(selection.text)
    same_page = tuple(block for block in blocks if block.page == selection.page)
    if not query or not same_page:
        return SelectionMatch("unresolved", None)

    textual = tuple(
        block
        for block in same_page
        if query in normalize_selection_text(block.text)
    )
    if textual:
        selected = _best_block(textual, selection.bbox)
        return SelectionMatch("resolved", selected.block_id, "text")

    if selection.bbox is None or selection.bbox.area <= 0:
        return SelectionMatch("unresolved", None)
    overlapping = tuple(
        block
        for block in same_page
        if block.bbox is not None and _intersection(selection.bbox, block.bbox) > 0
    )
    if not overlapping:
        return SelectionMatch("unresolved", None)
    selected = _best_block(overlapping, selection.bbox)
    return SelectionMatch("resolved", selected.block_id, "bbox")


def _best_block(
    blocks: Sequence[MatchableBlock],
    selection_box: NormalizedBox | None,
) -> MatchableBlock:
    if selection_box is None or selection_box.area <= 0:
        return min(blocks, key=lambda block: block.order)
    return min(
        blocks,
        key=lambda block: (
            -_intersection(selection_box, block.bbox) if block.bbox else 0.0,
            block.order,
        ),
    )


def _intersection(left: NormalizedBox, right: NormalizedBox) -> float:
    width = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    height = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
    return width * height
