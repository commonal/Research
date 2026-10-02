"""Gate C：最终 Markdown 成品的确定性质量检查。

这里只检查读者最终拿到的 Artifact，不重新判断论文事实、源材料或素材表示质量。
"""

from __future__ import annotations

import re

from .contracts import ArtifactQualityIssue, ArtifactQualityReport, RenderedNote
from .publication import PublicationManifest

_NUMBER = re.compile(r"^[+−-]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*[±%])?$", re.IGNORECASE)
_PDF_HYPHENATION = re.compile(r"\b[A-Za-z]{2,}-\s+[A-Za-z]{2,}\b")
_HEADING = re.compile(r"^(#{1,6})\s+\S")
_EDITORIAL_RESIDUE = re.compile(
    r"(?:\bTODO\b|待补充|待完善|作为(?:一个)?AI|根据(?:(?:你|您)?(?:所)?提供的|给定的)上下文)",
    re.IGNORECASE,
)


class ArtifactQualityGate:
    """以一个小接口封装最终成品的编辑检查规则。"""

    def evaluate(
        self,
        note: RenderedNote,
        *,
        publication_manifest: PublicationManifest | None = None,
    ) -> ArtifactQualityReport:
        issues = [
            *self._publication_integrity(publication_manifest),
            *self._asset_delivery(note),
            *self._unfinished_editorial_artifact(note.markdown),
            *self._duplicate_prose(note.markdown),
            *self._math_integrity(note.markdown),
            *self._image_integrity(note.markdown),
            *self._table_integrity(note.markdown),
            *self._suspicious_table_headers(note.markdown),
            *self._pdf_hyphenation(note.markdown),
        ]
        return ArtifactQualityReport(
            passed=not any(issue.severity == "error" for issue in issues),
            issues=tuple(issues),
        )

    @staticmethod
    def _publication_integrity(manifest: PublicationManifest | None) -> list[ArtifactQualityIssue]:
        if manifest is None:
            return []
        issues = [
            ArtifactQualityIssue(
                code="missing_publication_asset",
                message=f"发布素材 {name} 在源材料中不存在或不可读取。",
            )
            for name in manifest.missing_files
        ]
        issues.extend(
            ArtifactQualityIssue(
                code="publication_asset_collision",
                message=f"多个不同源素材映射到同一发布文件名 {name}。",
            )
            for name in manifest.collision_files
        )
        issues.extend(
            ArtifactQualityIssue(
                code="publication_asset_copy_failed",
                message=f"发布素材 {name} 复制失败。",
            )
            for name in manifest.copy_failed_files
        )
        if not manifest.passed and not issues:
            issues.append(ArtifactQualityIssue(
                code="incomplete_publication_manifest",
                message="发布清单中的 referenced 与 copied 素材不一致。",
            ))
        return issues

    @staticmethod
    def _asset_delivery(note: RenderedNote) -> list[ArtifactQualityIssue]:
        issues: list[ArtifactQualityIssue] = []
        lines = note.markdown.splitlines()
        fenced = _fenced_content_lines(lines)
        for line_number, line in enumerate(lines, start=1):
            if line_number in fenced:
                continue
            for match in re.finditer(r"\{\{asset:([A-Za-z0-9_.-]+)\}\}", line):
                issues.append(ArtifactQualityIssue(
                    code="unresolved_asset_anchor",
                    message=f"素材锚点 {match.group(1)} 未被 Renderer 解析。",
                    line=line_number,
                ))
        for result in note.render_results:
            if result.status != "rendered":
                issues.append(ArtifactQualityIssue(
                    code="failed_asset_render",
                    message=f"素材 {result.anchor_id} 渲染状态为 {result.status}。",
                ))
        return issues

    @staticmethod
    def _image_integrity(markdown: str) -> list[ArtifactQualityIssue]:
        issues: list[ArtifactQualityIssue] = []
        lines = markdown.splitlines()
        fenced = _fenced_content_lines(lines)
        for line_number, line in enumerate(lines, start=1):
            if line_number in fenced:
                continue
            if re.search(r"!\[[^\]]*\]\(\s*!\[", line):
                issues.append(ArtifactQualityIssue(
                    code="nested_image_markdown",
                    message="图片 Markdown 被嵌套在另一个图片链接中，Renderer 输出不可用。",
                    line=line_number,
                ))
            for match in re.finditer(r"!\[[^\]]*\]\(([^)\n]+)\)", line):
                target = match.group(1).strip().strip("<>")
                if re.match(r"^(?:[A-Za-z]:[\\/]|file://|/(?:tmp|var/tmp)/)", target, re.IGNORECASE):
                    issues.append(ArtifactQualityIssue(
                        code="local_image_path",
                        message="最终笔记中的图片链接指向本机临时或绝对文件路径。",
                        line=line_number,
                    ))
        for index, line in enumerate(lines):
            image = re.fullmatch(r"\s*!\[([^\]]*)\]\([^)]+\)\s*", line)
            if not image or not image.group(1).strip():
                continue
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index >= len(lines):
                continue
            visible = re.fullmatch(r"\s*(?:\*([^*]+)\*|_([^_]+)_)\s*", lines[next_index])
            if not visible:
                continue
            caption = (visible.group(1) or visible.group(2) or "").strip()
            if re.sub(r"\s+", " ", caption).casefold() == re.sub(r"\s+", " ", image.group(1)).strip().casefold():
                issues.append(ArtifactQualityIssue(
                    code="duplicate_visible_image_caption",
                    message="图片 alt 被再次作为可见图注输出，造成重复内容。",
                    line=next_index + 1,
                ))
                continue
            if len(caption) >= 80 and re.match(r"^(?:figure|fig\.|table|equation)\s*\d+", caption, re.IGNORECASE):
                issues.append(ArtifactQualityIssue(
                    code="visible_english_source_caption",
                    message="中文教学笔记的图片后残留长英文原始图注，应由正文中文解释承担展示职责。",
                    line=next_index + 1,
                ))
        return issues

    @staticmethod
    def _math_integrity(markdown: str) -> list[ArtifactQualityIssue]:
        markers: list[int] = []
        environments: list[tuple[str, int]] = []
        issues: list[ArtifactQualityIssue] = []
        in_fence = False
        for line_number, line in enumerate(markdown.splitlines(), start=1):
            if re.match(r"^\s*(`{3,}|~{3,})", line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            markers.extend(line_number for _ in re.finditer(r"(?<!\\)\$\$", line))
            for token in re.finditer(r"\\(begin|end)\{([A-Za-z*]+)\}", line):
                action, environment = token.group(1), token.group(2)
                if action == "begin":
                    environments.append((environment, line_number))
                elif environments and environments[-1][0] == environment:
                    environments.pop()
                else:
                    issues.append(ArtifactQualityIssue(
                        code="unmatched_latex_environment",
                        message=f"LaTeX 环境 {environment} 的 begin/end 不匹配。",
                        line=line_number,
                    ))
        if len(markers) % 2:
            issues.append(ArtifactQualityIssue(
                code="unclosed_display_math",
                message="Markdown display math 的 $$ 定界符未闭合。",
                line=markers[-1],
            ))
        issues.extend(
            ArtifactQualityIssue(
                code="unmatched_latex_environment",
                message=f"LaTeX 环境 {environment} 缺少对应的 end。",
                line=line_number,
            )
            for environment, line_number in environments
        )
        return issues

    @staticmethod
    def _duplicate_prose(markdown: str) -> list[ArtifactQualityIssue]:
        seen: dict[str, int] = {}
        issues: list[ArtifactQualityIssue] = []
        for paragraph, line_number in _prose_paragraphs(markdown):
            normalized = re.sub(r"\s+", " ", paragraph).strip().casefold()
            if len(normalized) < 30:
                continue
            first_line = seen.get(normalized)
            if first_line is None:
                seen[normalized] = line_number
                continue
            issues.append(ArtifactQualityIssue(
                code="duplicate_prose_paragraph",
                message=f"正文段落与第 {first_line} 行完全重复。",
                line=line_number,
            ))
        return issues

    @staticmethod
    def _table_integrity(markdown: str) -> list[ArtifactQualityIssue]:
        lines = markdown.splitlines()
        fenced = _fenced_content_lines(lines)
        issues: list[ArtifactQualityIssue] = []
        for index in range(len(lines) - 1):
            if index + 1 in fenced or index + 2 in fenced:
                continue
            header = _table_cells(lines[index])
            separator = _table_cells(lines[index + 1])
            if not header or not _is_separator_row(separator):
                continue
            expected = len(header)
            row_index = index + 2
            while row_index < len(lines):
                row = _table_cells(lines[row_index])
                if not row:
                    break
                if len(row) != expected:
                    issues.append(ArtifactQualityIssue(
                        code="inconsistent_table_columns",
                        message=f"Markdown 表格应有 {expected} 列，当前行有 {len(row)} 列。",
                        line=row_index + 1,
                    ))
                row_index += 1
        return issues

    @staticmethod
    def _unfinished_editorial_artifact(markdown: str) -> list[ArtifactQualityIssue]:
        lines = markdown.splitlines()
        fenced = _fenced_content_lines(lines)
        issues: list[ArtifactQualityIssue] = []
        previous_heading_level: int | None = None
        for index, line in enumerate(lines):
            if index + 1 in fenced:
                continue
            heading = _HEADING.match(line.strip())
            if not heading:
                continue
            heading_level = len(heading.group(1))
            if previous_heading_level is not None and heading_level > previous_heading_level + 1:
                issues.append(ArtifactQualityIssue(
                    code="heading_level_jump",
                    message=f"标题层级从 H{previous_heading_level} 直接跳到 H{heading_level}。",
                    line=index + 1,
                ))
            previous_heading_level = heading_level
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index >= len(lines):
                issues.append(ArtifactQualityIssue(
                    code="empty_section", message="章节标题后没有正文。", line=index + 1,
                ))
                continue
            next_heading = _HEADING.match(lines[next_index].strip())
            if next_heading and len(next_heading.group(1)) <= len(heading.group(1)):
                issues.append(ArtifactQualityIssue(
                    code="empty_section", message="章节标题后没有正文。", line=index + 1,
                ))

        for line_number, line in enumerate(lines, start=1):
            if line_number in fenced:
                continue
            if _EDITORIAL_RESIDUE.search(line):
                issues.append(ArtifactQualityIssue(
                    code="editorial_residue", message="正文残留 TODO 或模型生成话术。", line=line_number,
                ))

        fence_lines = [
            line_number for line_number, line in enumerate(lines, start=1)
            if re.match(r"^\s*(`{3,}|~{3,})", line)
        ]
        if len(fence_lines) % 2:
            issues.append(ArtifactQualityIssue(
                code="unclosed_fenced_block", message="Markdown fenced block 未闭合。", line=fence_lines[-1],
            ))
        final = next(((index + 1, line.strip()) for index, line in reversed(list(enumerate(lines))) if line.strip()), None)
        if final is not None:
            line_number, value = final
            if (
                not re.match(r"^(?:!\[|\||```|~~~|\$\$)", value)
                and value.endswith(("：", ":", "，", ",", "；", ";", "—", "–", "\\"))
            ):
                issues.append(ArtifactQualityIssue(
                    code="abrupt_artifact_ending",
                    message="最终正文以明确续写符结束，成品可能被截断。",
                    line=line_number,
                ))
        return issues

    @staticmethod
    def _suspicious_table_headers(markdown: str) -> list[ArtifactQualityIssue]:
        lines = markdown.splitlines()
        fenced = _fenced_content_lines(lines)
        issues: list[ArtifactQualityIssue] = []
        for index in range(len(lines) - 2):
            if any(line_number in fenced for line_number in (index + 1, index + 2, index + 3)):
                continue
            header = _table_cells(lines[index])
            separator = _table_cells(lines[index + 1])
            first_body_row = _table_cells(lines[index + 2])
            if not header or not _is_separator_row(separator) or not first_body_row:
                continue
            if len(header) != len(separator) or len(header) < 3:
                continue
            # 科研表格的第一格常是方法名；若其余格几乎全为测量值，第一行更像数据而非列名。
            if _numeric_ratio(header[1:]) >= 0.75 and _numeric_ratio(first_body_row[1:]) >= 0.75:
                issues.append(ArtifactQualityIssue(
                    code="suspicious_data_row_as_table_header",
                    message="表格首行疑似实验数据而不是有效列名，不能作为完整成品发布。",
                    line=index + 1,
                ))
        return issues

    @staticmethod
    def _pdf_hyphenation(markdown: str) -> list[ArtifactQualityIssue]:
        issues: list[ArtifactQualityIssue] = []
        lines = markdown.splitlines()
        fenced = _fenced_content_lines(lines)
        for line_number, line in enumerate(lines, start=1):
            if line_number in fenced:
                continue
            if _PDF_HYPHENATION.search(line):
                issues.append(ArtifactQualityIssue(
                    code="pdf_hyphenation",
                    message="正文残留疑似 PDF 行尾断词。",
                    line=line_number,
                ))
        return issues


def _table_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    return tuple(cell.strip() for cell in stripped[1:-1].split("|"))


def _prose_paragraphs(markdown: str) -> tuple[tuple[str, int], ...]:
    paragraphs: list[tuple[str, int]] = []
    current: list[str] = []
    start_line = 0
    in_fence = False

    def flush() -> None:
        nonlocal current, start_line
        if current:
            value = "\n".join(current).strip()
            if value and not re.match(r"^(?:#{1,6}\s|!\[|\||>|[-*+]\s|\d+[.)]\s)", value):
                paragraphs.append((value, start_line))
        current = []
        start_line = 0

    for line_number, line in enumerate(markdown.splitlines(), start=1):
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            flush()
            continue
        if not current:
            start_line = line_number
        current.append(line)
    flush()
    return tuple(paragraphs)


def _fenced_content_lines(lines: list[str]) -> frozenset[int]:
    fenced: set[int] = set()
    in_fence = False
    for line_number, line in enumerate(lines, start=1):
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            in_fence = not in_fence
            continue
        if in_fence:
            fenced.add(line_number)
    return frozenset(fenced)


def _is_separator_row(cells: tuple[str, ...]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _numeric_ratio(cells: tuple[str, ...]) -> float:
    if not cells:
        return 0.0
    numeric = sum(bool(_NUMBER.fullmatch(cell.replace(" ", ""))) for cell in cells)
    return numeric / len(cells)
