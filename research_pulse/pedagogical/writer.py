"""S5 教学化 NoteWriter。契约见 SPEC §7/§9。

Writer 只负责：把 ``PaperModel + TeachingPlan (+ briefs)`` 变成**带 asset anchor 的正文**。
不渲染、不编造图注/数值、不猜测 asset 位置。模型通过 ``write_pedagogical_note`` 注入。
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Callable, Mapping, Sequence

from .contracts import AssetAnchor, NoteDraftWithAnchors, PaperModel, TeachingPlan


def extract_anchor_ids(markdown: str) -> tuple[str, ...]:
    """从正文提取出现的 ``{{asset:<id>}}`` 标记。"""
    return tuple(dict.fromkeys(re.findall(r"\{\{asset:([A-Za-z0-9_.-]+)\}\}", markdown)))


def anchors_from_markdown(
    markdown: str,
    *,
    section_id: str = "",
    placement: str = "after_paragraph",
    render_mode: str = "inline",
) -> tuple[AssetAnchor, ...]:
    """把正文里出现的 asset 标记转成默认 anchor（P0 最小版：section/placement 取默认）。

    P1 由 AssetPlan 提供精确 section/placement；P0 最小闭环用默认值即可跑通。
    """
    return tuple(
        AssetAnchor(f"anchor-{i}", asset_id, section_id, placement, render_mode)
        for i, asset_id in enumerate(extract_anchor_ids(markdown), start=1)
    )


class PedagogicalWriter:
    """注入 model（须有 ``write_pedagogical_note(value) -> str``），产出带 anchors 的正文。"""

    def __init__(self, model: Any) -> None:
        self.model = model

    def write(
        self,
        paper_model: PaperModel,
        teaching_plan: TeachingPlan,
        briefs: Mapping[str, Any] | None = None,
    ) -> NoteDraftWithAnchors:
        value: dict[str, Any] = {
            "paper_model": asdict(paper_model),
            "teaching_plan": asdict(teaching_plan),
            "briefs": briefs or {},
            "instructions": _WRITER_INSTRUCTION,
        }
        markdown = self.model.write_pedagogical_note(value)
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("write_pedagogical_note 必须返回非空字符串")
        return NoteDraftWithAnchors(markdown=markdown, anchors=anchors_from_markdown(markdown))


_WRITER_INSTRUCTION = (
    "写一篇中文教学化论文笔记。目标读者：没读过原论文的技术读者。\n"
    "要求：\n"
    "1) 按 teaching_plan.sections 的顺序组织章节，每节实现其 teaching_goal；\n"
    "2) 用 reader_goal 统一口吻，必要时用 running_example 贯穿；\n"
    "3) 公式/符号讲清它解决什么判断困难；数值带条件与比较；\n"
    "4) briefs.selected_asset_ids 是 Planner 选中的完整清单；每个 id 都必须在正文恰好放置一个 "
    "{{asset:<asset_id>}} 标记，并在相邻正文说明它帮助读者理解什么（只给 asset_id，不要自己写路径或重建数值）；\n"
    "5) 不编造证据、不夸大结论、不写内部字段/证据句柄/路径；\n"
    "6) 结尾明确适用边界与局限。\n"
    "返回纯 Markdown 正文。"
)
