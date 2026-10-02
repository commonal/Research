"""域契约测试（T1/T2）：PaperModel 与 TeachingPlan 严格分离；资产锚协议校验。"""

from __future__ import annotations

import unittest

from research_pulse.pedagogical.contracts import (
    ARCHETYPES,
    AssetAnchor,
    BlindReaderResult,
    Experiment,
    PaperModel,
    TeachingPlan,
    TeachingSection,
    validate_contracts,
    parse_note_anchors,
)


class PaperModelTeachingPlanSeparationTests(unittest.TestCase):
    def test_paper_model_has_no_teaching_fields(self) -> None:
        model = PaperModel(thesis="t", experiments=(Experiment(question="q"),))
        for field in ("archetype", "reader_goal", "prerequisites", "sections", "running_example", "domain"):
            self.assertNotIn(field, model.__dataclass_fields__)

    def test_teaching_plan_has_no_fact_fields(self) -> None:
        plan = TeachingPlan(paper_archetype=("method",), domain=("LLM Safety",), reader_goal="g")
        for field in ("thesis", "source_facts", "experiments", "material_unknowns"):
            self.assertNotIn(field, plan.__dataclass_fields__)

    def test_archetype_must_be_valid(self) -> None:
        with self.assertRaises(ValueError):
            validate_contracts(
                PaperModel(thesis="t"),
                TeachingPlan(paper_archetype=("nonsense",), domain=(), reader_goal="g"),
            )

    def test_required_fields(self) -> None:
        with self.assertRaises(ValueError):
            validate_contracts(PaperModel(thesis="  "), TeachingPlan(paper_archetype=(), domain=(), reader_goal="g"))
        with self.assertRaises(ValueError):
            validate_contracts(PaperModel(thesis="t"), TeachingPlan(paper_archetype=(), domain=(), reader_goal=" "))


class AssetAnchorTests(unittest.TestCase):
    def test_parse_note_anchors(self) -> None:
        anchors = {
            "fig_1": AssetAnchor("a1", "fig_1", "core", "after_paragraph", "inline"),
        }
        found = parse_note_anchors("正文…{{asset:fig_1}}…", anchors)
        self.assertEqual([a.asset_id for a in found], ["fig_1"])
        self.assertEqual(found[0].render_mode, "inline")

    def test_undeclared_asset_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_note_anchors("{{asset:unknown_asset}}", {})

    def test_anchor_defaults(self) -> None:
        a = AssetAnchor("a1", "fig_1", "sec", "after_paragraph", "inline")
        self.assertIn(a.placement, ("after_paragraph", "before_paragraph", "end_of_section", "after_table"))
        self.assertIn(a.render_mode, ("inline", "reference"))


if __name__ == "__main__":
    unittest.main()
