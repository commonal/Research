"""S4 AssetInterpreter 测试（T10）：公式/表 briefs + 图 caption 模式（不联网）。

覆盖：公式/表 brief 解析、图 source=caption 零调用、空响应降级、
LLM 失败 degraded 不影响他项、asset_id 过滤只留选中的。
"""

from __future__ import annotations

import unittest

from research_pulse.pedagogical.contracts import AssetBriefs, AssetChoice, AssetPlan
from research_pulse.pedagogical.interpreter import interpret_assets
from research_pulse.pedagogical.renderer import PublishedAsset


def _plan(*choices: AssetChoice) -> AssetPlan:
    return AssetPlan(choices=tuple(choices))


def _asset(asset_id: str, kind: str, *, markdown: str = "", caption: str = "") -> PublishedAsset:
    return PublishedAsset(asset_id=asset_id, kind=kind, markdown=markdown or None, caption=caption)


class _FakeModel:
    def __init__(self, response: dict | None = None, *, boom: bool = False) -> None:
        self._response = response
        self._boom = boom
        self.calls: list[tuple[str, str, str]] = []
        self.text_model = "fake-text"

    def call_json(self, operation: str, model: str, prompt: str, image: str | None = None) -> dict:
        self.calls.append((operation, model, prompt))
        if self._boom:
            raise RuntimeError("LLM 挂了")
        return self._response or {}


def _assets_map() -> dict[str, PublishedAsset]:
    return {
        "formula-01": _asset("formula-01", "formula", markdown="$$x^2$$", caption="式1"),
        "formula-02": _asset("formula-02", "formula", markdown="$$y=ax+b$$", caption="式2"),
        "table-01": _asset("table-01", "table", markdown="| A | B |", caption="表1"),
        "figure-01": _asset("figure-01", "figure", caption="图1：架构"),
    }


class InterpretAssetsTests(unittest.TestCase):
    def test_formula_and_table_briefs_parsed(self) -> None:
        model = _FakeModel({
            "formulas": [
                {"asset_id": "formula-01", "role": "注意力定义", "plain_explanation": "把 query 和 key 做内积",
                 "symbol_meanings": ["Q: 查询向量", "K: 键向量"], "unknowns": []},
            ],
            "tables": [
                {"asset_id": "table-01", "role": "对比实验", "plain_explanation": "新方法全面领先",
                 "reading_notes": ["看 Acc 列"]},
            ],
        })
        plan = _plan(
            AssetChoice("formula-01", "formula", "inline", ""),
            AssetChoice("table-01", "table", "inline", ""),
            AssetChoice("figure-01", "figure", "inline", ""),
        )
        briefs = interpret_assets(model, plan, _assets_map())
        self.assertEqual(len(briefs.formulas), 1)
        self.assertEqual(briefs.formulas[0].asset_id, "formula-01")
        self.assertEqual(briefs.formulas[0].symbol_meanings, ("Q: 查询向量", "K: 键向量"))
        self.assertEqual(len(briefs.tables), 1)
        self.assertEqual(briefs.tables[0].reading_notes, ("看 Acc 列",))
        # 图：caption 模式，零 LLM 调用
        self.assertEqual(briefs.figures[0].source, "caption")
        self.assertEqual(briefs.figures[0].interpretation, "图1：架构")
        self.assertEqual(model.calls[0][0], "pedagogical_asset_briefs")
        self.assertEqual(len(model.calls), 1)  # 图不额外调用

    def test_figure_only_plan_makes_no_llm_call(self) -> None:
        model = _FakeModel()
        plan = _plan(AssetChoice("figure-01", "figure", "inline", ""))
        briefs = interpret_assets(model, plan, _assets_map())
        self.assertEqual(briefs.formulas, ())
        self.assertEqual(briefs.tables, ())
        self.assertEqual(len(briefs.figures), 1)
        self.assertEqual(model.calls, [], "只有图 → 不应调 LLM")

    def test_empty_response_degrades_gracefully(self) -> None:
        model = _FakeModel(response={})
        plan = _plan(AssetChoice("formula-01", "formula", "inline", ""))
        briefs = interpret_assets(model, plan, _assets_map())
        self.assertEqual(briefs.formulas, ())
        self.assertEqual(briefs.tables, ())

    def test_llm_failure_degrades_but_figures_survive(self) -> None:
        model = _FakeModel(boom=True)
        plan = _plan(
            AssetChoice("formula-01", "formula", "inline", ""),
            AssetChoice("figure-01", "figure", "inline", ""),
        )
        briefs = interpret_assets(model, plan, _assets_map())
        self.assertEqual(briefs.formulas, ())
        self.assertEqual(briefs.tables, ())
        # 图不受影响（caption 模式零调用）
        self.assertEqual(len(briefs.figures), 1)
        self.assertEqual(briefs.figures[0].interpretation, "图1：架构")

    def test_omitted_and_unknown_assets_filtered(self) -> None:
        model = _FakeModel({
            "formulas": [
                {"asset_id": "formula-01", "role": "r1", "plain_explanation": "e1"},
                {"asset_id": "formula-99", "role": "r9", "plain_explanation": "e9"},  # 不在 assets → 被过滤
            ],
        })
        plan = _plan(
            AssetChoice("formula-01", "formula", "inline", ""),
            AssetChoice("formula-02", "formula", "omit", "超预算"),  # omit 不进 brief
        )
        briefs = interpret_assets(model, plan, _assets_map())
        self.assertEqual([b.asset_id for b in briefs.formulas], ["formula-01"])


if __name__ == "__main__":
    unittest.main()
