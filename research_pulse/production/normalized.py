"""Parser-neutral normalization for MinerU and Docling extraction output.

The raw parser outputs stay in the source cache.  This module only creates a
small, deterministic intermediate representation that can later be adapted to
the existing transient ``EvidenceCandidate`` contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from hashlib import sha256
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence
import json
import re
import argparse


NormalizedKind = Literal["text", "formula", "table", "figure", "caption"]
Alignment = Literal["aligned", "mineru_only", "docling_only", "html_native"]
ParseStatus = Literal["available", "degraded", "unparsed"]


@dataclass(frozen=True)
class SourceRef:
    """A locator into an immutable parser output file."""

    parser: Literal["mineru", "mineru_api", "docling", "arxiv_html"]
    locator: str

    def to_dict(self) -> dict[str, str]:
        return {"parser": self.parser, "locator": self.locator}


@dataclass(frozen=True)
class NormalizedBlock:
    """One aligned or single-source block used by later reading stages."""

    block_id: str
    kind: NormalizedKind
    text: str
    section_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    caption: str | None = None
    latex: str | None = None
    table_html: str | None = None
    image_path: str | None = None
    table_rows: int | None = None
    table_columns: int | None = None
    sources: tuple[SourceRef, ...] = ()
    alignment: Alignment = "mineru_only"
    parse_status: ParseStatus = "available"
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.block_id.strip() or not self.text.strip():
            raise ValueError("NormalizedBlock requires a non-empty ID and text.")
        if self.kind not in {"text", "formula", "table", "figure", "caption"}:
            raise ValueError("NormalizedBlock kind is invalid.")
        if self.alignment not in {"aligned", "mineru_only", "docling_only", "html_native"}:
            raise ValueError("NormalizedBlock alignment is invalid.")
        if not self.sources:
            raise ValueError("NormalizedBlock requires at least one parser source.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("NormalizedBlock confidence must be between 0 and 1.")
        if self.page_end is not None and (self.page_start is None or self.page_end < self.page_start):
            raise ValueError("NormalizedBlock page range is invalid.")
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("NormalizedBlock bbox must contain four coordinates.")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "block_id": self.block_id,
            "kind": self.kind,
            "text": self.text,
            "section_path": list(self.section_path),
            "page_start": self.page_start,
            "page_end": self.page_end,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "caption": self.caption,
            "latex": self.latex,
            "table_html": self.table_html,
            "image_path": self.image_path,
            "table_rows": self.table_rows,
            "table_columns": self.table_columns,
            "sources": [source.to_dict() for source in self.sources],
            "alignment": self.alignment,
            "parse_status": self.parse_status,
            "confidence": round(self.confidence, 4),
        }
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizedBlock":
        sources = tuple(
            SourceRef(parser=item["parser"], locator=item["locator"])
            for item in payload.get("sources", [])
            if isinstance(item, dict) and item.get("parser") in {"mineru", "mineru_api", "docling", "arxiv_html"} and isinstance(item.get("locator"), str)
        )
        bbox = payload.get("bbox")
        return cls(
            block_id=str(payload["block_id"]),
            kind=payload["kind"],
            text=str(payload["text"]),
            section_path=tuple(str(item) for item in payload.get("section_path", [])),
            page_start=payload.get("page_start"),
            page_end=payload.get("page_end"),
            bbox=tuple(float(item) for item in bbox) if isinstance(bbox, list) and len(bbox) == 4 else None,
            caption=payload.get("caption"),
            latex=payload.get("latex"),
            table_html=payload.get("table_html"),
            image_path=payload.get("image_path"),
            table_rows=payload.get("table_rows"),
            table_columns=payload.get("table_columns"),
            sources=sources,
            alignment=payload.get("alignment", "mineru_only"),
            parse_status=payload.get("parse_status", "available"),
            confidence=float(payload.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class NormalizedDocument:
    source_id: str
    source_url: str
    blocks: tuple[NormalizedBlock, ...]
    input_hashes: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    parser: str | None = None
    parser_version: str | None = None

    @property
    def alignment_counts(self) -> dict[str, int]:
        return {
            key: sum(block.alignment == key for block in self.blocks)
            for key in ("aligned", "mineru_only", "docling_only", "html_native")
        }

    @property
    def kind_counts(self) -> dict[str, int]:
        return {kind: sum(block.kind == kind for block in self.blocks) for kind in ("text", "formula", "table", "figure", "caption")}

    def write(self, output_dir: Path) -> tuple[Path, Path]:
        """Commit JSONL blocks first and the completeness manifest last."""

        output_dir.mkdir(parents=True, exist_ok=True)
        blocks_path = output_dir / "blocks.jsonl"
        manifest_path = output_dir / "manifest.json"
        blocks_tmp = output_dir / "blocks.jsonl.tmp"
        manifest_tmp = output_dir / "manifest.json.tmp"
        manifest = {
            "schema_version": 1,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "input_hashes": dict(self.input_hashes),
            "block_count": len(self.blocks),
            "alignment_counts": self.alignment_counts,
            "kind_counts": self.kind_counts,
            "warnings": list(self.warnings),
            "complete": True,
            "persistence": "normalized metadata and bounded block projections; raw PDF/parser outputs remain in source cache",
        }
        if self.parser:
            manifest["parser"] = self.parser
        if self.parser_version:
            manifest["parser_version"] = self.parser_version
        try:
            blocks_tmp.write_text(
                "".join(json.dumps(block.to_dict(), ensure_ascii=False, sort_keys=True) + "\n" for block in self.blocks),
                encoding="utf-8",
            )
            manifest_tmp.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # The manifest is the commit marker.  Removing an older marker
            # before replacing blocks makes every interrupted update invalid
            # instead of allowing a stale manifest to bless new partial data.
            manifest_path.unlink(missing_ok=True)
            blocks_tmp.replace(blocks_path)
            manifest_tmp.replace(manifest_path)
        finally:
            blocks_tmp.unlink(missing_ok=True)
            manifest_tmp.unlink(missing_ok=True)
        return blocks_path, manifest_path


@dataclass(frozen=True)
class _RawBlock:
    parser: Literal["mineru", "docling"]
    kind: NormalizedKind
    text: str
    section_path: tuple[str, ...]
    page: int | None
    bbox: tuple[float, float, float, float] | None
    caption: str | None
    latex: str | None
    table_html: str | None
    image_path: str | None
    table_rows: int | None
    table_columns: int | None
    locator: str
    parse_status: ParseStatus = "available"


def normalize_extractions(
    *,
    source_id: str,
    source_url: str,
    mineru_content_list: Path,
    docling_document: Path,
) -> NormalizedDocument:
    """Load both JSON outputs and produce deterministic normalized blocks."""

    mineru = _read_json(mineru_content_list)
    docling = _read_json(docling_document)
    mineru_blocks = _mineru_blocks(mineru)
    docling_blocks = _docling_blocks(docling)
    merged = _align(mineru_blocks, docling_blocks)
    return NormalizedDocument(
        source_id=source_id,
        source_url=source_url,
        blocks=_assign_ids(source_id, merged),
        input_hashes={
            "mineru_content_list": _file_sha256(mineru_content_list),
            "docling_document": _file_sha256(docling_document),
        },
    )


def load_normalized_jsonl(path: Path) -> tuple[NormalizedBlock, ...]:
    """Load only normalized projections; raw parser files are never consulted."""

    if not path.is_file():
        raise FileNotFoundError(path)
    blocks: list[NormalizedBlock] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("block must be an object")
            blocks.append(NormalizedBlock.from_dict(payload))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid normalized block at line {line_number}.") from error
    if not blocks:
        raise ValueError("Normalized JSONL contains no blocks.")
    return tuple(blocks)


def load_complete_normalized(
    normalized_dir: Path,
    *,
    expected_source_id: str,
    image_roots: Sequence[Path] = (),
) -> tuple[NormalizedBlock, ...]:
    """Load a normalized cache only after its committed manifest is coherent.

    Schema-v1 caches created before the explicit ``complete`` marker remain
    readable when their manifest, block count, source identity, input hashes,
    and referenced images are all intact.  A present ``complete=false`` marker
    is always rejected.
    """

    normalized_dir = Path(normalized_dir)
    blocks_path = normalized_dir / "blocks.jsonl"
    manifest_path = normalized_dir / "manifest.json"
    if not blocks_path.is_file():
        raise ValueError("normalized blocks.jsonl is missing")
    if not manifest_path.is_file():
        raise ValueError("normalized manifest.json is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("normalized manifest.json is invalid JSON") from error
    if not isinstance(manifest, dict):
        raise ValueError("normalized manifest.json must be an object")
    if manifest.get("complete") is False:
        raise ValueError("normalized manifest marks the cache incomplete")
    if manifest.get("source_id") != expected_source_id:
        raise ValueError("normalized manifest source_id does not match the requested paper")
    input_hashes = manifest.get("input_hashes")
    if not isinstance(input_hashes, dict):
        raise ValueError("normalized manifest input_hashes are incomplete")
    manifest_parser = manifest.get("parser")
    if manifest_parser == "arxiv_html":
        required_hashes = ("arxiv_html",)
    elif manifest_parser == "mineru_api":
        required_hashes = ("mineru_content_list",)
    else:
        required_hashes = ("mineru_content_list", "docling_document")
    if not all(
        isinstance(input_hashes.get(name), str) and input_hashes[name].strip()
        for name in required_hashes
    ):
        raise ValueError("normalized manifest input_hashes are incomplete")

    blocks = load_normalized_jsonl(blocks_path)
    html_sources_present = any(
        source.parser == "arxiv_html"
        for block in blocks
        for source in block.sources
    )
    if manifest_parser == "arxiv_html":
        parser_version = manifest.get("parser_version")
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("normalized HTML manifest parser_version is missing")
        page_path = normalized_dir / "page.html"
        if not page_path.is_file():
            raise ValueError("normalized HTML source page.html is missing")
        if _file_sha256(page_path) != input_hashes["arxiv_html"]:
            raise ValueError("normalized HTML source hash does not match manifest")
        if any(
            not block.sources or any(source.parser != "arxiv_html" for source in block.sources)
            for block in blocks
        ):
            raise ValueError("normalized HTML block sources do not match manifest parser")
    elif manifest_parser == "mineru_api":
        parser_version = manifest.get("parser_version")
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("normalized MinerU API manifest parser_version is missing")
        content_path = normalized_dir / "content_list.json"
        if not content_path.is_file():
            raise ValueError("normalized MinerU API content_list.json is missing")
        if _file_sha256(content_path) != input_hashes["mineru_content_list"]:
            raise ValueError("normalized MinerU API content hash does not match manifest")
        if any(
            not block.sources or any(source.parser != "mineru_api" for source in block.sources)
            for block in blocks
        ):
            raise ValueError("normalized MinerU API block sources do not match manifest parser")
    elif html_sources_present:
        raise ValueError("normalized block sources do not match manifest parser")
    declared_count = manifest.get("block_count")
    if not isinstance(declared_count, int) or declared_count != len(blocks):
        raise ValueError(
            f"normalized manifest block_count={declared_count!r} does not match {len(blocks)} blocks"
        )
    roots = tuple(Path(root).resolve() for root in image_roots) or (normalized_dir.resolve(),)
    for block in blocks:
        if not block.image_path:
            continue
        relative = Path(block.image_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"normalized image_path is unsafe: {block.image_path!r}")
        if not any((root / relative).is_file() for root in roots):
            raise ValueError(f"normalized referenced image is missing: {block.image_path}")
    return blocks


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mineru_blocks(payload: Any) -> list[_RawBlock]:
    pages = payload if isinstance(payload, list) else []
    blocks: list[_RawBlock] = []
    section: tuple[str, ...] = ()
    for page_index, page in enumerate(pages, start=1):
        items = page if isinstance(page, list) else page.get("items", []) if isinstance(page, dict) else []
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            kind_name = str(item.get("type", "")).casefold()
            content = item.get("content")
            if kind_name == "title":
                title = _content_text(content)
                if title:
                    section = (title,)
            kind: NormalizedKind | None
            if kind_name in {"equation", "interline_equation", "inline_equation", "equation_interline", "equation_inline"}:
                kind = "formula"
            elif kind_name == "table":
                kind = "table"
            elif kind_name in {"image", "chart", "figure"}:
                kind = "figure"
            elif kind_name in {"caption", "image_caption", "table_caption"}:
                kind = "caption"
            elif kind_name in {"text", "paragraph", "title", "abstract", "list"}:
                kind = "text"
            else:
                continue
            if kind == "table":
                html = _string_value(content, "html")
                caption = _caption_from(content, "table_caption")
                text = _html_table_projection(html) if html else caption or ""
                rows, columns = _table_dimensions(html)
            elif kind == "figure":
                key = "chart_caption" if kind_name == "chart" else "image_caption"
                caption = _caption_from(content, key)
                text = caption or _content_text(content)
                html = None
                rows = columns = None
            elif kind == "formula":
                latex = _formula_text(content)
                text = latex
                caption = None
                html = None
                rows = columns = None
            else:
                text = _content_text(content)
                caption = None
                latex = None
                html = None
                rows = columns = None
            if not text.strip():
                continue
            image_path = _string_value(content, "image_source", "path") if isinstance(content, dict) else None
            blocks.append(
                _RawBlock(
                    parser="mineru",
                    kind=kind,
                    text=_clean(text),
                    section_path=section,
                    page=page_index,
                    bbox=_bbox(item.get("bbox")),
                    caption=_clean(caption) if caption else None,
                    latex=_clean(latex) if isinstance(latex, str) else None,
                    table_html=html,
                    image_path=image_path,
                    table_rows=rows,
                    table_columns=columns,
                    locator=f"mineru:content_list_v2.json#/pages/{page_index - 1}/items/{item_index}",
                    parse_status="available",
                )
            )
    return blocks


def _docling_blocks(payload: Any) -> list[_RawBlock]:
    if not isinstance(payload, dict):
        return []
    heights = _page_heights(payload.get("pages"))
    texts = payload.get("texts", [])
    object_text_refs = _object_text_refs(payload)
    section_by_page: dict[int, tuple[str, ...]] = {}
    current: tuple[str, ...] = ()
    blocks: list[_RawBlock] = []
    if isinstance(texts, list):
        for index, item in enumerate(texts):
            if not isinstance(item, dict):
                continue
            page, bbox = _docling_locator(item, heights)
            label = str(item.get("label", "")).casefold()
            self_ref = item.get("self_ref")
            value = _clean(item.get("text") or "")
            original = _clean(item.get("orig") or "")
            if label in {"section_header", "heading", "title"} and value:
                current = (value,)
            if page is not None:
                section_by_page[page] = current
            if label in {"page_header", "page_footer", "footnote", "page_footnote"} or (self_ref in object_text_refs and label not in {"section_header", "heading", "title"}) or not (value or original):
                continue
            if label == "formula":
                blocks.append(_RawBlock(
                    parser="docling", kind="formula", text=value or original, section_path=current,
                    page=page, bbox=bbox, caption=None, latex=original or None,
                    table_html=None, image_path=None, table_rows=None, table_columns=None,
                    locator=f"docling:document.json#/texts/{index}",
                    parse_status="available" if value else "unparsed",
                ))
            else:
                blocks.append(_RawBlock(
                    parser="docling", kind="text", text=value or original, section_path=current,
                    page=page, bbox=bbox, caption=None, latex=None, table_html=None,
                    image_path=None, table_rows=None, table_columns=None,
                    locator=f"docling:document.json#/texts/{index}",
                ))
    for index, item in enumerate(payload.get("tables", []) if isinstance(payload.get("tables"), list) else []):
        if not isinstance(item, dict):
            continue
        page, bbox = _docling_locator(item, heights)
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        cells = data.get("table_cells") if isinstance(data.get("table_cells"), list) else []
        # Docling end offsets are exclusive (a 2x2 grid ends at offset 2).
        rows = max((int(cell.get("end_row_offset_idx", 0)) for cell in cells if isinstance(cell, dict)), default=0)
        columns = max((int(cell.get("end_col_offset_idx", 0)) for cell in cells if isinstance(cell, dict)), default=0)
        grid = [["" for _ in range(max(columns, 0))] for _ in range(max(rows, 0))]
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            row = int(cell.get("start_row_offset_idx", 0)); col = int(cell.get("start_col_offset_idx", 0))
            if 0 <= row < rows and 0 <= col < columns:
                grid[row][col] = _clean(cell.get("text") or "")
        caption = _resolve_refs(item.get("captions"), texts)
        text = _grid_text(grid) or caption
        if text:
            blocks.append(_RawBlock("docling", "table", text, section_by_page.get(page or -1, ()), page, bbox, caption or None, None, None, None, rows or None, columns or None, f"docling:document.json#/tables/{index}"))
    for index, item in enumerate(payload.get("pictures", []) if isinstance(payload.get("pictures"), list) else []):
        if not isinstance(item, dict):
            continue
        page, bbox = _docling_locator(item, heights)
        caption = _resolve_refs(item.get("captions"), texts)
        if caption:
            blocks.append(_RawBlock("docling", "figure", caption, section_by_page.get(page or -1, ()), page, bbox, caption, None, None, None, None, None, f"docling:document.json#/pictures/{index}"))
    return blocks


def _object_text_refs(payload: Mapping[str, Any]) -> set[str]:
    """Return text refs already represented by a table/picture object.

    Docling exposes figure labels and table captions both as object children and
    as entries in ``texts``.  Keeping both would create duplicate normalized
    blocks and make downstream reading over-count evidence.
    """

    refs: set[str] = set()
    for key in ("tables", "pictures"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            for child_key in ("children", "captions", "references", "footnotes"):
                children = item.get(child_key)
                if not isinstance(children, list):
                    continue
                refs.update(child.get("$ref") for child in children if isinstance(child, dict) and isinstance(child.get("$ref"), str))
    return refs


def _align(mineru: Sequence[_RawBlock], docling: Sequence[_RawBlock]) -> list[tuple[_RawBlock, _RawBlock | None, float]]:
    candidates: list[tuple[float, int, int]] = []
    for left_index, left in enumerate(mineru):
        for right_index, right in enumerate(docling):
            if left.kind != right.kind or left.page is None or right.page is None or abs(left.page - right.page) > 1:
                continue
            similarity = _similarity(left, right)
            overlap = _bbox_iou(left.bbox, right.bbox)
            score = max(similarity, overlap * 0.75 + similarity * 0.25)
            threshold = 0.30 if left.kind in {"table", "figure", "formula"} else 0.42
            if score >= threshold:
                candidates.append((score, left_index, right_index))
    candidates.sort(key=lambda value: (-value[0], value[1], value[2]))
    used_left: set[int] = set(); used_right: set[int] = set(); matches: list[tuple[_RawBlock, _RawBlock | None, float]] = []
    for score, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index); used_right.add(right_index)
        matches.append((mineru[left_index], docling[right_index], score))
    for index, block in enumerate(mineru):
        if index not in used_left:
            matches.append((block, None, 0.55))
    for index, block in enumerate(docling):
        if index not in used_right:
            matches.append((block, None, 0.5))
    return sorted(matches, key=lambda item: (item[0].page or item[1].page or 0, item[0].bbox[1] if item[0].bbox else 0, item[0].locator))


def _assign_ids(source_id: str, merged: Iterable[tuple[_RawBlock, _RawBlock | None, float]]) -> tuple[NormalizedBlock, ...]:
    result: list[NormalizedBlock] = []
    seen: dict[str, int] = {}
    for left, right, score in merged:
        aligned = right is not None and left.parser != right.parser
        mineru = left if left.parser == "mineru" else right
        docling = right if right is not None and right.parser == "docling" else None
        primary = mineru or left
        sources = tuple(SourceRef(block.parser, block.locator) for block in (left, right) if block is not None)
        identity = "|".join(source.locator for source in sources) + "|" + primary.kind
        digest = sha256(identity.encode("utf-8")).hexdigest()[:12]
        occurrence = seen.get(digest, 0); seen[digest] = occurrence + 1
        block_id = f"normalized:{source_id}:{primary.kind}:{digest}" + (f"-{occurrence}" if occurrence else "")
        text = primary.text
        if primary.kind == "formula" and mineru is None and docling is not None:
            text = docling.text
        result.append(NormalizedBlock(
            block_id=block_id,
            kind=primary.kind,
            text=text,
            section_path=primary.section_path or (docling.section_path if docling else ()),
            page_start=primary.page or (docling.page if docling else None),
            page_end=primary.page or (docling.page if docling else None),
            bbox=(docling.bbox if docling and docling.bbox else primary.bbox),
            caption=primary.caption or (docling.caption if docling else None),
            latex=mineru.latex if mineru else (docling.latex if docling else None),
            table_html=mineru.table_html if mineru else None,
            image_path=mineru.image_path if mineru else None,
            table_rows=(docling.table_rows if docling and docling.table_rows else primary.table_rows),
            table_columns=(docling.table_columns if docling and docling.table_columns else primary.table_columns),
            sources=sources,
            alignment="aligned" if aligned else ("mineru_only" if left.parser == "mineru" else "docling_only"),
            parse_status=("available" if mineru and mineru.parse_status == "available" else primary.parse_status),
            confidence=min(1.0, score if aligned else 0.55),
        ))
    return tuple(result)


def _similarity(left: _RawBlock, right: _RawBlock) -> float:
    a = _norm_compare(left.caption or left.text)
    b = _norm_compare(right.caption or right.text)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    left_tokens, right_tokens = set(a.split()), set(b.split())
    jaccard = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return max(sequence, jaccard)


def _norm_compare(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff%./+-]+", " ", value.casefold())
    return " ".join(value.split())


def _bbox_iou(left: tuple[float, float, float, float] | None, right: tuple[float, float, float, float] | None) -> float:
    if not left or not right:
        return 0.0
    lx1, ly1, lx2, ly2 = left; rx1, ry1, rx2, ry2 = right
    ix1, iy1, ix2, iy2 = max(lx1, rx1), max(ly1, ry1), min(lx2, rx2), min(ly2, ry2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1) + max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1) - intersection
    return intersection / union if union else 0.0


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_content_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("title_content", "paragraph_content", "text", "content", "latex"):
            if key in value:
                return _content_text(value[key])
    return ""


def _formula_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("latex", "math_content", "formula", "content"):
            if key in value:
                found = _content_text(value[key])
                if found:
                    return found
    return _content_text(value)


def _caption_from(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    return _content_text(value.get(key)) or None


def _string_value(value: Any, *keys: str) -> str | None:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) and current.strip() else None


def _html_text(value: str | None) -> str:
    if not value:
        return ""
    return _clean(unescape(re.sub(r"<[^>]+>", " ", value)))


def _html_table_projection(value: str | None) -> str:
    """Keep a complete bounded table projection for the existing evidence gate."""

    if not value:
        return ""
    rows: list[list[str]] = []
    for raw_row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", value, re.I | re.S):
        cells = [_clean(unescape(re.sub(r"<[^>]+>", " ", raw))) for raw in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", raw_row, re.I | re.S)]
        if cells:
            rows.append(cells)
    if not rows:
        return _html_text(value)
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in normalized]
    if len(lines) > 1:
        lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(lines)


def _table_dimensions(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    rows = len(re.findall(r"<tr\b", value, re.I))
    columns = max((len(re.findall(r"<t[dh]\b", row, re.I)) for row in re.findall(r"<tr\b[^>]*>.*?</tr>", value, re.I | re.S)), default=0)
    return rows or None, columns or None


def _grid_text(grid: Sequence[Sequence[str]]) -> str:
    if not grid:
        return ""
    return "\n".join("| " + " | ".join(row) + " |" for row in grid)


def _resolve_refs(refs: Any, texts: Any) -> str:
    if not isinstance(refs, list) or not isinstance(texts, list):
        return ""
    by_ref = {item.get("self_ref"): item for item in texts if isinstance(item, dict)}
    values: list[str] = []
    for ref in refs:
        key = ref.get("$ref") if isinstance(ref, dict) else None
        item = by_ref.get(key)
        if item:
            values.append(_clean(item.get("text") or item.get("orig") or ""))
    return " ".join(value for value in values if value)


def _docling_locator(item: Mapping[str, Any], heights: Mapping[int, float]) -> tuple[int | None, tuple[float, float, float, float] | None]:
    prov = item.get("prov")
    first = prov[0] if isinstance(prov, list) and prov and isinstance(prov[0], dict) else None
    if not first:
        return None, None
    page = first.get("page_no") if isinstance(first.get("page_no"), int) else None
    raw = first.get("bbox") if isinstance(first.get("bbox"), dict) else None
    if not raw:
        return page, None
    bbox = _bbox([raw.get("l"), raw.get("t"), raw.get("r"), raw.get("b")])
    if bbox and str(raw.get("coord_origin", "")).upper() == "BOTTOMLEFT" and page in heights:
        x1, y1, x2, y2 = bbox
        height = heights[page]
        bbox = (x1, height - y2, x2, height - y1)
    return page, bbox


def _page_heights(pages: Any) -> dict[int, float]:
    result: dict[int, float] = {}
    values = pages.values() if isinstance(pages, dict) else pages if isinstance(pages, list) else []
    for index, page in enumerate(values, start=1):
        if not isinstance(page, dict):
            continue
        size = page.get("size") if isinstance(page.get("size"), dict) else page
        height = size.get("height") if isinstance(size, dict) else None
        if isinstance(height, (int, float)):
            result[index] = float(height)
    return result


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(isinstance(item, (int, float)) for item in value):
        return None
    return tuple(float(item) for item in value)  # type: ignore[return-value]


def _clean(value: str | None) -> str:
    return " ".join(str(value or "").replace("\u00ad", "").split())


def main(argv: Sequence[str] | None = None) -> int:
    """Run normalization without importing the application or parser SDKs."""

    parser = argparse.ArgumentParser(description="Normalize MinerU and Docling JSON outputs.")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--mineru-content-list", type=Path, required=True)
    parser.add_argument("--docling-document", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    document = normalize_extractions(
        source_id=args.source_id,
        source_url=args.source_url,
        mineru_content_list=args.mineru_content_list,
        docling_document=args.docling_document,
    )
    blocks_path, manifest_path = document.write(args.output_dir)
    print(json.dumps({
        "blocks": len(document.blocks),
        "alignment_counts": document.alignment_counts,
        "kind_counts": document.kind_counts,
        "blocks_path": str(blocks_path),
        "manifest_path": str(manifest_path),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
