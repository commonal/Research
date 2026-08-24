"""Deterministic gate that decides whether answer generation is allowed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from research_pulse.rag.contracts import EvidenceHit


@dataclass(frozen=True)
class SufficiencyDecision:
    sufficient: bool
    reason: str
    distinct_asset_count: int
    anchored_hit_count: int


def assess_sufficiency(hits: Sequence[EvidenceHit], *, minimum_hits: int = 2) -> SufficiencyDecision:
    """Block answer generation until multiple source-addressable hits exist.

    Raw score thresholds are intentionally absent until calibrated per embedding
    model. This gate uses invariants that remain meaningful across retrievers.
    """

    anchored_hits = [hit for hit in hits if hit.anchor_id and hit.source_url]
    distinct_assets = {hit.knowledge_id for hit in anchored_hits}
    if len(anchored_hits) < minimum_hits:
        return SufficiencyDecision(
            sufficient=False,
            reason=f"仅找到 {len(anchored_hits)} 条可定位证据，至少需要 {minimum_hits} 条。",
            distinct_asset_count=len(distinct_assets),
            anchored_hit_count=len(anchored_hits),
        )
    return SufficiencyDecision(
        sufficient=True,
        reason="存在足够的可定位知识库证据，可以进入受控回答生成。",
        distinct_asset_count=len(distinct_assets),
        anchored_hit_count=len(anchored_hits),
    )
