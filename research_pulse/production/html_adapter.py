"""arXiv 官方 HTML(LaTeXML)→ NormalizedBlock 适配器(第三路解析)。

设计动机见 docs:parsing 诊断(2026-08)。OCR 双路(MinerU+Docling)的素材质量受
OCR 固有伤(刻度碎块、数学字符错位、<sub> 残留)与双路合并偏置拖累;arXiv 官方
HTML 由 LaTeX 源直转,公式/表格/正文零 OCR,是 arXiv 语料的权威第三路。

约定:
- 纯标准库(html.parser),不引第三方依赖,可在任何环境运行。
- 输出与 normalized_adapter.py 完全相同的 blocks.jsonl schema,下游零改动。
- 显示公式:LaTeXML 有两种版式,都要认——
  a) 正文级 ``<math id="S*.E<no>.m<no>">``(DeepSeek-V3 技术报告);
  b) ``<table class="ltx_eqn_table">`` 公式表,每行一个公式、拆成多个 math 段
     (m1 左侧、m2 自 "=" 起),按行拼接(Mamba 系列论文;可在 figure 内)。
- 行内公式以 ``\\(...\\)`` 原文内嵌回段落文本;math 内部的 MathML 字符文本一律
  丢弃(有 alttext 时),防止正文混入标记字符。
- 表格:figure.ltx_table 内的 ``<table>`` 重建为清洗后 HTML(剥 style,MathML
  单元格换 alttext),另产管道文本;**figure 外的独立数据表也解析**(v2 漏掉,
  模型配置表属此类;LaTeXML 特有的 ltx_eqn_table 公式表与 TOC 过滤,不当数据)。
- 图:``<img class="ltx_graphics">`` 与 ``<object data=*.svg>`` 都算(部分论文用
  SVG object 嵌图);URL = arxiv.org/html/<相对路径>。
- 嵌套 figure(多面板)用栈处理;无图无表的浮动面板(如 algorithm)只记 caption。
- 参考文献(ltx_bibliography)整段跳过——不是教学素材。
- alignment 统一 "aligned"(LaTeXML 即权威源在场);source ref 记 element id。

实现要点(html.parser 的坑):
- ``<tr/>``/``<mstyle/>`` 这类自闭合写法会进 ``handle_startendtag``;通用标签走
  重入 handle_starttag 会破坏 figure/表格状态机(空 <tr/> 会把 row_cells 置空)。
  因此 v3 的事件入口统一经 ``_dispatch_start``,startendtag 只对 img/object/math
  等语义完整的空标签做起停配对,其余忽略(HTML 里 void 元素只有 img/br/hr 等)。
- figcaption 跨嵌套标签,文本经 caption_open 标志积累。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html import escape as escape_html
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ARXIV_HTML_BASE = "https://arxiv.org/html"
HTML_ADAPTER_VERSION = "arxiv-html-v3"
_MIN_BODY_TEXT_BLOCKS = 3
_MIN_BODY_TEXT_CHARS = 800
_MIN_BODY_SECTIONS = 2
_EQ_ID = re.compile(r"\.E\d+(?:\.\d+)?\.m\d+$")     # S2.E21.m1 / S2.E1.1.m1
_EQ_ROW_ID = re.compile(r"\.m\d+$")                  # 方程 key = math id 去掉 .m#
_EQ_CONTAINER_P = re.compile(r"\.E\d+")              # 包着显示公式的 p(S*.E*.p* 形态)
_TOC_HINT = re.compile(r"\.{4,}|\d+\s*$")


def _hash12(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _strip_displaystyle(latex: str) -> str:
    return latex.replace("\\displaystyle", "").strip()


@dataclass
class _TableCapture:
    """Any data table (inside a figure, or standalone in the body)."""

    element_id: str
    html_parts: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    row_cells: list[str] | None = None
    cell_buffer: list[str] | None = None
    cell_tag: str | None = None

    @property
    def rows_count(self) -> int:
        return len(self.rows)

    @property
    def columns_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)


@dataclass
class _FigureCapture:
    element_id: str
    is_table_class: bool = False
    caption: str = ""
    caption_open: bool = False
    caption_buffer: list[str] = field(default_factory=list)
    table: _TableCapture | None = None
    table_emitted: bool = False
    images: list[tuple[str, str]] = field(default_factory=list)  # (src, element_id)
    seen_srcs: set[str] = field(default_factory=set)


class _LaTeXMLExtractor(HTMLParser):
    """Streaming extractor: LaTeXML HTML events → block drafts in document order."""

    def __init__(self, source_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_id = source_id
        self.blocks: list[dict[str, Any]] = []
        self.section_path: list[str] = []
        # text sinks(文本去向:p / figcaption / heading)
        self._p_buffer: list[str] | None = None
        self._p_id: str = ""
        self._heading_level: int | None = None
        self._heading_buffer: list[str] = []
        # 结构状态
        self._figure_stack: list[_FigureCapture] = []
        self._standalone_table: _TableCapture | None = None
        self._eqn_table_depth = 0            # 公式表(ltx_eqn_table),可在 figure 内
        self._eqn_row_key: str = ""
        self._eqn_row_parts: list[str] = []
        self._body_eq_key: str = ""          # 正文级显示公式积累(DeepSeek 版式)
        self._body_eq_parts: list[str] = []
        self._math_alt: str | None = None    # 非 None = 位于 <math> 内
        self._skip_depth = 0                 # ltx_bibliography

    # ------------------------------------------------------------------
    # 事件入口(防重入统一通道)
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self._handle_end(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"img", "object", "math"}:
            self._handle_start(tag, attrs)
            self._handle_end(tag)
        # 其余空元素(tr/, mstyle/ 等)忽略:start 事件由 parser 在真实
        # 起始处发出过;这里若再走 start 会重置行/单元格状态(见 docstring)。

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _classes(self, attrs: Sequence[tuple[str, str | None]]) -> str:
        for key, value in attrs:
            if key == "class" and value:
                return value
        return ""

    def _attr(self, attrs: Sequence[tuple[str, str | None]], name: str) -> str:
        for key, value in attrs:
            if key == name and value is not None:
                return value
        return ""

    @property
    def _figure(self) -> _FigureCapture | None:
        return self._figure_stack[-1] if self._figure_stack else None

    def _active_table(self) -> _TableCapture | None:
        fig = self._figure
        if fig is not None and fig.table is not None:
            return fig.table
        return self._standalone_table

    @staticmethod
    def _close_table_cell(table: _TableCapture) -> None:
        if table.cell_buffer is None:
            return
        if table.row_cells is None:
            table.row_cells = []
        table.row_cells.append(_clean_text("".join(table.cell_buffer)))
        table.cell_buffer = None
        table.html_parts.append(f"</{table.cell_tag or 'td'}>")
        table.cell_tag = None

    @classmethod
    def _close_table_row(cls, table: _TableCapture) -> None:
        cls._close_table_cell(table)
        if table.row_cells is None:
            return
        table.rows.append(list(table.row_cells))
        table.row_cells = None
        table.html_parts.append("</tr>")

    def _append_text(self, value: str) -> None:
        if self._p_buffer is not None:
            self._p_buffer.append(value)
        for fig in self._figure_stack:
            if fig.caption_open:
                fig.caption_buffer.append(value)
        if self._heading_level is not None:
            self._heading_buffer.append(value)

    def _append_table_text(self, value: str) -> None:
        table = self._active_table()
        if table is None:
            return
        if table.cell_buffer is not None:
            table.cell_buffer.append(value)
        table.html_parts.append(escape_html(value, quote=False))

    def _emit(self, kind: str, text: str, *, element_id: str, latex: str | None = None,
              table_html: str | None = None, image_path: str | None = None,
              caption: str | None = None, table_rows: int | None = None,
              table_columns: int | None = None, parse_status: str = "available") -> None:
        if not text.strip():
            return
        self.blocks.append({
            "block_id": f"normalized:{self.source_id}:{kind}:{_hash12(kind, text, latex or '', (table_html or '')[:200])}",
            "kind": kind,
            "text": _clean_text(text),
            "section_path": [item for item in self.section_path if item],
            "page_start": None,
            "page_end": None,
            "bbox": None,
            "caption": caption,
            "latex": latex,
            "table_html": table_html,
            "image_path": image_path,
            "table_rows": table_rows,
            "table_columns": table_columns,
            "sources": [{"parser": "arxiv_html", "locator": f"page.html#{element_id}" if element_id else "page.html"}],
            "alignment": "html_native",
            "parse_status": parse_status,
            "confidence": 1.0,
        })

    def _emit_formula(self, element_id: str, latex: str, parse_status: str = "available") -> None:
        cleaned = _strip_displaystyle(latex)
        self._emit("formula", cleaned, element_id=element_id, latex=cleaned, parse_status=parse_status)

    def _flush_body_eq(self) -> None:
        if self._body_eq_key and self._body_eq_parts:
            self._emit_formula(self._body_eq_key, " ".join(self._body_eq_parts))
        self._body_eq_key = ""
        self._body_eq_parts = []

    # ------------------------------------------------------------------
    # start-tag 主逻辑
    # ------------------------------------------------------------------

    def _handle_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: C901, PLR0912
        classes = self._classes(attrs)
        element_id = self._attr(attrs, "id")

        if self._skip_depth:
            if tag in {"section", "div"}:
                self._skip_depth += 1
            return
        if tag == "section" and "ltx_bibliography" in classes:
            self._skip_depth = 1
            return

        if tag in {"h1", "h2", "h3", "h4"} and "ltx_title" in classes:
            self._heading_level = int(tag[1])
            self._heading_buffer = []
            return

        # ---- 公式表(ltx_eqn_table):接管 tr/math 事件 ----
        if tag == "table" and ("ltx_eqn_table" in classes or "ltx_equationgroup" in classes):
            self._eqn_table_depth += 1
            return
        if self._eqn_table_depth:
            if tag == "tr":
                if _EQ_ID.match(element_id or ""):
                    self._eqn_row_key = _EQ_ROW_ID.sub("", element_id)
                self._eqn_row_parts = []
            elif tag == "math":
                alt = self._attr(attrs, "alttext")
                if alt:
                    self._math_alt = alt
                    if _EQ_ID.search(element_id):
                        if not self._eqn_row_key:
                            self._eqn_row_key = _EQ_ROW_ID.sub("", element_id)
                        self._eqn_row_parts.append(_strip_displaystyle(alt))
                    else:
                        self._append_text(f" \\({_strip_displaystyle(alt)}\\) ")
            return

        # ---- figure ----
        if tag == "figure":
            self._figure_stack.append(_FigureCapture(
                element_id=element_id,
                is_table_class="ltx_table" in classes,
            ))
            return

        if self._figure is not None:
            fig = self._figure
            if tag == "figcaption":
                fig.caption_open = True
                fig.caption_buffer = []
                return
            if tag == "table":
                fig.table = _TableCapture(element_id=element_id)
                fig.table.html_parts.append("<table>")
                return
            if tag in {"img", "object"} and fig.table is None:
                src = self._attr(attrs, "src") or self._attr(attrs, "data")
                if src and "ltx_graphics" in classes and src not in fig.seen_srcs and not src.startswith("data:"):
                    fig.seen_srcs.add(src)
                    fig.images.append((src, element_id))
                return
            if fig.table is not None:
                table = fig.table
                if tag == "tr":
                    self._close_table_row(table)
                    table.row_cells = []
                elif tag in {"td", "th"}:
                    self._close_table_cell(table)
                    if table.row_cells is None:
                        table.row_cells = []
                    table.cell_buffer = []
                    table.cell_tag = tag
                if tag == "math":
                    alt = self._attr(attrs, "alttext")
                    if alt:
                        self._math_alt = alt
                        snippet = f" \\({_strip_displaystyle(alt)}\\) "
                        table.html_parts.append(escape_html(snippet, quote=False))
                        if table.cell_buffer is not None:
                            table.cell_buffer.append(snippet)
                        return
                kept = [(k, v) for k, v in attrs if k != "style" and v is not None]
                table.html_parts.append(f"<{tag}" + "".join(f' {k}="{v}"' for k, v in kept) + ">")
                return
            if tag == "math":
                # 图注等 figure 内非表格位置的行内公式:展开进 caption 文本
                alt = self._attr(attrs, "alttext")
                if alt:
                    self._math_alt = alt
                    self._append_text(f" \\({_strip_displaystyle(alt)}\\) ")
                return
            return

        # ---- 独立数据表(figure 外) ----
        if tag == "table":
            self._standalone_table = _TableCapture(element_id=element_id)
            self._standalone_table.html_parts.append("<table>")
            return
        if self._standalone_table is not None:
            table = self._standalone_table
            if tag == "tr":
                self._close_table_row(table)
                table.row_cells = []
            elif tag in {"td", "th"}:
                self._close_table_cell(table)
                if table.row_cells is None:
                    table.row_cells = []
                table.cell_buffer = []
                table.cell_tag = tag
            if tag == "math":
                alt = self._attr(attrs, "alttext")
                if alt:
                    self._math_alt = alt
                    snippet = f" \\({_strip_displaystyle(alt)}\\) "
                    table.html_parts.append(escape_html(snippet, quote=False))
                    if table.cell_buffer is not None:
                        table.cell_buffer.append(snippet)
                    return
            kept = [(k, v) for k, v in attrs if k != "style" and v is not None]
            table.html_parts.append(f"<{tag}" + "".join(f' {k}="{v}"' for k, v in kept) + ">")
            return

        if tag == "p":
            self._flush_body_eq()
            self._p_buffer = []
            self._p_id = element_id
            return

        if tag == "math":
            alt = self._attr(attrs, "alttext")
            self._math_alt = alt if alt else None
            if alt:
                stripped = _strip_displaystyle(alt)
                if _EQ_ID.search(element_id):
                    key = _EQ_ROW_ID.sub("", element_id)
                    if self._body_eq_key and self._body_eq_key != key:
                        self._flush_body_eq()
                    self._body_eq_key = key
                    self._body_eq_parts.append(stripped)
                else:
                    self._append_text(f" \\({stripped}\\) ")
            return

    # ------------------------------------------------------------------
    # end-tag 主逻辑
    # ------------------------------------------------------------------

    def _handle_end(self, tag: str) -> None:  # noqa: C901, PLR0912
        if self._skip_depth:
            if tag in {"section", "div"}:
                self._skip_depth -= 1
            return

        if tag in {"h1", "h2", "h3", "h4"} and self._heading_level is not None:
            title = _clean_text("".join(self._heading_buffer))
            level = self._heading_level
            self._heading_level = None
            self._heading_buffer = []
            if title:
                if level <= 2:
                    self.section_path = [title]
                elif level == 3:
                    self.section_path = (self.section_path[:1] or [""]) + [title]
                else:
                    base = self.section_path[:2] if len(self.section_path) >= 2 else self.section_path
                    self.section_path = (base or [""]) + [title]
            return

        if tag == "math":
            self._math_alt = None
            return

        if self._eqn_table_depth:
            if tag == "tr" and self._eqn_row_parts:
                status = "available" if self._eqn_row_key else "degraded"
                self._emit_formula(self._eqn_row_key or "eqn-row", " ".join(self._eqn_row_parts), parse_status=status)
                self._eqn_row_parts = []
                self._eqn_row_key = ""
            elif tag == "table":
                self._eqn_table_depth -= 1
            return

        if self._figure is not None:
            fig = self._figure
            if tag == "figcaption":
                fig.caption = _clean_text("".join(fig.caption_buffer))
                fig.caption_open = False
                fig.caption_buffer = []
                return
            if tag == "table" and fig.table is not None:
                self._finish_table(fig.table, figure=fig)
                fig.table = None
                fig.table_emitted = True
                return
            if fig.table is not None:
                table = fig.table
                if tag in {"td", "th"}:
                    self._close_table_cell(table)
                    return
                if tag == "tr":
                    self._close_table_row(table)
                    return
                if tag in {"thead", "tbody", "tfoot"}:
                    self._close_table_row(table)
                table.html_parts.append(f"</{tag}>")
                return
            if tag == "figure":
                self._figure_stack.pop()
                self._flush_figure(fig)
            return

        if self._standalone_table is not None:
            table = self._standalone_table
            if tag in {"td", "th"}:
                self._close_table_cell(table)
                return
            if tag == "tr":
                self._close_table_row(table)
                return
            if tag == "table":
                self._finish_table(table, figure=None)
                self._standalone_table = None
            else:
                if tag in {"thead", "tbody", "tfoot"}:
                    self._close_table_row(table)
                table.html_parts.append(f"</{tag}>")
            return

        if tag == "p" and self._p_buffer is not None:
            self._flush_body_eq()
            text = _clean_text("".join(self._p_buffer))
            is_equation_container = bool(_EQ_CONTAINER_P.search(self._p_id))
            element_id = self._p_id or "p"
            self._p_buffer = None
            self._p_id = ""
            if text and not is_equation_container:
                self._emit("text", text, element_id=element_id)
            return

    # ------------------------------------------------------------------
    # data 与 flush
    # ------------------------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._math_alt is not None:
            return  # MathML 字符文本一律丢弃,防正文混入标记字符
        if self._active_table() is not None:
            self._append_table_text(data)
            return
        self._append_text(data)

    def _finish_table(self, table: _TableCapture, *, figure: _FigureCapture | None) -> None:
        self._close_table_row(table)
        table.html_parts.append("</table>")
        html_text = "".join(table.html_parts).strip()
        rows_count = table.rows_count
        columns_count = table.columns_count
        grid = " | ".join(" | ".join(cell for cell in row if cell) for row in table.rows)
        caption = figure.caption if figure else None
        is_toc = caption is None and rows_count > 5 and columns_count <= 3 and all(
            cell == "" or _TOC_HINT.search(cell) for row in table.rows[:5] for cell in row[:1]
        )
        if is_toc:
            return  # 目录/页码表:教学素材之外的噪声
        self.blocks.append({
            "block_id": f"normalized:{self.source_id}:table:{_hash12('table', grid, html_text[:200])}",
            "kind": "table",
            "text": _clean_text(grid) or (caption or ""),
            "section_path": [item for item in self.section_path if item],
            "page_start": None,
            "page_end": None,
            "bbox": None,
            "caption": _clean_text(caption) if caption else None,
            "latex": None,
            "table_html": html_text,
            "image_path": None,
            "table_rows": rows_count,
            "table_columns": columns_count,
            "sources": [{"parser": "arxiv_html", "locator": f"page.html#{table.element_id}" if table.element_id else "page.html"}],
            "alignment": "html_native",
            "parse_status": "available",
            "confidence": 1.0,
        })

    def _flush_figure(self, fig: _FigureCapture) -> None:
        caption = fig.caption
        if fig.table is not None:  # 未闭合的表(畸形):先落
            self._finish_table(fig.table, figure=fig)
            fig.table = None
            fig.table_emitted = True
        for src, element_id in fig.images:
            url = f"{ARXIV_HTML_BASE}/{src.lstrip('/')}"
            text = caption or src
            self._emit_figure(fig, url, text, element_id)
        if not fig.images and not fig.table_emitted and caption:
            self._emit("text", caption, element_id=fig.element_id or "figure")

    def _emit_figure(self, fig: _FigureCapture, url: str, text: str, element_id: str) -> None:
        self.blocks.append({
            "block_id": f"normalized:{self.source_id}:figure:{_hash12('figure', url, text)}",
            "kind": "figure",
            "text": _clean_text(text),
            "section_path": [item for item in self.section_path if item],
            "page_start": None,
            "page_end": None,
            "bbox": None,
            "caption": _clean_text(text) or None,
            "latex": None,
            "table_html": None,
            "image_path": url,
            "table_rows": None,
            "table_columns": None,
            "sources": [{"parser": "arxiv_html", "locator": f"page.html#{element_id}" if element_id else "page.html"}],
            "alignment": "html_native",
            "parse_status": "available",
            "confidence": 1.0,
        })


def parse_arxiv_html(source_id: str, html_text: str) -> tuple[dict[str, Any], ...]:
    """Parse one arXiv HTML page into NormalizedBlock-shaped dicts."""
    extractor = _LaTeXMLExtractor(source_id)
    extractor.feed(html_text)
    extractor.close()
    extractor._flush_body_eq()
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for block in extractor.blocks:
        if block["block_id"] in seen:
            continue
        seen.add(block["block_id"])
        unique.append(block)
    return tuple(unique)


def _validate_html_material(html_text: str, blocks: Sequence[Mapping[str, Any]]) -> None:
    """Reject a structurally valid but clearly incomplete full-paper extraction.

    This validation lives inside the HTML adapter so every caller gets the same
    fallback semantics.  It intentionally uses only broad full-paper signals;
    detailed table/formula/image quality remains AssetPreparation's job.
    """

    text_blocks = [
        block for block in blocks
        if block.get("kind") == "text" and str(block.get("text") or "").strip()
    ]
    body_chars = sum(len(str(block.get("text") or "").strip()) for block in text_blocks)
    sections = {
        tuple(str(part) for part in block.get("section_path", ()) if str(part).strip())
        for block in text_blocks
    }
    sections.discard(())
    raw_paragraphs = len(re.findall(
        r"<p\b[^>]*class=[\"'][^\"']*\bltx_p\b[^\"']*[\"']",
        html_text,
        flags=re.IGNORECASE,
    ))
    reasons: list[str] = []
    if len(text_blocks) < _MIN_BODY_TEXT_BLOCKS:
        reasons.append("too_few_body_blocks")
    if body_chars < _MIN_BODY_TEXT_CHARS:
        reasons.append("body_text_too_short")
    if len(sections) < _MIN_BODY_SECTIONS:
        reasons.append("missing_section_structure")
    if raw_paragraphs >= 6 and len(text_blocks) / raw_paragraphs < 0.5:
        reasons.append("low_paragraph_extraction_coverage")
    if reasons:
        raise ValueError("html material rejected: " + ",".join(reasons))


def build_manifest(source_id: str, source_url: str, blocks: Sequence[Mapping[str, Any]],
                   html_sha256: str, *, images_fetched: int = 0) -> dict[str, Any]:
    kind_counts: dict[str, int] = {}
    alignment_counts = {"aligned": 0, "mineru_only": 0, "docling_only": 0, "html_native": 0}
    for block in blocks:
        kind_counts[block["kind"]] = kind_counts.get(block["kind"], 0) + 1
        alignment_counts[block["alignment"]] = alignment_counts.get(block["alignment"], 0) + 1
    return {
        "schema_version": "normalized-blocks-v1",
        "source_id": source_id,
        "source_url": source_url,
        "parser": "arxiv_html",
        "parser_version": HTML_ADAPTER_VERSION,
        "block_count": len(blocks),
        "kind_counts": kind_counts,
        "alignment_counts": alignment_counts,
        "input_hashes": {"arxiv_html": html_sha256},
        "available_assets": {"figures_fetched": images_fetched},
        "complete": True,
    }


def _write_blocks(blocks_path: Path, blocks: Sequence[Mapping[str, Any]]) -> None:
    with blocks_path.open("w", encoding="utf-8") as handle:
        for block in blocks:
            handle.write(json.dumps(block, ensure_ascii=False) + "\n")


def _default_image_fetcher(url: str, target: Path, timeout_seconds: int) -> None:
    request = Request(url, headers={"User-Agent": "research-pulse/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        data = response.read(32 * 1024 * 1024)
    if not data:
        raise ValueError("empty image body")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def fetch_block_images(blocks: list[dict[str, Any]], output_dir: Path, *, timeout_seconds: int = 60,
                       fetcher: Callable[[str, Path, int], None] | None = None) -> int:
    """把 figure 块的 image_path 从 arXiv 外链改为本地 ``images/`` 相对路径。

    单图下载失败 → 该块 ``image_path=None``、``parse_status=degraded``(不阻断整篇);
    返回成功下载的图片数。``load_complete_normalized`` 要求被引用的图片文件真实存在,
    所以落盘是 HTML 路接入读取链的必要步骤。
    """
    images_dir = output_dir / "images"
    fetch = fetcher or _default_image_fetcher
    fetched = 0
    for block in blocks:
        url = str(block.get("image_path") or "")
        if not url.startswith("http"):
            continue
        name = url.rsplit("/", 1)[-1] or f"img-{_hash12(url)}.bin"
        target = images_dir / name
        try:
            fetch(url, target, timeout_seconds)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            block["image_path"] = None
            block["parse_status"] = "degraded"
            continue
        local = f"images/{name}"
        block["image_path"] = local
        for source in block.get("sources", ()):  # locator 附加本地落盘位置,可溯源
            source["locator"] = f"{source.get('locator', 'page.html')};{local}"
        fetched += 1
    return fetched


def convert(source_id: str, source_url: str, html_path: Path, output_dir: Path, *,
            fetch_images: bool = False, timeout_seconds: int = 60,
            image_fetcher: Callable[[str, Path, int], None] | None = None) -> Path:
    """Full conversion: page.html → <output_dir>/{blocks.jsonl, manifest.json}. Returns blocks path."""
    raw = html_path.read_bytes()
    html_text = raw.decode("utf-8", errors="replace")
    blocks = list(parse_arxiv_html(source_id, html_text))
    _validate_html_material(html_text, blocks)
    output_dir.mkdir(parents=True, exist_ok=True)
    blocks_path = output_dir / "blocks.jsonl"
    images_fetched = 0
    if fetch_images:
        images_fetched = fetch_block_images(blocks, output_dir, timeout_seconds=timeout_seconds,
                                            fetcher=image_fetcher)
    manifest = build_manifest(source_id, source_url, blocks, hashlib.sha256(raw).hexdigest(),
                              images_fetched=images_fetched)
    page_path = output_dir / "page.html"
    manifest_path = output_dir / "manifest.json"
    page_tmp = output_dir / "page.html.tmp"
    blocks_tmp = output_dir / "blocks.jsonl.tmp"
    manifest_tmp = output_dir / "manifest.json.tmp"
    try:
        page_tmp.write_bytes(raw)
        _write_blocks(blocks_tmp, blocks)
        manifest_tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # manifest 是提交标记：先撤掉旧标记，再依次提交原文、块和新标记。
        manifest_path.unlink(missing_ok=True)
        page_tmp.replace(page_path)
        blocks_tmp.replace(blocks_path)
        manifest_tmp.replace(manifest_path)
    finally:
        page_tmp.unlink(missing_ok=True)
        blocks_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
    return blocks_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="arXiv HTML → blocks.jsonl")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    blocks_path = convert(args.source_id, args.source_url, args.html, args.output_dir)
    print(f"wrote {blocks_path}")


if __name__ == "__main__":
    main()
