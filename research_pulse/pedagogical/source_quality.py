"""Gate A（source half）：CanonicalPaperIR 整体源材料质量检查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import re

from ..production.reading import CanonicalPaperIR, PaperIRBlock

SourceQualityStatus = Literal["accepted", "degraded", "rejected"]

_ABSTRACT_SECTIONS = {"abstract", "摘要"}
_REFERENCE_SECTIONS = {"references", "reference", "bibliography", "参考文献"}
_NON_BODY_SECTIONS = _ABSTRACT_SECTIONS | _REFERENCE_SECTIONS | {
    "title", "contents", "table of contents", "目录", "acknowledgments", "acknowledgements", "致谢",
}
_TEXT_KINDS = {"paragraph", "text"}


@dataclass(frozen=True)
class SourceQualityIssue:
    code: str
    message: str
    severity: Literal["warning", "error"] = "error"
    block_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceQualityReport:
    status: SourceQualityStatus
    issues: tuple[SourceQualityIssue, ...] = ()
    usable_block_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status != "rejected"


class SourceQualityRejected(RuntimeError):
    """源材料没有可读正文；调用方必须停止生成而不是改用另一个 Writer。"""

    def __init__(self, report: SourceQualityReport) -> None:
        super().__init__("source quality rejected: " + ",".join(issue.code for issue in report.issues))
        self.report = report


class SourceQualityGate:
    """判断一篇 CanonicalPaperIR 是否有足够正文进入阅读流程。"""

    def evaluate(self, paper: CanonicalPaperIR) -> SourceQualityReport:
        body_candidates = tuple(block for block in paper.ordered_blocks if _is_body_text_block(block))
        body_blocks = tuple(block for block in body_candidates if block.parse_status == "available")
        usable = tuple(block.block_id for block in body_blocks)
        if not usable:
            code = "no_available_body_blocks" if body_candidates else "missing_substantive_body"
            message = (
                "正文 block 均不可用，不能进入完整阅读流程。"
                if body_candidates
                else "解析结果只有摘要、目录、标题或参考文献，没有可供完整阅读的正文。"
            )
            return SourceQualityReport(
                status="rejected",
                issues=(SourceQualityIssue(
                    code=code,
                    message=message,
                    block_ids=tuple(block.block_id for block in paper.ordered_blocks),
                ),),
                usable_block_ids=(),
            )

        issues: list[SourceQualityIssue] = []
        inverted_ids = tuple(
            block_id
            for left, right in zip(paper.blocks, paper.blocks[1:])
            if right.order < left.order
            for block_id in (left.block_id, right.block_id)
        )
        if inverted_ids:
            issues.append(SourceQualityIssue(
                code="non_monotonic_block_order",
                message="CanonicalPaperIR 中的 block order 出现逆序，正文阅读顺序可能不可靠。",
                severity="warning",
                block_ids=tuple(dict.fromkeys(inverted_ids)),
            ))
        unavailable = tuple(block.block_id for block in body_candidates if block.parse_status != "available")
        if unavailable and len(unavailable) / len(body_candidates) >= 0.5:
            issues.append(SourceQualityIssue(
                code="unavailable_body_blocks",
                message="较高比例的正文 block 解析状态不可用。",
                severity="warning",
                block_ids=unavailable,
            ))
        if body_candidates[-1].parse_status != "available":
            issues.append(SourceQualityIssue(
                code="truncated_body_tail",
                message="最后一个正文 block 明确不可用，解析结果可能在正文尾部截断。",
                severity="warning",
                block_ids=(body_candidates[-1].block_id,),
            ))

        garbled_ids = tuple(block.block_id for block in body_blocks if _looks_garbled(block.text))
        if garbled_ids:
            issues.append(SourceQualityIssue(
                code="garbled_body_text",
                message="正文包含高密度 replacement/control characters，可能存在乱码或解码失败。",
                severity="warning",
                block_ids=garbled_ids,
            ))
        normalized_groups: dict[str, list[str]] = {}
        for block in body_blocks:
            normalized = re.sub(r"\s+", " ", block.text).strip().casefold()
            normalized_groups.setdefault(normalized, []).append(block.block_id)
        repeated_ids = tuple(
            block_id
            for block_ids in normalized_groups.values()
            if len(block_ids) > 1
            for block_id in block_ids
        )
        if repeated_ids and len(repeated_ids) / len(body_blocks) >= 0.3:
            issues.append(SourceQualityIssue(
                code="duplicate_body_blocks",
                message="正文存在较高比例的完全重复段落，可能包含重复页面或页眉页脚污染。",
                severity="warning",
                block_ids=repeated_ids,
            ))
        boilerplate_ids = tuple(
            block_id
            for normalized, block_ids in normalized_groups.items()
            if 8 <= len(normalized) <= 60 and len(block_ids) >= 3
            for block_id in block_ids
        )
        if boilerplate_ids:
            issues.append(SourceQualityIssue(
                code="repeated_boilerplate",
                message="同一短文本重复至少三次，可能是未清理的页眉、页脚或出版信息。",
                severity="warning",
                block_ids=boilerplate_ids,
            ))

        body_text = "\n".join(block.text for block in body_blocks)
        section_names = {
            _normalized_section_name(part)
            for block in body_blocks
            for part in block.section.replace(">", "/").split("/")
            if part.strip()
        }
        if len(body_blocks) >= 6 and len(body_text) >= 4_000 and len(section_names) <= 1:
            issues.append(SourceQualityIssue(
                code="missing_section_structure",
                message="长篇正文缺少可用章节结构，代表性取样与覆盖判断可能不可靠。",
                severity="warning",
                block_ids=tuple(block.block_id for block in body_blocks),
            ))
        hyphenated = re.findall(r"\b[A-Za-z]{2,}-\s+[A-Za-z]{2,}\b", body_text)
        english_words = re.findall(r"\b[A-Za-z]{2,}\b", body_text)
        if len(hyphenated) >= 2 and len(hyphenated) / max(1, len(english_words)) >= 0.02:
            issues.append(SourceQualityIssue(
                code="pdf_hyphenation",
                message="正文包含较高比例的疑似 PDF 行尾断词。",
                severity="warning",
            ))

        return SourceQualityReport(
            status="degraded" if issues else "accepted",
            issues=tuple(issues),
            usable_block_ids=usable,
        )


def _is_body_text_block(block: PaperIRBlock) -> bool:
    if block.kind not in _TEXT_KINDS:
        return False
    section_parts = {
        _normalized_section_name(part)
        for part in block.section.replace(">", "/").split("/")
        if part.strip()
    }
    return not bool(section_parts & _NON_BODY_SECTIONS)


def _normalized_section_name(value: str) -> str:
    name = value.strip().casefold().rstrip(":：.- ")
    return re.sub(r"^(?:(?:\d+(?:\.\d+)*)|(?:[ivxlcdm]+))[.)]?\s+", "", name)


def _looks_garbled(text: str) -> bool:
    suspicious = sum(
        character == "\ufffd" or (ord(character) < 32 and character not in "\n\r\t")
        for character in text
    )
    return suspicious >= 3 and suspicious / max(1, len(text)) >= 0.02
