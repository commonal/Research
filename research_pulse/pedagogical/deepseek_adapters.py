"""真实 DeepSeek 适配器（P0 最小闭环接真模型）。

把 ``PedagogicalReadingPipeline`` 的三个可注入步骤接到生产 ``DeepSeekPaperReadingModel`` 上：

- S1 ``build_paper_model``：一次调用 → ``PaperModel``（论文事实层，无教学字段）
- S2 ``build_teaching_plan``：一次调用 → ``TeachingPlan``（读者侧解释策略）
- S5 ``write_pedagogical_note``：一次调用 → 带 ``{{asset:<id>}}`` 锚点的中文教学化正文

资产（表/图/公式）由 ``assets_from_blocks`` 从真实归一化块**确定性**构建：
- 表：``text`` 已是管道分隔 → 还原成 markdown 管道表（Renderer 替换 anchor 时落地）。
- 图：``image_path`` + ``caption`` → ``PublishedAsset(kind=figure, image_path=...)``。
- 公式：有 ``latex`` → ``$$...$$``；无 latex 但有 ``image_path`` → 图片引用；二者皆无 → **不建资产**（散文解释，不假装渲染不可得的公式）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperIRBlock
from .contracts import Experiment, PaperModel, TeachingPlan, TeachingSection
from .renderer import PublishedAsset


# --------------------------------------------------------------------------- #
# 确定性资产构建
# --------------------------------------------------------------------------- #
def pipe_table_from_text(text: str) -> str | None:
    """把管道分隔文本还原成 markdown 管道表；不像表则返回 None。"""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if not lines:
        return None
    rows: list[list[str]] = []
    for line in lines:
        body = line.strip().strip("|")
        if not body.strip():
            continue
        cells = [c.strip() for c in body.split("|")]
        if len(cells) <= 1:
            return None
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue  # 跳过已有分隔行
        rows.append(cells)
    if len(rows) < 1:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header = rows[0]
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(out)


def _norm(value: object) -> str:
    """把 None / 字面量 'None' / 'null' 归一到空串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "null") else text


def _clean_caption(value: object) -> str:
    """归一化 caption 并剥掉 MinerU 残留的 HTML 标签（``<sub>``/``<sup>``）。"""
    return re.sub(r"<[^>]+>", "", _norm(value))


_BS = chr(92)  # 单个反斜杠，避免源码里反斜杠字面量被双重转义


def _unescape_latex(latex: str) -> str:
    """把被 JSON 双重转义的 LaTeX（``\\begin``）还原成单反斜杠（``\begin``），KaTeX 才能解析。"""
    if not latex:
        return latex
    return latex.replace(_BS * 2, _BS)


def table_from_html(html_text: str) -> str | None:
    """把解析器给的 ``<table><tr><td>`` 网格重建为合法 markdown 管道表。

    ``block.text`` 常被塌成一行、``| --- |`` 内联，不是合法 GFM 表；``table_html``
    是干净的单元格网格，用它重建才对。无有效行则返回 ``None``。
    """
    import html as _html

    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, re.S):
        cells: list[str] = []
        for cell in re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", tr, re.S):
            value = _html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
            value = re.sub(r"\s+", " ", value)
            value = re.sub(r"(?<!\\)\|", r"\\|", value)
            cells.append(value)
        if any(cells):
            rows.append(cells)
    if len(rows) < 1:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(out)


def assets_from_blocks(blocks: Sequence[PaperIRBlock]) -> tuple[dict[str, PublishedAsset], list[dict[str, Any]]]:
    """从真实块生成可渲染资产表 + 供 writer 决策的紧凑候选清单。

    返回 ``(assets, candidates)``，二者用同一 ``asset_id`` 对齐：
    - ``assets`` 只含*可渲染*资产（表/有 latex 或图片的公式/图）；writer 只能锚这些。
    - ``candidates`` 是传给 writer 的元数据，含 ``renderable`` 标记与简短说明。
    """
    assets: dict[str, PublishedAsset] = {}
    candidates: list[dict[str, Any]] = []
    counters = {"figure": 0, "table": 0, "formula": 0}
    for block in blocks:
        kind = block.kind
        if kind not in counters:
            continue
        counters[kind] += 1
        asset_id = f"{kind}-{counters[kind]:02d}"
        caption = _clean_caption(block.caption)
        if kind == "table":
            # block.text 常被解析器塌成一行、内联 `| --- |`，不是合法 GFM 表；
            # 用干净的 table_html 网格重建才对。无 html 时退化到 text 解析。
            table_md = table_from_html(_norm(block.table_html)) or pipe_table_from_text(block.text)
            if not table_md:
                candidates.append({"asset_id": asset_id, "source_block_id": block.block_id, "kind": kind, "caption": caption, "renderable": False, "order": len(candidates)})
                continue
            assets[asset_id] = PublishedAsset(asset_id=asset_id, kind="table", markdown=table_md, caption=caption)
            candidates.append({
                "asset_id": asset_id, "source_block_id": block.block_id, "kind": kind, "caption": caption or asset_id,
                "renderable": True, "preview": "\n".join(table_md.splitlines()[:4]), "order": len(candidates),
            })
        elif kind == "formula":
            latex = _unescape_latex(_norm(block.latex))  # 还原被 JSON 双重转义的 LaTeX
            image_path = _norm(block.image_path)
            renderable = bool(latex or image_path)
            if latex:
                assets[asset_id] = PublishedAsset(asset_id=asset_id, kind="formula", markdown=f"$${latex}$$", caption=caption)
            elif image_path:
                assets[asset_id] = PublishedAsset(asset_id=asset_id, kind="formula", image_path=image_path, caption=caption or "公式")
            candidates.append({
                "asset_id": asset_id, "source_block_id": block.block_id, "kind": kind, "caption": caption or asset_id,
                "renderable": renderable, "preview": _norm(block.text)[:120], "order": len(candidates),
            })
        elif kind == "figure":
            image_path = _norm(block.image_path)
            renderable = bool(image_path)
            if renderable:
                assets[asset_id] = PublishedAsset(asset_id=asset_id, kind="figure", image_path=image_path, caption=caption)
            candidates.append({
                "asset_id": asset_id, "source_block_id": block.block_id, "kind": kind, "caption": caption or asset_id,
                "renderable": renderable, "preview": "", "order": len(candidates),
            })
    return assets, candidates


def paper_title_from_blocks(blocks: Sequence[PaperIRBlock]) -> str:
    for block in blocks:
        if block.kind != "paragraph":
            continue
        section = block.section.casefold()
        if "title" in section or "body" == block.section.casefold():
            return block.text.strip()[:120]
    return blocks[0].text.strip()[:120] if blocks else ""


# --------------------------------------------------------------------------- #
# 响应 → 契约映射
# --------------------------------------------------------------------------- #
def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value if str(item).strip())


def _default_reader_goal(archetype: tuple[str, ...], domain: tuple[str, ...]) -> str:
    kinds = "、".join(archetype) if archetype else "论文"
    fields = "、".join(domain) if domain else ""
    tail = f"（领域：{fields}）" if fields else ""
    return f"理解这篇 {kinds} 论文的核心机制、主要结果与适用边界{tail}"


def _experiments_from_response(items: Any) -> tuple[Experiment, ...]:
    if not isinstance(items, (list, tuple)):
        return ()
    out: list[Experiment] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        out.append(Experiment(
            question=str(item.get("question", "")).strip(),
            setup=_as_tuple(item.get("setup")),
            comparison=_as_tuple(item.get("comparison")),
            results=_as_tuple(item.get("results")),
            interpretation=str(item.get("interpretation", "")).strip(),
            boundary=str(item.get("boundary", "")).strip(),
            evidence_handles=_as_tuple(item.get("evidence_handles")),
            experiment_id=str(item.get("experiment_id", "")).strip(),
        ))
    return tuple(out)


def _sections_from_response(items: Any) -> tuple[TeachingSection, ...]:
    if not isinstance(items, (list, tuple)):
        return ()
    out: list[TeachingSection] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("section_id", "")).strip()
        if not sid:
            continue
        out.append(TeachingSection(
            section_id=sid,
            title=str(item.get("title", "")).strip(),
            teaching_goal=str(item.get("teaching_goal", "")).strip(),
            evidence_targets=_as_tuple(item.get("evidence_targets")),
        ))
    return tuple(out)


# --------------------------------------------------------------------------- #
# 适配器
# --------------------------------------------------------------------------- #
class DeepSeekPedagogicalModel:
    """把 S1/S2/S5 接到生产 DeepSeek 模型上；持有资产表以便 writer 锚可渲染资产。"""

    def __init__(
        self,
        model: DeepSeekPaperReadingModel,
        *,
        assets: Mapping[str, PublishedAsset],
        candidates: Sequence[Mapping[str, Any]] = (),
        plan: Any | None = None,
    ) -> None:
        self.model = model
        self.assets = assets
        self.candidates = tuple(candidates)
        self.plan = plan
        self.text_model = model.text_model
        self.vision_model = model.vision_model

    # --- S1 ---
    def build_paper_model(self, paper: CanonicalPaperIR) -> PaperModel:
        prompt = _PAPER_MODEL_PROMPT(paper)
        response = self.model.call_json("pedagogical_paper_model", self.text_model, prompt)
        return _paper_model_from_response(response)

    # --- S2 ---
    def build_teaching_plan(self, paper_model: PaperModel) -> TeachingPlan:
        prompt = _TEACHING_PLAN_PROMPT(paper_model)
        response = self.model.call_json("pedagogical_teaching_plan", self.text_model, prompt)
        archetype = _as_tuple(response.get("paper_archetype"))
        domain = _as_tuple(response.get("domain"))
        reader_goal = str(response.get("reader_goal", "")).strip()
        if not reader_goal:
            # 模型偶发返回空 reader_goal；用 archetype/domain 派生一句默认，避免整篇 fatal。
            reader_goal = _default_reader_goal(archetype, domain)
        return TeachingPlan(
            paper_archetype=archetype,
            domain=domain,
            reader_goal=reader_goal,
            prerequisites=_as_tuple(response.get("prerequisites")),
            sections=_sections_from_response(response.get("sections")),
            running_example=str(response.get("running_example", "")).strip(),
        )

    # --- S5 ---
    def write_pedagogical_note(self, value: Mapping[str, Any]) -> str:
        candidates = value.get("candidates") or self.candidates
        if self.plan is not None:
            # S3 接管素材选择：writer 只锚 plan 选中的资产（零锚定从机制上消除）
            from .planner import selected_asset_ids

            selected = set(selected_asset_ids(self.plan))
            candidates = [c for c in candidates if c.get("asset_id") in selected]
        briefs = value.get("briefs") or {}
        prompt = _WRITER_PROMPT(value.get("paper_model", {}), value.get("teaching_plan", {}), candidates, briefs)
        response = self.model.call_json("pedagogical_note_write", self.text_model, prompt)
        markdown = str(response.get("markdown") or response.get("note") or response.get("content") or "").strip()
        if not markdown:
            raise ValueError("write_pedagogical_note 返回空正文")
        return markdown + "\n"


def _paper_model_from_response(response: Mapping[str, Any]) -> PaperModel:
    return PaperModel(
        thesis=str(response.get("thesis", "")).strip(),
        central_problem=str(response.get("central_problem", "")).strip(),
        prior_gap=str(response.get("prior_gap", "")).strip(),
        central_idea=str(response.get("central_idea", "")).strip(),
        argument_chain=_as_tuple(response.get("argument_chain")),
        experiments=_experiments_from_response(response.get("experiments")),
        must_preserve_facts=_as_tuple(response.get("must_preserve_facts")),
        limitations=_as_tuple(response.get("limitations")),
        source_facts=_as_tuple(response.get("source_facts")),
        material_unknowns=_as_tuple(response.get("material_unknowns")),
    )


# --------------------------------------------------------------------------- #
# 提示词（复用生产模型的"Return json only."约定）
# --------------------------------------------------------------------------- #
def _paper_payload_blocks(paper: CanonicalPaperIR) -> str:
    """给 S1 一篇有界、可追溯的论文视图：标题/摘要 + 代表性正文块 + 资产清单。"""
    nav = paper.navigation_blocks()
    lines = [f"# 标题: {paper.title}", f"# source_id: {paper.source_id}", ""]
    blocks_text = []
    for block in nav:
        blocks_text.append(f"[{block.block_id}|{block.kind}|{block.section}]\n{block.text}")
    if paper.abstract:
        lines.append(f"[abstract|paragraph|abstract]\n{paper.abstract}")
    else:
        lines.append("[abstract|paragraph|abstract]\n(无独立摘要块)")
    lines.append("---- 代表性正文块 ----")
    lines.extend(blocks_text)
    from collections import Counter
    counts = Counter(block.kind for block in paper.blocks)
    lines.append("---- 资产类型统计 ----")
    lines.append(str(dict(counts)))
    return "\n".join(str(line) for line in lines)


def _PAPER_MODEL_PROMPT(paper: CanonicalPaperIR) -> str:
    return json.dumps({
        "operation": "pedagogical_paper_model",
        "task": "从论文事实视图抽取论文是什么（只做事实层，不做任何'怎么讲'的教学决策）。",
        "rules": [
            "thesis 用一句话概括论文主张；central_problem/prior_gap/central_idea 各自独立、互不重复。",
            "argument_chain 按论述顺序列出核心推理步骤（每步非空）。",
            "experiments 每个实验含 question/setup/comparison/results/interpretation/boundary；数值保留原 token（条件+比较+量纲）。",
            "must_preserve_facts 列出必须逐字保留的数值、专名、机制术语；不要半句化。",
            "material_unknowns 只列论文明确未给出/未建立的部分；不要臆测。",
            "禁止编造：来源只限所给正文；不写 JSON 之外内容；空字段用 [] 或空字符串。",
        ],
        "paper_view": _paper_payload_blocks(paper),
        "return": {
            "thesis": "一句",
            "central_problem": "",
            "prior_gap": "",
            "central_idea": "",
            "argument_chain": ["..."],
            "experiments": [{"experiment_id": "", "question": "", "setup": [], "comparison": [], "results": [], "interpretation": "", "boundary": "", "evidence_handles": []}],
            "must_preserve_facts": ["..."],
            "limitations": ["..."],
            "source_facts": ["..."],
            "material_unknowns": ["..."],
        },
    }, ensure_ascii=False)


def _TEACHING_PLAN_PROMPT(paper_model: PaperModel) -> str:
    return json.dumps({
        "operation": "pedagogical_teaching_plan",
        "task": "给定论文事实层，给出给没读过原论文的技术读者的解释策略（只做'怎么讲'，不改事实）。",
        "rules": [
            "paper_archetype 从 [method, benchmark, system, empirical-study] 选（可多选，按主导标注）；**archetype 决定讲解/阅读顺序**。",
            "domain 只写论文领域/子领域（例如 LLM Safety、RLHF、Token Efficient）；与 archetype 勿混成 paper_type。",
            "reader_goal 一句话写出读者读完能判断什么；prerequisites 列出前置知识（可空）。",
            "sections 按最利于理解论文的顺序组织；每节 section_id 稳定、title 中文、teaching_goal 一句说清该节让读者获得什么、evidence_targets 指该节依赖的事实层字段/实验 id。",
            "running_example 可选：用一个贯穿示例帮助理解（无合适示例则空字符串）。",
            "不许修改/新增论文事实；不许编造 prerequisites 之外的具体数字。",
        ],
        "paper_model": asdict(paper_model),
        "return": {
            "paper_archetype": ["method"],
            "domain": ["LLM"],
            "reader_goal": "",
            "prerequisites": ["..."],
            "sections": [{"section_id": "", "title": "", "teaching_goal": "", "evidence_targets": []}],
            "running_example": "",
        },
    }, ensure_ascii=False)


def _WRITER_PROMPT(paper_model: Any, teaching_plan: Any, candidates: Sequence[Mapping[str, Any]], briefs: Mapping[str, Any]) -> str:
    candidate_brief = [
        {"asset_id": c.get("asset_id"), "kind": c.get("kind"), "caption": c.get("caption"),
         "renderable": bool(c.get("renderable")),
         "evidence_capability": c.get("evidence_capability", "exact"),
         "preview": c.get("preview", "")}
        for c in candidates
    ]
    return json.dumps({
        "operation": "pedagogical_note_write",
        "task": "写一篇中文教学化论文笔记（读者没读过原论文）。按 teaching_plan.sections 组织，每节达成其 teaching_goal。",
        "rules": [
            "统一 reader_goal 口吻，可用 running_example 贯穿；每节先讲'为什么要看它/解决什么判断困难'再讲内容。",
            "涉及已有图片/表格/公式时，正文用 {{asset:<asset_id>}} 标记其出现位置（锚点）；**只锚 candidates 里 renderable=true 的资产**，其余一律在正文里用文字讲清，不假装渲染。",
            "**严禁零锚定**：凡你在正文里引用的具体数值、对比结论或机制，若来自 candidates 中某张表/图/公式，必须用 {{asset:<id>}} 锚定它（并配合讲清它解决什么判断困难），而不是只把数字散文化复述一遍。引用即锚定。",
            "asset_candidates.evidence_capability=exact 才允许支撑精确数字、公式转写或行列比较；descriptive 只能说明素材主题、作用和阅读方向，不得作为精确事实的唯一来源。",
            "已有 asset_id 的表格/公式/图片必须用 {{asset:<asset_id>}} 交给 Renderer；不得重新构造对应 Markdown 表、公式或图片链接来绕过素材质量与表示选择。",
            "公式讲清符号含义与它解决什么判断困难，不逐字抄读；数值带条件与比较。",
            "图的解读只来自 caption/上下文（非像素）；不要描述你没有看到的图内细节。",
            "不编造证据、不夸大结论、不写内部字段/证据柄/路径/block_id；不写图片 URL（渲染器负责放图）。",
            "结尾明确适用边界与局限（来自 PaperModel.limitations / material_unknowns）。",
            "返回 JSON：{\"markdown\": \"完整中文笔记 markdown\"}。",
        ],
        "paper_model": paper_model,
        "teaching_plan": teaching_plan,
        "asset_candidates": candidate_brief,
        "extra_briefs": briefs,
        "return": {"markdown": "完整正文"},
    }, ensure_ascii=False)


import re  # noqa: E402  (used by pipe_table_from_text; keep import visible)
