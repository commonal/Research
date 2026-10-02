"""Prepare paper assets once for planning, rendering, and audit."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, UnidentifiedImageError

from research_pulse.production.reading import PaperIRBlock

from .asset_quality import AssetQualityAssessment, AssetQualityGate
from .deepseek_adapters import assets_from_blocks
from .renderer import PublishedAsset


@dataclass(frozen=True)
class PreparedAssetCatalog:
    planner_candidates: tuple[Mapping[str, Any], ...]
    publishable_assets: Mapping[str, PublishedAsset]
    diagnostics: tuple[AssetQualityAssessment, ...]


class AssetPreparation:
    """Preserve representations, choose one deterministically, and expose compatible views."""

    @classmethod
    def prepare(
        cls,
        blocks: Sequence[PaperIRBlock],
        *,
        asset_root: Path | None = None,
    ) -> PreparedAssetCatalog:
        assets, candidates = assets_from_blocks(blocks)
        source_blocks = {block.block_id: block for block in _asset_blocks(blocks)}

        for candidate in candidates:
            source_block_id = str(candidate.get("source_block_id") or "")
            block = source_blocks.get(source_block_id)
            if block is None:
                raise ValueError(
                    f"Asset candidate {candidate.get('asset_id')!r} has no canonical source block "
                    f"{source_block_id!r}."
                )
            if block.kind == "table" and block.table_html:
                candidate["_source_table_html"] = block.table_html
            asset_id = str(candidate["asset_id"])
            image_path = _usable_image(block.image_path, asset_root)
            asset = assets.get(asset_id)
            if asset is not None and image_path is not None:
                assets[asset_id] = replace(asset, image_path=str(image_path), image_verified=True)
            elif asset is not None and asset.image_path:
                assets[asset_id] = replace(asset, image_path=None, image_verified=False)
            elif asset is None and image_path is not None:
                assets[asset_id] = PublishedAsset(
                    asset_id=asset_id,
                    kind=str(candidate["kind"]),
                    image_path=str(image_path),
                    caption=str(candidate.get("caption") or ""),
                    image_verified=True,
                )

        quality = AssetQualityGate().evaluate(assets, candidates)
        prepared_assets: dict[str, PublishedAsset] = {}
        prepared_candidates: list[Mapping[str, Any]] = []
        for candidate, assessment in zip(candidates, quality.assessments, strict=True):
            asset_id = str(candidate["asset_id"])
            asset = assets.get(asset_id)
            annotated = dict(candidate)
            annotated.pop("_source_table_html", None)
            selected_quality = assessment.status
            annotated["asset_quality_reasons"] = list(assessment.reasons)

            if asset is not None and assessment.status == "usable":
                selected = "image" if asset.kind == "figure" else ("latex" if asset.kind == "formula" else "markdown_table")
                prepared_assets[asset_id] = replace(asset, selected_format=selected)
                annotated["renderable"] = True
                annotated["evidence_capability"] = "descriptive" if selected == "image" else "exact"
            elif asset is not None and asset.image_path and _can_fallback_to_image(asset, assessment):
                prepared_assets[asset_id] = replace(asset, markdown=None, selected_format="image")
                annotated["renderable"] = True
                annotated["evidence_capability"] = "descriptive"
                annotated["preview"] = ""
                selected_quality = "usable" if asset.kind == "figure" else "degraded"
            else:
                annotated["renderable"] = False
                annotated["evidence_capability"] = "none"
            annotated["asset_quality"] = selected_quality
            prepared_candidates.append(annotated)

        return PreparedAssetCatalog(
            planner_candidates=tuple(prepared_candidates),
            publishable_assets=prepared_assets,
            diagnostics=quality.assessments,
        )


def _asset_blocks(blocks: Sequence[PaperIRBlock]) -> list[PaperIRBlock]:
    return [block for block in blocks if block.kind in {"table", "formula", "figure"}]


def _can_fallback_to_image(asset: PublishedAsset, assessment: AssetQualityAssessment) -> bool:
    if not asset.image_verified:
        return False
    if assessment.status == "degraded":
        return True
    if asset.kind == "figure":
        return True
    return asset.kind == "formula" and "image_only_formula_not_renderable" in assessment.reasons


def _usable_image(value: str | None, asset_root: Path | None) -> Path | None:
    if not value:
        return None
    raw = Path(value)
    if asset_root is None:
        return None
    trusted_root = asset_root.resolve()
    path = raw.resolve() if raw.is_absolute() else (trusted_root / raw).resolve()
    try:
        path.relative_to(trusted_root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError):
        return None
    if width < 2 or height < 2:
        return None
    return path.resolve()
