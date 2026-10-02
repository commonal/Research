"""MinerU material package loader and deterministic navigation indexes."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
import json

from .asset_quality import assess_representation, table_html_to_markdown
from .contracts import AssetRef, AssetRepresentation, MaterialBlock, MaterialDocument, MaterialPackage, SectionIndex


class MaterialError(ValueError):
    """Safe failure raised before any model is called."""


def load_material_document(root: Path, *, source_id: str, source_url: str) -> MaterialDocument:
    root = Path(root)
    markdown_path = root / "full.md"
    content_path = root / "content_list.json"
    if not markdown_path.is_file() or not markdown_path.read_bytes().strip():
        raise MaterialError("material_missing_full_markdown")
    if not content_path.is_file() or not content_path.read_bytes().strip():
        raise MaterialError("material_missing_content_list")
    try:
        payload = json.loads(content_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterialError("material_invalid_content_list") from exc
    if not isinstance(payload, list) or not payload:
        raise MaterialError("material_empty_content_list")
    version = sha256(markdown_path.read_bytes() + b"\0" + content_path.read_bytes()).hexdigest()
    blocks = _blocks(payload, source_id=source_id, material_version=version)
    if not blocks:
        raise MaterialError("material_has_no_readable_blocks")
    package = MaterialPackage(
        source_id=source_id,
        source_url=source_url,
        root=root,
        material_version=version,
        markdown_path=markdown_path,
        content_list_path=content_path,
        image_root=root / "images",
    )
    sections = _sections(blocks)
    assets = _assets(blocks, root)
    return MaterialDocument(package, tuple(blocks), tuple(sections), tuple(assets))


def _blocks(payload: list[Any], *, source_id: str, material_version: str) -> list[MaterialBlock]:
    result: list[MaterialBlock] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("type") or "text").casefold()
        text = _content(raw, kind)
        caption = _joined(raw.get("image_caption") or raw.get("table_caption") or raw.get("chart_caption") or raw.get("caption"))
        if not text and not caption:
            continue
        page_idx = raw.get("page_idx", 0)
        if isinstance(page_idx, bool) or not isinstance(page_idx, int) or page_idx < 0:
            raise MaterialError(f"material_invalid_page:{index}")
        bbox = _bbox(raw.get("bbox"), index)
        level = raw.get("text_level", 0)
        level = level if isinstance(level, int) and not isinstance(level, bool) and level >= 0 else 0
        image_path = _safe_relative(raw.get("img_path"))
        result.append(MaterialBlock(
            block_id=f"material:{source_id}:{material_version[:12]}:{index}",
            material_version=material_version,
            order=len(result),
            kind=kind,
            text=text or caption,
            page=page_idx + 1,
            bbox=bbox,
            locator=f"content_list.json#/{index}",
            text_level=level,
            image_path=image_path,
            caption=caption,
        ))
    return result


def _sections(blocks: Sequence[MaterialBlock]) -> list[SectionIndex]:
    headings = [(index, block) for index, block in enumerate(blocks) if block.text_level > 0 or block.kind in {"title", "heading"}]
    if not headings:
        return [SectionIndex("section:document", "全文", 0, 0, len(blocks) - 1, blocks[0].page, blocks[-1].page)]
    sections: list[SectionIndex] = []
    stack: list[tuple[int, str]] = []
    for position, (start, block) in enumerate(headings):
        level = max(1, block.text_level or 1)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        section_id = f"section:{start}:{sha256(block.text.encode()).hexdigest()[:8]}"
        next_same_or_higher = next((candidate for candidate, other in headings[position + 1:] if max(1, other.text_level or 1) <= level), len(blocks))
        end = max(start, next_same_or_higher - 1)
        sections.append(SectionIndex(section_id, block.text, level, start, end, block.page, blocks[end].page, parent_id))
        stack.append((level, section_id))
    first_heading = headings[0][0]
    if first_heading > 0:
        sections.insert(0, SectionIndex("section:document", "文档前言", 0, 0, first_heading - 1, blocks[0].page, blocks[first_heading - 1].page))
    return sections


def _assets(blocks: Sequence[MaterialBlock], root: Path) -> list[AssetRef]:
    assets: list[AssetRef] = []
    for block in blocks:
        representations: list[AssetRepresentation] = []
        if block.image_path:
            candidate = root / Path(*PurePosixPath(block.image_path).parts)
            if candidate.is_file():
                representations.append(AssetRepresentation("image", path=block.image_path))
        if block.kind == "table" and block.text:
            representation_type = "html" if "<table" in block.text.casefold() else "markdown"
            assessment = assess_representation(representation_type, block.text)
            if assessment.usable:
                representations.append(AssetRepresentation(representation_type, content=block.text))
                if representation_type == "html":
                    # Keep the parser representation for evidence inspection,
                    # but publish only a safe GFM projection.  Complex tables
                    # (row/column spans or ragged rows) intentionally remain
                    # image-only so flattening cannot change their meaning.
                    markdown = table_html_to_markdown(block.text)
                    if markdown:
                        representations.append(AssetRepresentation("markdown", content=markdown))
        if block.kind in {"equation", "formula"} and block.text:
            if assess_representation("latex", block.text).usable:
                representations.append(AssetRepresentation("latex", content=block.text))
        if block.kind in {"image", "figure", "chart", "table", "equation", "formula"}:
            # 即使 representations 为空也建立 AssetRef：素材必须对 Survey/Planner 可见，
            # 缺文件或结构化内容退化时由 AssetDecision 显式记录 omit(no_publishable_representation)，
            # 而不是让素材从系统里静默消失。
            primary_path = next((item.path for item in representations if item.path), "")
            assets.append(AssetRef(f"asset:{block.block_id}", block.kind, primary_path, block.caption, block.block_id, tuple(representations)))
    return assets


def _content(raw: Mapping[str, Any], kind: str) -> str:
    if kind == "table":
        return _text(raw.get("table_body")) or _text(raw.get("content"))
    if kind in {"equation", "formula"}:
        return _text(raw.get("text")) or _text(raw.get("latex"))
    if kind in {"image", "figure", "chart"}:
        return _text(raw.get("content"))
    return _text(raw.get("text")) or _text(raw.get("content"))


def _bbox(value: Any, index: int) -> tuple[float, float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise MaterialError(f"material_invalid_bbox:{index}")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        raise MaterialError(f"material_invalid_bbox:{index}")
    x0, y0, x1, y1 = (float(item) for item in value)
    if x1 < x0 or y1 < y0:
        raise MaterialError(f"material_invalid_bbox:{index}")
    return x0, y0, x1, y1


def _safe_relative(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _joined(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return ""
