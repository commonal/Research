"""S4 AssetInterpreter — 素材解释（公式/表 briefs；图默认 caption 模式）。

- 公式 + 表：**一次** LLM 调用产 briefs（role / plain_explanation / symbol_meanings / unknowns），
  喂给 Writer 讲素材时用。调用失败/空响应 → 空 briefs（Writer 降级纯 caption，不阻断）。
- 图：默认 ``source=caption``（零调用、确定性），解释用已清洗的 caption；
  单图失败不影响他项（tasks.md T10：degraded 语义）。vision pixels 模式留到成本优化阶段按需接。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .contracts import AssetBriefs, AssetPlan, FigureBrief, FormulaBrief, TableBrief

_SELECTED = ("inline", "reference")


def interpret_assets(
    model: Any,
    plan: AssetPlan,
    assets: Mapping[str, Any],
) -> AssetBriefs:
    """按 ``AssetPlan`` 对选中的公式/表/图产 briefs。永不抛异常（失败降级为空 briefs）。"""
    selected = [c for c in plan.choices if c.decision in _SELECTED]
    formulas = [assets[c.asset_id] for c in selected if c.kind == "formula" and c.asset_id in assets]
    tables = [assets[c.asset_id] for c in selected if c.kind == "table" and c.asset_id in assets]
    figures = [assets[c.asset_id] for c in selected if c.kind == "figure" and c.asset_id in assets]

    formula_briefs: tuple[FormulaBrief, ...] = ()
    table_briefs: tuple[TableBrief, ...] = ()
    selected_ids = {c.asset_id for c in selected}
    if formulas or tables:
        try:
            response = model.call_json(
                "pedagogical_asset_briefs",
                getattr(model, "text_model", "deepseek-v4-flash"),
                _briefs_prompt(formulas, tables),
            )
            formula_briefs = tuple(b for b in _formula_briefs_from_response(response) if b.asset_id in selected_ids)
            table_briefs = tuple(b for b in _table_briefs_from_response(response) if b.asset_id in selected_ids)
        except Exception:
            formula_briefs, table_briefs = (), ()   # degraded：Writer 用 caption 兜底

    figure_briefs = tuple(
        FigureBrief(
            asset_id=asset.asset_id,
            source="caption",
            interpretation=str(getattr(asset, "caption", "") or ""),
        )
        for asset in figures
    )

    return AssetBriefs(
        formulas=formula_briefs,
        tables=table_briefs,
        figures=figure_briefs,
    )


def _briefs_prompt(formulas: Sequence[Any], tables: Sequence[Any]) -> str:
    def _view(asset: Any) -> dict[str, str]:
        return {
            "asset_id": asset.asset_id,
            "kind": asset.kind,
            "caption": asset.caption,
            "content": (asset.markdown or "")[:4000],
        }

    return json.dumps({
        "operation": "pedagogical_asset_briefs",
        "task": "为教学化笔记解释下面的公式与表格素材：每个素材讲清它'在论证里起什么作用'（role）与'一句白话解释'（plain_explanation）。",
        "rules": [
            "公式：symbol_meanings 逐条列符号含义（格式 '符号: 含义'）；不确定的进 unknowns。",
            "表格：reading_notes 说明怎么读（看哪列、注意什么）。",
            "只解释给出的素材，不编造素材里没有的结论；brief 是给 writer 的提示，不是最终成文。",
        ],
        "formulas": [_view(a) for a in formulas],
        "tables": [_view(a) for a in tables],
        "please_output_json_exactly": {
            "formulas": [{"asset_id": "", "role": "", "plain_explanation": "", "symbol_meanings": [], "unknowns": []}],
            "tables": [{"asset_id": "", "role": "", "plain_explanation": "", "reading_notes": []}],
        },
    }, ensure_ascii=False)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return tuple(str(item) for item in value if str(item).strip())


def _formula_briefs_from_response(response: Mapping[str, Any]) -> tuple[FormulaBrief, ...]:
    out: list[FormulaBrief] = []
    for item in (response.get("formulas") or ()):
        if not isinstance(item, Mapping) or not str(item.get("asset_id") or "").strip():
            continue
        out.append(FormulaBrief(
            asset_id=str(item["asset_id"]).strip(),
            role=str(item.get("role") or "").strip(),
            plain_explanation=str(item.get("plain_explanation") or "").strip(),
            symbol_meanings=_as_tuple(item.get("symbol_meanings")),
            unknowns=_as_tuple(item.get("unknowns")),
        ))
    return tuple(out)


def _table_briefs_from_response(response: Mapping[str, Any]) -> tuple[TableBrief, ...]:
    out: list[TableBrief] = []
    for item in (response.get("tables") or ()):
        if not isinstance(item, Mapping) or not str(item.get("asset_id") or "").strip():
            continue
        out.append(TableBrief(
            asset_id=str(item["asset_id"]).strip(),
            role=str(item.get("role") or "").strip(),
            plain_explanation=str(item.get("plain_explanation") or "").strip(),
            reading_notes=_as_tuple(item.get("reading_notes")),
        ))
    return tuple(out)
