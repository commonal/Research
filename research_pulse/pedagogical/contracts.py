"""Pedagogical Reading Pipeline — 域契约（framework-free）。

单一事实源：``docs/pedagogical-reading-pipeline.md`` §2（域结构）与 §9（工程契约）。
此处只做 dataclass + 校验，不掺 FastAPI / LangGraph / 数据库。

两层必须严格分离：
- ``PaperModel``：论文**事实**状态（thesis / 机制 / 实验 / 限制）。不含任何"怎么讲"字段，可被 Knowledge Layer 复用。
- ``TeachingPlan``：**读者侧解释策略**（archetype / domain / reader_goal / prerequisites / sections）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

if TYPE_CHECKING:
    from .source_quality import SourceQualityReport

ARCHETYPES = ("method", "benchmark", "system", "empirical-study")
PLACEMENTS = ("after_paragraph", "before_paragraph", "end_of_section", "after_table")
RENDER_MODES = ("inline", "reference")
RENDER_STATUSES = ("rendered", "missing", "unavailable")


# --------------------------------------------------------------------------- #
# PaperModel — 事实层（不含教学字段）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Experiment:
    """一个实验事实：它要回答的问题、怎么搭、和谁比、结果、怎么解读、边界在哪。"""

    question: str
    setup: tuple[str, ...] = ()
    comparison: tuple[str, ...] = ()
    results: tuple[str, ...] = ()
    interpretation: str = ""
    boundary: str = ""
    evidence_handles: tuple[str, ...] = ()
    experiment_id: str = ""


@dataclass(frozen=True)
class PaperModel:
    """论文是什么。仅事实；`teaching` 字段一律禁止。"""

    thesis: str
    central_problem: str = ""
    prior_gap: str = ""
    central_idea: str = ""
    argument_chain: tuple[str, ...] = ()
    experiments: tuple[Experiment, ...] = ()
    must_preserve_facts: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    source_facts: tuple[str, ...] = ()
    material_unknowns: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# TeachingPlan — 读者侧解释策略
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TeachingSection:
    section_id: str
    title: str
    teaching_goal: str
    evidence_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class TeachingPlan:
    """我准备怎么给读者讲。archetype 决定阅读顺序；domain 只影响前提与领域背景。"""

    paper_archetype: tuple[str, ...]          # subset of ARCHETYPES
    domain: tuple[str, ...]                   # e.g. ("LLM Safety", "RLHF")
    reader_goal: str
    prerequisites: tuple[str, ...] = ()
    sections: tuple[TeachingSection, ...] = ()
    running_example: str = ""


# --------------------------------------------------------------------------- #
# Asset anchor 协议（Writer 只产 anchor；Renderer 只做确定性解析）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AssetAnchor:
    anchor_id: str
    asset_id: str
    section_id: str
    placement: str = "after_paragraph"         # PLACEMENTS
    render_mode: str = "inline"                # RENDER_MODES


# --------------------------------------------------------------------------- #
# S3 AssetPlan — 素材选择（解释价值优先 + 预算）
# --------------------------------------------------------------------------- #
ASSET_DECISIONS = ("inline", "reference", "omit")
ASSET_BUDGETS = {"formula": 4, "figure": 3, "table": 3}   # tasks.md T9 预算


@dataclass(frozen=True)
class AssetChoice:
    """S3 对单个资产的决策：选不选、怎么用、依据是什么。"""

    asset_id: str
    kind: str                                   # formula | table | figure
    decision: str                               # ASSET_DECISIONS
    rationale: str = ""


@dataclass(frozen=True)
class AssetPlan:
    """S3 产物：Writer 只能锚 ``choices`` 里 decision=inline|reference 的资产。"""

    choices: tuple[AssetChoice, ...] = ()
    overrides: tuple[str, ...] = ()             # 超预算时的依据（tasks.md: override 需 rationale）


# --------------------------------------------------------------------------- #
# S4 AssetBriefs — 素材解释（怎么讲素材）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FormulaBrief:
    asset_id: str
    role: str                                   # 这个公式在论证里起什么作用
    plain_explanation: str                      # 白话解释（不读公式的人怎么懂）
    symbol_meanings: tuple[str, ...] = ()       # 符号含义逐条
    unknowns: tuple[str, ...] = ()              # 不确定/未展开的项


@dataclass(frozen=True)
class TableBrief:
    asset_id: str
    role: str
    plain_explanation: str
    reading_notes: tuple[str, ...] = ()         # 怎么读这张表（看哪列、注意什么）


@dataclass(frozen=True)
class FigureBrief:
    asset_id: str
    source: str                                 # caption | pixels（解释来源）
    interpretation: str = ""
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssetBriefs:
    """S4 产物：Writer 讲素材时的逐资产解释。"""

    formulas: tuple[FormulaBrief, ...] = ()
    tables: tuple[TableBrief, ...] = ()
    figures: tuple[FigureBrief, ...] = ()


@dataclass(frozen=True)
class RenderResult:
    """Renderer 对单个 anchor 的确定性记录。"""

    anchor_id: str
    status: str                                 # RENDER_STATUSES
    markdown: str = ""


@dataclass(frozen=True)
class NoteDraftWithAnchors:
    """Writer 产物：正文 + 锚点列表；正文采用 ``{{asset:<asset_id>}}`` 内联标记。"""

    markdown: str
    anchors: tuple[AssetAnchor, ...] = ()


@dataclass(frozen=True)
class RenderedNote:
    """最终读者能看到的 note artifact = 正文 + 已渲染可访问资产（非纯字符串）。"""

    markdown: str
    render_results: tuple[RenderResult, ...] = ()


# --------------------------------------------------------------------------- #
# 门禁结果（P2 使用，先定义占位契约）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvidenceGateResult:
    passed: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactQualityIssue:
    """最终成品中的确定性编辑缺陷；location 使用 1-based Markdown 行号。"""

    code: str
    message: str
    line: int | None = None
    severity: Literal["warning", "error"] = "error"


@dataclass(frozen=True)
class ArtifactQualityReport:
    """Gate C 的结构化结果；只有 error 会阻止发布。"""

    passed: bool
    issues: tuple[ArtifactQualityIssue, ...] = ()


@dataclass(frozen=True)
class BlindReaderDimension:
    dimension: Literal[
        "background", "prior_gap", "mechanism", "formalism", "experiment", "visual", "boundary"
    ]
    score: Literal["clear", "partial", "missing"]
    evidence: str = ""
    note: str = ""


@dataclass(frozen=True)
class BlindReaderResult:
    overall: Literal["pass", "needs_targeted_revision", "fail"]
    dimensions: tuple[BlindReaderDimension, ...] = ()
    critical_missing_information: tuple[Mapping[str, str], ...] = ()
    model: str = ""
    prompt_version: str = ""


@dataclass(frozen=True)
class AssetUsageLedger:
    """一次笔记运行中素材从计划到发布的确定性使用记录。"""

    selected_ids: tuple[str, ...] = ()
    anchored_ids: tuple[str, ...] = ()
    rendered_ids: tuple[str, ...] = ()
    unreferenced_selected_ids: tuple[str, ...] = ()
    failed_render_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PedagogicalResult:
    """教学管线一次运行的完整产出：笔记 + 三道门禁结果 + 是否做过修复。"""

    note: RenderedNote
    evidence: "EvidenceGateResult"
    blind: BlindReaderResult
    artifact: ArtifactQualityReport = field(default_factory=lambda: ArtifactQualityReport(passed=True))
    source_quality: "SourceQualityReport | None" = None
    repaired: bool = False
    zero_anchors: bool = False  # 有资产候选但正文零锚定（教学化笔记必须有资产解释）
    asset_usage: AssetUsageLedger = field(default_factory=AssetUsageLedger)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate_contracts(paper_model: PaperModel, teaching_plan: TeachingPlan) -> None:
    """校验两个边界：PaperModel 不含教学字段；TeachingPlan 的 archetype 合法。"""
    if not paper_model.thesis.strip():
        raise ValueError("PaperModel.thesis 不能为空")
    if not teaching_plan.reader_goal.strip():
        raise ValueError("TeachingPlan.reader_goal 不能为空")
    bad = [a for a in teaching_plan.paper_archetype if a not in ARCHETYPES]
    if bad:
        raise ValueError(f"TeachingPlan 含非法 archetype: {bad}")


def parse_note_anchors(markdown: str, known: Mapping[str, AssetAnchor]) -> tuple[AssetAnchor, ...]:
    """从 ``{{asset:<asset_id>}}`` 标记解析出正文出现的 anchor，须全部在 ``known`` 中。

    Renderer 据此做确定性绑定；不允许出现未知 asset（否则缺 anchor 记录）。
    """
    import re

    found: list[AssetAnchor] = []
    for asset_id in re.findall(r"\{\{asset:([A-Za-z0-9_.-]+)\}\}", markdown):
        anchor = known.get(asset_id)
        if anchor is None:
            raise ValueError(f"正文引用了未声明的 asset anchor: {asset_id}")
        found.append(anchor)
    return tuple(found)
