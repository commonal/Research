"""S3 AssetPlanner 测试（T9）：预算化素材选择。

覆盖：预算内全选、超预算 omit + override 依据、不可渲染不占预算、
有实质 caption 优先、按论文出现顺序稳定、selected_asset_ids 契约。
"""

from __future__ import annotations

import unittest

from research_pulse.pedagogical.contracts import (
    ASSET_BUDGETS,
    AssetChoice,
    AssetPlan,
    Experiment,
    PaperModel,
    TeachingPlan,
    TeachingSection,
)
from research_pulse.pedagogical.planner import SemanticAssetPlanner, plan_assets, selected_asset_ids


def _cand(asset_id: str, kind: str, *, caption: str = "", renderable: bool = True, order: int = 0) -> dict:
    return {"asset_id": asset_id, "kind": kind, "caption": caption, "renderable": renderable, "order": order}


class PlanAssetsTests(unittest.TestCase):
    def test_budget_within_limits_selects_all(self) -> None:
        candidates = [
            _cand("formula-01", "formula", caption="式1：注意力", order=0),
            _cand("figure-01", "figure", caption="图1：架构", order=1),
            _cand("table-01", "table", caption="表1：对比", order=2),
        ]
        plan = plan_assets(candidates)
        self.assertEqual(len(plan.choices), 3)
        self.assertTrue(all(c.decision == "inline" for c in plan.choices))
        self.assertEqual(plan.overrides, ())

    def test_over_budget_omits_and_records_override(self) -> None:
        # 5 个公式（预算 4）→ 第 5 个 omit，override 记录依据
        candidates = [_cand(f"formula-{i:02d}", "formula", caption=f"式{i}", order=i) for i in range(1, 6)]
        plan = plan_assets(candidates)
        self.assertEqual(ASSET_BUDGETS["formula"], 4)
        inline = [c for c in plan.choices if c.decision == "inline"]
        self.assertEqual(len(inline), 4)
        self.assertEqual(inline[0].asset_id, "formula-01")
        self.assertEqual(inline[-1].asset_id, "formula-04")
        self.assertTrue(plan.overrides, "超预算必须有 override 依据")
        self.assertIn("formula-05", plan.overrides[0])
        self.assertIn("预算", plan.overrides[0])

    def test_figure_and_table_budgets(self) -> None:
        # 4 图（预算 3）+ 4 表（预算 3）→ 各 omit 一个
        candidates = (
            [_cand(f"figure-{i:02d}", "figure", caption=f"图{i}", order=i) for i in range(1, 5)]
            + [_cand(f"table-{i:02d}", "table", caption=f"表{i}", order=i) for i in range(1, 5)]
        )
        plan = plan_assets(candidates)
        figures = [c for c in plan.choices if c.kind == "figure"]
        tables = [c for c in plan.choices if c.kind == "table"]
        self.assertEqual(len(figures), 3)
        self.assertEqual(len(tables), 3)
        self.assertEqual(len(plan.overrides), 2)

    def test_unrenderable_assets_do_not_consume_budget(self) -> None:
        # 3 个不可渲染公式不占预算 → 2 个可渲染全选（预算 4 内）
        candidates = [
            _cand("formula-01", "formula", caption="式1", renderable=False, order=0),
            _cand("formula-02", "formula", caption="式2", renderable=False, order=1),
            _cand("formula-03", "formula", caption="式3", renderable=True, order=2),
            _cand("formula-04", "formula", caption="式4", renderable=True, order=3),
        ]
        plan = plan_assets(candidates)
        self.assertEqual(len(plan.choices), 2)
        self.assertEqual(plan.overrides, ())

    def test_caption_value_ranks_over_order(self) -> None:
        # 有实质 caption 的排在无 caption 的前面（解释价值优先）
        candidates = [
            _cand("formula-01", "formula", caption="", order=0),
            _cand("formula-02", "formula", caption="式2：负载均衡", order=1),
            _cand("formula-03", "formula", caption="figure", order=2),  # 占位 caption 视为无
        ]
        plan = plan_assets(candidates)
        ids = [c.asset_id for c in plan.choices]
        self.assertEqual(ids[0], "formula-02")
        self.assertIn("formula-01", ids)
        self.assertIn("formula-03", ids)  # 预算 4 够，占位 caption 也会选（只是排后面）
        # 预算只有 2 时，占位 caption 的会被挤出
        plan2 = plan_assets(candidates, budgets={"formula": 2, "figure": 3, "table": 3})
        self.assertEqual([c.asset_id for c in plan2.choices], ["formula-02", "formula-01"])

    def test_selected_asset_ids_filters_omits(self) -> None:
        candidates = [
            _cand("formula-01", "formula", caption="式1", order=0),
            _cand("figure-01", "figure", caption="图1", order=1),
        ]
        plan = plan_assets(candidates)
        self.assertEqual(selected_asset_ids(plan), ("formula-01", "figure-01"))
        # omit 决策不进 selected
        plan2 = AssetPlan(choices=(
            AssetChoice("a", "formula", "inline", ""),
            AssetChoice("b", "figure", "omit", ""),
            AssetChoice("c", "table", "reference", ""),
        ))
        self.assertEqual(selected_asset_ids(plan2), ("a", "c"))

    def test_empty_candidates_yields_empty_plan(self) -> None:
        plan = plan_assets([])
        self.assertEqual(plan.choices, ())
        self.assertEqual(plan.overrides, ())


class _SemanticPlanningModel:
    text_model = "fake-planner"

    def call_json(self, operation: str, model: str, prompt: str) -> dict:
        return {
            "choices": [
                {
                    "asset_id": "figure-03",
                    "decision": "inline",
                    "rationale": "直接呈现核心的人类与 Agent 对比结果",
                }
            ]
        }


class SemanticAssetPlannerTests(unittest.TestCase):
    def test_selects_late_asset_that_supports_core_experiment(self) -> None:
        candidates = [
            _cand("figure-01", "figure", caption="System overview", order=1),
            _cand("table-01", "table", caption="Task mechanics", order=2),
            _cand(
                "figure-03",
                "figure",
                caption="Main result: human versus agent performance",
                order=9,
            ),
        ]
        paper_model = PaperModel(
            thesis="Agent performance must be compared with human performance.",
            experiments=(
                Experiment(
                    question="How do agents compare with humans?",
                    results=("Humans outperform current agents.",),
                ),
            ),
        )
        teaching_plan = TeachingPlan(
            paper_archetype=("benchmark",),
            domain=("AI Agents",),
            reader_goal="Understand the main benchmark result.",
            sections=(
                TeachingSection(
                    section_id="results",
                    title="Main results",
                    teaching_goal="Explain the human-agent performance gap.",
                ),
            ),
        )

        plan = SemanticAssetPlanner(_SemanticPlanningModel()).plan(
            paper_model,
            teaching_plan,
            candidates,
        )

        self.assertEqual(selected_asset_ids(plan), ("figure-03",))
        self.assertEqual(plan.choices[0].kind, "figure")

    def test_invalid_model_response_falls_back_and_records_reason(self) -> None:
        class _InvalidModel:
            text_model = "fake-planner"

            def __init__(self) -> None:
                self.fallbacks: list[str] = []

            def call_json(self, operation: str, model: str, prompt: str) -> dict:
                return {"unexpected": []}

        model = _InvalidModel()
        plan = SemanticAssetPlanner(model).plan(
            PaperModel(thesis="A test thesis"),
            TeachingPlan(
                paper_archetype=("method",),
                domain=("AI",),
                reader_goal="Understand the method.",
            ),
            [_cand("figure-01", "figure", caption="Method overview", order=1)],
        )

        self.assertEqual(selected_asset_ids(plan), ("figure-01",))
        self.assertIn("pedagogical_asset_plan:invalid_response_fallback", model.fallbacks)


if __name__ == "__main__":
    unittest.main()
