"""确定性素材表示质量判定（Traceable Reading 链路）。

继承 pedagogical 链路 AssetPreparation（docs/asset-preparation-design.md）的 P0 质量教训：
"能渲染"和"内容可信"是两个维度——结构合法但语义错位的 Markdown 表格、括号不闭合的
LaTeX 不应挤掉可用的原图表示。本模块只做无 LLM 的结构检查；复杂跨行/跨列表格
不会被强行扁平化，而是交给原图回退；更细的跨行断词规则按真实失败样本再补。
"""
from __future__ import annotations

from html import unescape
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityAssessment:
    usable: bool
    reasons: tuple[str, ...] = ()


def table_html_to_markdown(text: str) -> str | None:
    """Convert a simple, rectangular HTML table to safe GFM.

    MinerU emits a mixture of HTML tables and source images.  Rendering raw
    HTML in the reader would require opting into dangerous HTML handling, so
    simple tables are normalised at the publication boundary instead.  Tables
    with ``rowspan``/``colspan`` are deliberately rejected: flattening those
    cells would silently change the paper's meaning and the caller should use
    the source image fallback.
    """

    value = str(text or "").strip()
    if not value or not re.search(r"<table\b[^>]*>", value, flags=re.IGNORECASE) or not re.search(
        r"</table\s*>", value, flags=re.IGNORECASE
    ):
        return None
    if re.search(r"<(?:td|th)\b[^>]*(?:rowspan|colspan)\s*=", value, flags=re.IGNORECASE):
        return None

    rows: list[list[str]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", value, flags=re.IGNORECASE | re.DOTALL):
        cells: list[str] = []
        for cell_html in re.findall(
            r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)\s*>",
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            cell = re.sub(r"<br\s*/?>", " ", cell_html, flags=re.IGNORECASE)
            cell = re.sub(r"<[^>]+>", "", cell)
            cell = re.sub(r"\s+", " ", unescape(cell)).strip()
            # A pipe inside a cell must not be interpreted as a column break.
            cell = re.sub(r"(?<!\\)\|", r"\\|", cell)
            cells.append(cell)
        if cells:
            rows.append(cells)

    if len(rows) < 2:
        return None
    width = len(rows[0])
    if width < 2 or any(len(row) != width for row in rows):
        return None
    output = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    output.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    result = "\n".join(output)
    return result if assess_table_markdown(result).usable else None


def _strip_outer_math_delimiters(text: str) -> str:
    value = text.strip()
    if value.startswith("$$") and value.endswith("$$") and len(value) >= 4:
        value = value[2:-2].strip()
    elif value.startswith("$") and value.endswith("$") and len(value) >= 2:
        value = value[1:-1].strip()
    return value


def _count_unescaped(text: str, char: str) -> int:
    return len(re.findall(rf"(?<!\\){re.escape(char)}", text))


def assess_table_html(text: str) -> QualityAssessment:
    """检查 HTML 表：必须真含 <table>，行列数一致，且不是目录伪表。

    目录伪表（MinerU 把目录页识别成表格）常见特征是整格以点线结尾，如
    ``<td>1.1 Introduction......</td>``——直接剔除，不允许其挤占表示优先级。
    """
    value = text.strip()
    if "<table" not in value.lower() or "</table>" not in value.lower():
        return QualityAssessment(False, ("table_html_missing_table_tag",))

    header_matches = re.findall(r"<th[^>]*>(.*?)</th>", value, flags=re.DOTALL | re.IGNORECASE)
    row_matches = re.findall(r"<tr[^>]*>(.*?)</tr>", value, flags=re.DOTALL | re.IGNORECASE)
    if not row_matches:
        return QualityAssessment(False, ("table_html_missing_rows",))

    header_count = len(header_matches)

    def _cell_count(row_html: str) -> int:
        cells = re.findall(r"<t[dh][^>]*>.*?</t[dh]>", row_html, flags=re.DOTALL | re.IGNORECASE)
        return len(cells)

    body_rows = [row for row in row_matches if _cell_count(row)]
    if body_rows:
        widths = {_cell_count(row) for row in body_rows}
        if len(widths) > 1:
            return QualityAssessment(False, ("table_html_ragged_rows",))
        width = widths.pop()
        if header_count and header_count != width:
            return QualityAssessment(False, ("table_html_header_mismatch",))
    if re.search(r"\.{4,}\s*</t[dh]>", value, flags=re.IGNORECASE):
        return QualityAssessment(False, ("table_html_looks_like_toc",))
    return QualityAssessment(True, ())


def assess_latex(text: str) -> QualityAssessment:
    """检查 LaTeX 公式：$ 配对、{}/() 大致平衡、mathrm/frac 等花括号不悬空。"""
    value = _strip_outer_math_delimiters(text)
    if not value:
        return QualityAssessment(False, ("latex_empty",))
    if _count_unescaped(value, "$") % 2:
        return QualityAssessment(False, ("latex_unbalanced_dollars",))
    for open_ch, close_ch in (("{", "}"), ("(", ")")):
        if value.count(open_ch) != value.count(close_ch):
            return QualityAssessment(False, (f"latex_unbalanced_{open_ch}{close_ch}",))
    if len(re.findall(r"\\begin\{", value)) != len(re.findall(r"\\end\{", value)):
        return QualityAssessment(False, ("latex_unbalanced_environment",))
    if re.search(r"\\[a-zA-Z]+\s*$", value) and not value.rstrip().endswith("}"):
        # 命令悬在末尾却没有参数（如 "... \mathrm"），通常是截断。
        return QualityAssessment(False, ("latex_truncated_command",))
    return QualityAssessment(True, ())


def assess_table_markdown(text: str) -> QualityAssessment:
    """检查 Markdown 管道表：至少表头+分隔两行、逐行竖线开头、列数一致、非目录伪表。"""
    value = text.strip()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) < 2 or not all(line.startswith("|") for line in lines):
        return QualityAssessment(False, ("table_markdown_missing_pipes",))

    def _cells(line: str) -> list[str]:
        body = line[1:-1] if line.endswith("|") else line[1:]
        return re.split(r"(?<!\\)\|", body)

    widths = {len(_cells(line)) for line in lines}
    if len(widths) != 1:
        return QualityAssessment(False, ("table_markdown_ragged_rows",))
    if not re.fullmatch(r"\|(\s*:?-+:?\s*\|)+", lines[1]):
        return QualityAssessment(False, ("table_markdown_missing_separator",))
    if re.search(r"\.{4,}\s*\|", value):
        return QualityAssessment(False, ("table_markdown_looks_like_toc",))
    return QualityAssessment(True, ())


def assess_representation(representation_type: str, content: str) -> QualityAssessment:
    if representation_type == "html":
        return assess_table_html(content)
    if representation_type == "markdown":
        return assess_table_markdown(content)
    if representation_type == "latex":
        return assess_latex(content)
    # image 及未知类型不做结构判定：图片由文件存在性保证。
    return QualityAssessment(True, ())
