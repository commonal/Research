"""AssetPreparation：多表示质量判断与确定性图片回退。"""

from __future__ import annotations

import unittest
import json
from pathlib import Path

from research_pulse.pedagogical.asset_preparation import AssetPreparation
from research_pulse.pedagogical.contracts import AssetAnchor, NoteDraftWithAnchors
from research_pulse.pedagogical.renderer import DeterministicRenderer
from research_pulse.production.reading import PaperIRBlock


class AssetPreparationTests(unittest.TestCase):
    def test_planner_assets_keep_their_canonical_source_block_identity(self) -> None:
        blocks = (
            PaperIRBlock(
                "source-table-results",
                "table",
                "Results",
                "Method Score Ours 98.4",
                1,
                table_html=(
                    "<table><tr><th>Method</th><th>Score</th></tr>"
                    "<tr><td>Ours</td><td>98.4</td></tr></table>"
                ),
            ),
            PaperIRBlock(
                "source-formula-objective",
                "formula",
                "Method",
                "L equals x squared",
                2,
                latex="L=x^2",
            ),
        )

        catalog = AssetPreparation.prepare(blocks)

        self.assertEqual(
            {
                candidate["asset_id"]: candidate["source_block_id"]
                for candidate in catalog.planner_candidates
            },
            {
                "table-01": "source-table-results",
                "formula-01": "source-formula-objective",
            },
        )

    def test_verified_figure_is_usable_without_rechecking_resolved_path_shape(self) -> None:
        root = Path(__file__).resolve().parent / "fixtures"
        block = PaperIRBlock(
            "fig1",
            "figure",
            "Method",
            "Architecture overview",
            1,
            caption="Figure 1: Architecture",
            image_path="one_pixel.ppm",
            safe_image=True,
        )

        catalog = AssetPreparation.prepare((block,), asset_root=root)

        candidate = catalog.planner_candidates[0]
        assessment = catalog.diagnostics[0]
        self.assertEqual(assessment.status, "usable")
        self.assertNotIn("unsafe_image_path", assessment.reasons)
        self.assertTrue(candidate["renderable"])
        self.assertEqual(candidate["asset_quality"], "usable")
        self.assertEqual(candidate["evidence_capability"], "descriptive")
        self.assertEqual(catalog.publishable_assets["figure-01"].selected_format, "image")

    def test_degraded_table_falls_back_to_existing_image_without_hiding_from_planner(self) -> None:
        root = Path(__file__).resolve().parent / "fixtures"
        block = PaperIRBlock(
            "t1",
            "table",
            "Experiments",
            (
                "| Method | A | B | C |\n"
                "| --- | --- | --- | --- |\n"
                "| Proposed | 55.8±1.8 | 73.4±1.2 39.9±1.0 44.2±1.1 | 13.8±0.6 |"
            ),
            1,
            caption="Table 1: Main results",
            image_path="one_pixel.ppm",
        )

        catalog = AssetPreparation.prepare((block,), asset_root=root)

        candidate = catalog.planner_candidates[0]
        asset = catalog.publishable_assets["table-01"]
        self.assertTrue(candidate["renderable"])
        self.assertEqual(candidate["asset_quality"], "degraded")
        self.assertEqual(candidate["evidence_capability"], "descriptive")
        self.assertEqual(candidate["preview"], "")
        self.assertEqual(asset.selected_format, "image")
        self.assertIsNone(asset.markdown)

        note = DeterministicRenderer(catalog.publishable_assets).render(
            NoteDraftWithAnchors(
                "结果如下：{{asset:table-01}}",
                (AssetAnchor("a1", "table-01", "results"),),
            )
        )
        self.assertIn("![表 1](assets/one_pixel.ppm)", note.markdown)
        self.assertNotIn("73.4±1.2 39.9±1.0", note.markdown)
        self.assertIn("结构化解析质量不足", note.markdown)

    def test_writer_receives_evidence_capability_and_cannot_rebuild_known_table(self) -> None:
        from research_pulse.pedagogical.deepseek_adapters import _WRITER_PROMPT

        payload = json.loads(_WRITER_PROMPT(
            {},
            {},
            ({
                "asset_id": "table-01",
                "kind": "table",
                "caption": "Main results",
                "renderable": True,
                "evidence_capability": "descriptive",
                "preview": "",
            },),
            {},
        ))

        self.assertEqual(payload["asset_candidates"][0]["evidence_capability"], "descriptive")
        rules = "\n".join(payload["rules"])
        self.assertIn("descriptive", rules)
        self.assertIn("不得重新构造", rules)
        self.assertNotIn("要么直接以 markdown 管道表呈现", rules)

    def test_image_only_formula_is_publishable_but_descriptive(self) -> None:
        root = Path(__file__).resolve().parent / "fixtures"
        block = PaperIRBlock(
            "f1",
            "formula",
            "Method",
            "Equation image",
            1,
            caption="Equation 1",
            image_path="one_pixel.ppm",
        )

        catalog = AssetPreparation.prepare((block,), asset_root=root)

        candidate = catalog.planner_candidates[0]
        asset = catalog.publishable_assets["formula-01"]
        self.assertTrue(candidate["renderable"])
        self.assertEqual(candidate["evidence_capability"], "descriptive")
        self.assertEqual(asset.selected_format, "image")

    def test_truncated_image_with_valid_header_is_rejected(self) -> None:
        root = Path(__file__).resolve().parent / "fixtures"
        block = PaperIRBlock(
            "f1",
            "formula",
            "Method",
            "Equation image",
            1,
            image_path="truncated.ppm",
        )

        catalog = AssetPreparation.prepare((block,), asset_root=root)

        self.assertFalse(catalog.planner_candidates[0]["renderable"])
        self.assertNotIn("formula-01", catalog.publishable_assets)

    def test_absolute_image_outside_trusted_asset_root_is_rejected(self) -> None:
        fixtures = Path(__file__).resolve().parent / "fixtures"
        block = PaperIRBlock(
            "f1",
            "formula",
            "Method",
            "Equation image",
            1,
            image_path=str((fixtures / "one_pixel.ppm").resolve()),
        )

        catalog = AssetPreparation.prepare((block,), asset_root=fixtures / "trusted-root")

        self.assertFalse(catalog.planner_candidates[0]["renderable"])
        self.assertNotIn("formula-01", catalog.publishable_assets)


class AssetPreparationPipelineTests(unittest.TestCase):
    def test_pipeline_uses_prepared_image_fallback(self) -> None:
        from research_pulse.pedagogical.contracts import BlindReaderResult, EvidenceGateResult
        from research_pulse.pedagogical.pipeline import PedagogicalService
        from research_pulse.production.reading import CanonicalPaperIR

        class Backend:
            text_model = "fake"
            vision_model = None

            def call_json(self, operation, model, prompt, image=None):  # noqa: ANN001
                payload = json.loads(prompt)
                if operation == "pedagogical_paper_model":
                    return {"thesis": "t", "argument_chain": [], "experiments": []}
                if operation == "pedagogical_teaching_plan":
                    return {"paper_archetype": ["method"], "domain": [], "reader_goal": "理解方法"}
                if operation == "pedagogical_asset_briefs":
                    return {"formulas": [], "tables": []}
                if operation == "pedagogical_note_write":
                    self.assert_fallback(payload["asset_candidates"])
                    return {"markdown": "# 笔记\n\n{{asset:table-01}}"}
                raise AssertionError(operation)

            @staticmethod
            def assert_fallback(candidates):  # noqa: ANN001
                assert candidates[0]["asset_id"] == "table-01"
                assert candidates[0]["renderable"] is True

        class PassEvidence:
            def evaluate(self, note, paper_model):  # noqa: ANN001
                return EvidenceGateResult(passed=True)

        class PassBlind:
            def read(self, note):  # noqa: ANN001
                return BlindReaderResult(overall="pass")

        image = Path(__file__).resolve().parent / "fixtures" / "one_pixel.ppm"
        paper = CanonicalPaperIR(
            "p",
            "Paper",
            blocks=(
                PaperIRBlock("intro", "paragraph", "Introduction", "正文", 0),
                PaperIRBlock(
                    "t1",
                    "table",
                    "Experiments",
                    "| Method | A |\n| --- | --- |\n| Ours | 1±0.1 2±0.2 |",
                    1,
                    caption="Main results",
                    image_path=str(image),
                ),
            ),
        )

        result = PedagogicalService(
            Backend(),
            asset_root=image.parent,
            evidence_gate=PassEvidence(),
            blind_reader=PassBlind(),
            max_repairs=0,
        ).run(paper)

        self.assertIn("assets/one_pixel.ppm", result.note.markdown)
        self.assertNotIn("1±0.1 2±0.2", result.note.markdown)


if __name__ == "__main__":
    unittest.main()
