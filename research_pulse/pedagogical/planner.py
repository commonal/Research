"""S3 AssetPlanner — 基于教学目标的语义选择 + 确定性 fallback。

主路径由 ``SemanticAssetPlanner`` 消费 ``PaperModel``、``TeachingPlan`` 与简化候选，
只判断素材是否有不可替代的解释价值。``plan_assets`` 保留为无模型/异常时的
确定性 fallback：按 kind 分组，以 caption 与出现顺序排序并执行每类预算。

Writer 只能锚 ``choices`` 里 decision=inline|reference 的资产——零锚定问题
从机制上消除：S3 明确选了什么，S5 没有理由不写。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Mapping, Sequence

from .contracts import ASSET_BUDGETS, AssetChoice, AssetPlan, PaperModel, TeachingPlan

_PLACEHOLDER_CAPTIONS = {"figure", "fig", "table", "formula", "image", "图", "表", "公式", "figure 1", "table 1"}


class SemanticAssetPlanner:
    """基于论文内容与教学目标选择少量有解释价值的素材。

    接口只返回 ``AssetPlan``；表示选择、渲染 fallback 与素材质量诊断仍由
    AssetPreparation/Renderer 负责。模型输出无效时退回确定性 Planner。
    """

    def __init__(self, model: object, *, max_selected: int = 4) -> None:
        self.model = model
        self.max_selected = max_selected

    def plan(
        self,
        paper_model: PaperModel,
        teaching_plan: TeachingPlan,
        candidates: Sequence[Mapping],
    ) -> AssetPlan:
        eligible = {
            str(candidate.get("asset_id") or ""): candidate
            for candidate in candidates
            if candidate.get("renderable")
            and candidate.get("kind") in ASSET_BUDGETS
            and candidate.get("asset_id")
        }
        if not eligible:
            return AssetPlan()

        def deterministic_fallback(reason: str) -> AssetPlan:
            fallbacks = getattr(self.model, "fallbacks", None)
            if isinstance(fallbacks, list):
                fallbacks.append(f"pedagogical_asset_plan:{reason}_fallback")
            return plan_assets(candidates)

        semantic_candidates = [
            {
                "asset_id": asset_id,
                "kind": candidate.get("kind"),
                "caption": candidate.get("caption") or "",
                "evidence_capability": candidate.get("evidence_capability") or "none",
                "warnings": list(candidate.get("warnings") or ()),
            }
            for asset_id, candidate in eligible.items()
        ]
        prompt = json.dumps(
            {
                "task": (
                    "Select only assets that materially help explain the paper to the reader. "
                    "Prefer assets supporting the thesis, mechanism, or decisive experiments. "
                    f"Return at most {self.max_selected} choices. Do not discuss render formats."
                ),
                "paper_model": asdict(paper_model),
                "teaching_plan": asdict(teaching_plan),
                "candidates": semantic_candidates,
                "output": {
                    "choices": [
                        {
                            "asset_id": "candidate id",
                            "decision": "inline or reference",
                            "rationale": "specific explanatory value",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        try:
            payload = self.model.call_json(
                "pedagogical_asset_plan",
                getattr(self.model, "text_model", ""),
                prompt,
            )
        except Exception:
            return deterministic_fallback("call_failed")

        raw_choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if not isinstance(raw_choices, list):
            return deterministic_fallback("invalid_response")

        choices: list[AssetChoice] = []
        seen: set[str] = set()
        for item in raw_choices:
            if not isinstance(item, Mapping):
                continue
            asset_id = str(item.get("asset_id") or "")
            decision = str(item.get("decision") or "")
            if asset_id not in eligible or asset_id in seen or decision not in {"inline", "reference"}:
                continue
            candidate = eligible[asset_id]
            choices.append(AssetChoice(
                asset_id=asset_id,
                kind=str(candidate.get("kind")),
                decision=decision,
                rationale=str(item.get("rationale") or "具有教学解释价值"),
            ))
            seen.add(asset_id)
            if len(choices) >= self.max_selected:
                break

        if raw_choices and not choices:
            return deterministic_fallback("invalid_choices")

        overrides = tuple(
            f"{asset_id} 未被语义 Planner 选中：对当前教学目标不是优先素材"
            for asset_id in eligible
            if asset_id not in seen
        )
        return AssetPlan(choices=tuple(choices), overrides=overrides)


def _explanation_value(candidate: Mapping) -> tuple[int, int]:
    """解释价值排序键：(有实质 caption, 出现顺序)。越靠前价值越高。"""
    caption = str(candidate.get("caption") or "").strip().casefold()
    has_caption = 0 if caption in _PLACEHOLDER_CAPTIONS or not caption else 1
    order = int(candidate.get("order") or 0)
    return (-has_caption, order)


def plan_assets(
    candidates: Sequence[Mapping],
    *,
    budgets: Mapping[str, int] | None = None,
) -> AssetPlan:
    """从资产候选清单产 ``AssetPlan``。

    ``candidates`` 元素须含 ``asset_id`` / ``kind`` / ``caption`` / ``order``；
    只考虑 ``renderable`` 为真的候选（不可渲染的资产不占预算、直接 omit）。
    """
    budgets = budgets or ASSET_BUDGETS
    groups: dict[str, list[Mapping]] = {}
    for candidate in candidates:
        if not candidate.get("renderable"):
            continue
        kind = str(candidate.get("kind") or "")
        if kind not in budgets:
            continue
        groups.setdefault(kind, []).append(candidate)

    choices: list[AssetChoice] = []
    overrides: list[str] = []
    for kind in ("formula", "figure", "table"):
        items = sorted(groups.get(kind, ()), key=_explanation_value)
        budget = budgets[kind]
        for index, candidate in enumerate(items):
            asset_id = str(candidate.get("asset_id") or "")
            if index < budget:
                caption = str(candidate.get("caption") or "").strip() or asset_id
                choices.append(AssetChoice(
                    asset_id=asset_id,
                    kind=kind,
                    decision="inline",
                    rationale=f"{kind} #{index + 1}：{caption[:60]}（解释价值优先，预算 {budget} 内）",
                ))
            else:
                overrides.append(
                    f"{asset_id} 超 {kind} 预算（{budget}），omit —— 解释价值排序第 {index + 1}"
                )
    return AssetPlan(choices=tuple(choices), overrides=tuple(overrides))


def selected_asset_ids(plan: AssetPlan) -> tuple[str, ...]:
    """S3 选中的、Writer 应锚定的资产 id（inline | reference）。"""
    return tuple(
        choice.asset_id for choice in plan.choices if choice.decision in ("inline", "reference")
    )
