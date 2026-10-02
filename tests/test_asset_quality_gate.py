"""AssetQualityGate：素材进入 Planner 前的确定性质量分级。"""

from __future__ import annotations

import json
import unittest

from research_pulse.pedagogical.asset_quality import AssetQualityGate
from research_pulse.pedagogical.renderer import PublishedAsset


class AssetQualityGateTests(unittest.TestCase):
    def test_table_of_contents_is_unavailable_and_not_planner_renderable(self) -> None:
        assets = {
            "table-01": PublishedAsset(
                asset_id="table-01",
                kind="table",
                markdown=(
                    "| A | Experiment Details . . . . | . 14 |\n"
                    "| --- | --- | --- |\n"
                    "|  | A.1 Benchmarks and Splits . . . . . . | . 14 |\n"
                    "| B | Prompt Templates . . . . | . 15 |"
                ),
                caption="table-04",
            )
        }
        candidates = (
            {
                "asset_id": "table-01",
                "kind": "table",
                "caption": "table-04",
                "renderable": True,
                "order": 0,
            },
        )

        report = AssetQualityGate().evaluate(assets, candidates)

        assessment = report.assessments[0]
        self.assertEqual(assessment.status, "unavailable")
        self.assertIn("table_of_contents", assessment.reasons)
        self.assertFalse(report.planner_candidates[0]["renderable"])
        self.assertEqual(report.planner_candidates[0]["asset_quality"], "unavailable")

    def test_malformed_table_is_degraded_instead_of_failing_the_paper(self) -> None:
        assets = {
            "table-02": PublishedAsset(
                asset_id="table-02",
                kind="table",
                markdown="| Method | Score |\n| Baseline | 82.1 | Extra |",
                caption="Main results",
            )
        }
        candidates = (
            {"asset_id": "table-02", "kind": "table", "caption": "Main results", "renderable": True},
        )

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(report.assessments[0].status, "degraded")
        self.assertIn("malformed_markdown_table", report.assessments[0].reasons)
        self.assertFalse(report.planner_candidates[0]["renderable"])

    def test_collapsed_measurements_in_one_cell_degrade_results_table(self) -> None:
        assets = {
            "table-02": PublishedAsset(
                asset_id="table-02",
                kind="table",
                markdown=(
                    "| Method | A | B | C |\n"
                    "| --- | --- | --- | --- |\n"
                    "| Baseline | 42.8±2.7 | 67.0±1.1 | 45.6±1.4 |\n"
                    "| Proposed | 55.8±1.8 | 73.4±1.2 39.9±1.0 44.2±1.1 | 13.8±0.6 |"
                ),
                caption="Main results",
            )
        }
        candidates = (
            {"asset_id": "table-02", "kind": "table", "caption": "Main results", "renderable": True},
        )

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(report.assessments[0].status, "degraded")
        self.assertIn("collapsed_measurements", report.assessments[0].reasons)
        self.assertFalse(report.planner_candidates[0]["renderable"])

    def test_multiple_symbol_keys_collapsed_into_one_cell_degrade_notation_table(self) -> None:
        assets = {
            "table-01": PublishedAsset(
                asset_id="table-01",
                kind="table",
                markdown=(
                    "| Symbol | Meaning |\n"
                    "| --- | --- |\n"
                    "| $\\mathcal{X}, x_t$   $\\mathcal{M}_t$ | task stream and task governed memory bank |\n"
                    "| $m, \\mathcal{C}_t$   $\\tau_t, y_t, e_t$ | memory record trajectory and output |"
                ),
                caption="Table 1: Notation used in Section 3.",
            )
        }
        candidates = (
            {"asset_id": "table-01", "kind": "table", "caption": "Notation", "renderable": True},
        )

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(report.assessments[0].status, "degraded")
        self.assertIn("collapsed_key_items", report.assessments[0].reasons)

    def test_source_table_with_spanning_cells_degrades_when_markdown_flattens_header(self) -> None:
        assets = {
            "table-03": PublishedAsset(
                asset_id="table-03",
                kind="table",
                markdown=(
                    "| Variant | Terminal-Bench SWE-Bench WebArena Mind2Web |  |  |  |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    "| Full | 67.4/30.4 | 83.6/30.6 | 58.4/6.9 | 51.8/11.5 |"
                ),
                caption="Compact ablation across four benchmarks",
            )
        }
        candidates = (
            {
                "asset_id": "table-03",
                "kind": "table",
                "caption": "Compact ablation",
                "renderable": True,
                "_source_table_html": (
                    '<table><tr><td rowspan="2">Variant</td>'
                    '<td colspan="4">Benchmarks</td></tr><tr><td>A</td><td>B</td>'
                    '<td>C</td><td>D</td></tr><tr><td>Full</td><td>1</td><td>2</td>'
                    '<td>3</td><td>4</td></tr></table>'
                ),
            },
        )

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(report.assessments[0].status, "degraded")
        self.assertIn("spanning_cells_not_preserved", report.assessments[0].reasons)

    def test_hyphenated_meaning_continuing_after_key_changes_degrades_table(self) -> None:
        assets = {
            "table-01": PublishedAsset(
                "table-01",
                "table",
                markdown=(
                    "| Symbol | Meaning |\n"
                    "| --- | --- |\n"
                    "| $e_t$ | trajectory, final output, and execution sta- |\n"
                    "| $d_m$ | tus verifier descriptor attached to record |"
                ),
                caption="Notation",
            )
        }
        candidates = ({"asset_id": "table-01", "kind": "table", "renderable": True},)

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(report.assessments[0].status, "degraded")
        self.assertIn("shifted_cell_continuation", report.assessments[0].reasons)

    def test_blank_nonleading_header_cell_degrades_flattened_table(self) -> None:
        assets = {
            "table-15": PublishedAsset(
                "table-15",
                "table",
                markdown=(
                    "| Variant |  | Terminal-Bench SWE-Bench | WebArena | Mind2Web |\n"
                    "| --- | --- | --- | --- | --- |\n"
                    "| Full | 67.4/30.4 | 83.6/30.6 | 58.4/6.9 | 51.8/11.5 |"
                ),
            )
        }
        candidates = ({"asset_id": "table-15", "kind": "table", "renderable": True},)

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(report.assessments[0].status, "degraded")
        self.assertIn("blank_header_cell", report.assessments[0].reasons)

    def test_formula_quality_reflects_what_renderer_can_reliably_render(self) -> None:
        assets = {
            "formula-01": PublishedAsset("formula-01", "formula", markdown="$$R_t = \\sum_j w_j$$"),
            "formula-02": PublishedAsset("formula-02", "formula", markdown="$$R_t = {x$$"),
            "formula-03": PublishedAsset("formula-03", "formula", image_path="images/equation.jpg"),
        }
        candidates = tuple(
            {"asset_id": asset_id, "kind": "formula", "renderable": True, "order": index}
            for index, asset_id in enumerate(assets)
        )

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(
            [item.status for item in report.assessments],
            ["usable", "degraded", "unavailable"],
        )
        self.assertIn("unbalanced_latex", report.assessments[1].reasons)
        self.assertIn("image_only_formula_not_renderable", report.assessments[2].reasons)

    def test_mismatched_latex_environments_degrade_formula(self) -> None:
        assets = {
            "formula-01": PublishedAsset(
                "formula-01",
                "formula",
                markdown=r"$$\begin{array} x & y \\ z & w \end{aligned}$$",
            )
        }
        candidates = ({"asset_id": "formula-01", "kind": "formula", "renderable": True},)

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(report.assessments[0].status, "degraded")
        self.assertIn("mismatched_latex_environment", report.assessments[0].reasons)

    def test_figure_requires_a_verified_image_representation(self) -> None:
        assets = {
            "figure-01": PublishedAsset(
                "figure-01",
                "figure",
                image_path="C:/normalized/images/architecture.jpg",
                image_verified=True,
            ),
            "figure-02": PublishedAsset("figure-02", "figure", image_path="../secret.jpg"),
            "figure-03": PublishedAsset("figure-03", "figure"),
        }
        candidates = tuple(
            {"asset_id": asset_id, "kind": "figure", "renderable": True, "order": index}
            for index, asset_id in enumerate(assets)
        )

        report = AssetQualityGate().evaluate(assets, candidates)

        self.assertEqual(
            [item.status for item in report.assessments],
            ["usable", "unavailable", "unavailable"],
        )
        self.assertIn("unverified_image_representation", report.assessments[1].reasons)
        self.assertIn("missing_image_path", report.assessments[2].reasons)


class AssetQualityPipelineIntegrationTests(unittest.TestCase):
    def test_unavailable_table_never_reaches_writer_or_zero_anchor_gate(self) -> None:
        from research_pulse.pedagogical.contracts import BlindReaderResult, EvidenceGateResult
        from research_pulse.pedagogical.pipeline import PedagogicalService
        from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock

        class Backend:
            text_model = "fake"
            vision_model = None

            def __init__(self) -> None:
                self.writer_payload = None

            def call_json(self, operation, model, prompt, image=None):  # noqa: ANN001
                payload = json.loads(prompt)
                if operation == "pedagogical_paper_model":
                    return {"thesis": "t", "argument_chain": [], "experiments": []}
                if operation == "pedagogical_teaching_plan":
                    return {"paper_archetype": ["method"], "domain": [], "reader_goal": "理解方法"}
                if operation == "pedagogical_asset_briefs":
                    return {"formulas": [], "tables": []}
                if operation == "pedagogical_note_write":
                    self.writer_payload = payload
                    return {"markdown": "# 可靠的纯文本笔记\n\n正文。"}
                raise AssertionError(operation)

        class PassEvidence:
            def evaluate(self, note, paper_model):  # noqa: ANN001
                return EvidenceGateResult(passed=True)

        class PassBlind:
            def read(self, note):  # noqa: ANN001
                return BlindReaderResult(overall="pass")

        backend = Backend()
        paper = CanonicalPaperIR(
            "p",
            "Paper",
            blocks=(
                PaperIRBlock("intro", "paragraph", "Introduction", "正文", 1),
                PaperIRBlock(
                    "toc",
                    "table",
                    "Contents",
                    "| A | Experiments .... | 14 |\n| --- | --- | --- |\n| B | Prompts .... | 15 |",
                    2,
                    caption="Contents",
                ),
            ),
        )
        result = PedagogicalService(
            backend,
            evidence_gate=PassEvidence(),
            blind_reader=PassBlind(),
            max_repairs=0,
        ).run(paper)

        self.assertEqual(backend.writer_payload["asset_candidates"], [])
        self.assertFalse(result.zero_anchors)


if __name__ == "__main__":
    unittest.main()
