"""Deterministic fixed-scope resolution over managed paper blocks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence


FixedScope = Literal["none", "selection", "section", "full"]


class ContextResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvableBlock:
    block_id: str
    text: str
    section_path: tuple[str, ...]
    page: int | None
    order: int
    bbox: tuple[float, float, float, float] | None = None
    # MinerU API content-list coordinates are expressed on a fixed 1000x1000
    # page canvas, while other parsers generally use PDF page points.  Keep
    # the coordinate contract alongside the box so clients can map it to the
    # rendered PDF without guessing from the numeric range.
    bbox_space: str = "page_points"
    bbox_dimensions: tuple[float, float] | None = None


@dataclass(frozen=True)
class ResolvedContext:
    scope: FixedScope
    blocks: tuple[ResolvableBlock, ...]
    truncated: bool
    available_block_count: int
    included_block_count: int
    block_id: str | None = None
    section_path: tuple[str, ...] = ()
    block_ids: tuple[str, ...] = ()


class ContextResolver:
    def __init__(self, *, max_blocks: int) -> None:
        if max_blocks < 1:
            raise ValueError("max_blocks must be positive")
        self.max_blocks = max_blocks

    def resolve(
        self,
        scope: str,
        blocks: Sequence[ResolvableBlock],
        *,
        block_id: str | None = None,
        block_ids: tuple[str, ...] = (),
        section_path: tuple[str, ...] = (),
    ) -> ResolvedContext:
        ordered = tuple(sorted(blocks, key=lambda block: (block.order, block.block_id)))
        if scope == "none":
            return ResolvedContext("none", (), False, 0, 0)
        if scope == "selection":
            requested_ids = tuple(dict.fromkeys(block_ids or ((block_id,) if block_id else ())))
            if not requested_ids:
                raise ContextResolutionError("selection scope requires a full block_id")
            selected = tuple(block for block in ordered if block.block_id in requested_ids)
            if len(selected) != len(requested_ids):
                raise ContextResolutionError("selection block is unresolved")
            return ResolvedContext(
                "selection", selected, False, len(selected), len(selected),
                block_id=requested_ids[0], block_ids=requested_ids,
            )
        if scope == "section":
            if not section_path:
                raise ContextResolutionError("section scope requires section_path")
            selected = tuple(
                block
                for block in ordered
                if block.section_path[: len(section_path)] == section_path
            )
            if not selected:
                raise ContextResolutionError("section is unresolved")
            return self._bounded("section", selected, section_path=section_path)
        if scope == "full":
            return self._bounded("full", ordered)
        raise ContextResolutionError("scope is not a fixed paper context")

    def _bounded(
        self,
        scope: Literal["section", "full"],
        blocks: tuple[ResolvableBlock, ...],
        *,
        section_path: tuple[str, ...] = (),
    ) -> ResolvedContext:
        included = blocks[: self.max_blocks]
        return ResolvedContext(
            scope,
            included,
            len(included) < len(blocks),
            len(blocks),
            len(included),
            section_path=section_path,
        )
