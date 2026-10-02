"""DeepSeek 适配器 + 确定性资产构建的契约测试（不联网）。

用 fake ``call_json`` 验证 S1/S2/S5 的响应→契约映射稳定，且资产构建对
latex/image_path/caption 的 'None' 字面量归一正确。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from research_pulse.pedagogical.deepseek_adapters import (
    DeepSeekPedagogicalModel,
    _unescape_latex,
    assets_from_blocks,
    pipe_table_from_text,
    table_from_html,
)
from research_pulse.pedagogical.contracts import PaperModel, TeachingPlan, validate_contracts
from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock


def _block(kind: str, text: str = "", *, latex=None, image_path=None, caption=None, section="body", bid="b1") -> PaperIRBlock:
    if not text:
        text = {"formula": "E = mc^2", "figure": "Figure caption", "table": "| A | B |"}.get(kind, "some text")
    return PaperIRBlock(
        block_id=bid, kind=kind, section=section, text=text, order=0, caption=caption,
        latex=latex, table_html=None, image_path=image_path, safe_image=bool(image_path),
        parse_status="available", facets=(),
    )


class FakeCallJson:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.text_model = "fake-text"
        self.vision_model = None

    def call_json(self, operation: str, model: str, prompt: str, image: str | None = None) -> dict:
        self.calls.append(operation)
        return self.responses.pop(0)


class PipeTableTests(unittest.TestCase):
    def test_converts_pipe_text_to_markdown_table(self):
        text = "| Model | Params | Acc |\n| A | 7B | 0.91 |\n| B | 70B | 0.95 |"
        md = pipe_table_from_text(text)
        self.assertIn("| Model | Params | Acc |", md)
        self.assertIn("| --- | --- | --- |", md)
        self.assertIn("| A | 7B | 0.91 |", md)
        self.assertIn("| B | 70B | 0.95 |", md)

    def test_non_table_returns_none(self):
        self.assertIsNone(pipe_table_from_text("just a paragraph, no pipes"))


class AssetsFromBlocksTests(unittest.TestCase):
    def test_kind_mapping(self):
        blocks = [
            _block("table", caption="None", bid="t1",
                   text="| H1 | H2 |\n| a | b |"),
            _block("formula", latex="x^2", bid="f1"),
            _block("formula", image_path="images/eq.jpg", latex="None", caption="None", bid="f2"),
            _block("formula", latex="None", image_path="None", text="E=mc^2", bid="f3"),
            _block("figure", image_path="images/fig.jpg", caption="Fig", bid="g1"),
        ]
        assets, cands = assets_from_blocks(blocks)
        # table
        self.assertIn("table-01", assets)
        self.assertIn("| H1 | H2 |", assets["table-01"].markdown)
        # formula latex -> $$..$$
        self.assertEqual(assets["formula-01"].markdown, "$$x^2$$")
        # formula image -> image asset
        self.assertEqual(assets["formula-02"].image_path, "images/eq.jpg")
        # formula neither -> not renderable, no asset
        self.assertNotIn("formula-03", assets)
        r = [c for c in cands if c["asset_id"] == "formula-03"][0]
        self.assertFalse(r["renderable"])
        # figure
        self.assertEqual(assets["figure-01"].image_path, "images/fig.jpg")
        # literal 'None' caption normalized
        self.assertEqual(assets["figure-01"].caption, "Fig")
        self.assertEqual(assets["table-01"].caption, "")

    def test_caption_strips_mineru_html_tags(self):
        # MinerU 把 caption 里的上下标转成 <sub>/<sup> → 必须剥掉（否则图注/候选里带标签符号）
        blocks = [
            _block("figure", image_path="images/fig.jpg",
                   caption="Fig 3: th<sub>e u</sub>bi<sub>qu</sub>it<sub>ous</sub> MLP block", bid="g1"),
        ]
        assets, cands = assets_from_blocks(blocks)
        self.assertNotIn("<sub>", assets["figure-01"].caption)
        self.assertEqual(assets["figure-01"].caption, "Fig 3: the ubiquitous MLP block")
        self.assertNotIn("<sub>", cands[0]["caption"])


class AdapterContractsTests(unittest.TestCase):
    def _paper(self) -> CanonicalPaperIR:
        blocks = [_block("paragraph", "DeepSeek-V3 is a Mixture-of-Experts model.", bid="p1"),
                  _block("table", "| A | B |\n| 1 | 2 |", caption="Table 1", bid="t1")]
        return CanonicalPaperIR(source_id="fake-source", title="DeepSeek-V3", blocks=tuple(blocks), abstract="Abstract.")

    def test_s1_s2_s5_round_trip_with_fake_model(self):
        responses = [
            {"thesis": "DeepSeek-V3 用 MoE 架构", "central_problem": "降低训练成本", "prior_gap": "",
             "central_idea": "MoE + MLA", "argument_chain": ["用 MoE 稀疏化"],
             "experiments": [{"experiment_id": "e1", "question": "训练成本?", "setup": ["8卡"],
                             "comparison": ["vs V2"], "results": ["0.9x"],
                             "interpretation": "更低", "boundary": "限集群"}],
             "must_preserve_facts": ["671B"], "limitations": ["依赖集群"], "material_unknowns": []},
            {"paper_archetype": ["method"], "domain": ["LLM"], "reader_goal": "判断是否采用",
             "prerequisites": ["MoE"], "running_example": "用一个小工厂理解 MoE",
             "sections": [{"section_id": "s1", "title": "是什么", "teaching_goal": "建立整体图景",
                           "evidence_targets": ["e1"]}]},
            {"markdown": "## 是什么\nDeepSeek-V3 用 MoE。见表 {{asset:table-01}}。"},
        ]
        fake = FakeCallJson(responses)
        ped = DeepSeekPedagogicalModel(fake, assets={}, candidates=[])  # assets unused here, writer anchored from prompt anyway
        paper = self._paper()
        pm = ped.build_paper_model(paper)
        self.assertIsInstance(pm, PaperModel)
        self.assertEqual(pm.thesis, "DeepSeek-V3 用 MoE 架构")
        self.assertEqual(pm.experiments[0].experiment_id, "e1")
        tp = ped.build_teaching_plan(pm)
        self.assertIsInstance(tp, TeachingPlan)
        self.assertEqual(tp.paper_archetype, ("method",))
        validate_contracts(pm, tp)  # should not raise
        md = ped.write_pedagogical_note({"paper_model": {"thesis": pm.thesis}, "teaching_plan": {"reader_goal": tp.reader_goal}})
        self.assertIsInstance(md, str)
        self.assertIn("MoE", md)
        self.assertEqual(fake.calls, ["pedagogical_paper_model", "pedagogical_teaching_plan", "pedagogical_note_write"])


class HtmlTableTests(unittest.TestCase):
    def test_table_from_html_builds_pipe_table(self):
        html = ("<table><tr><th>Metric</th><th>Base</th><th>Seed 1</th></tr>"
                "<tr><td>Safe success</td><td>64.36%</td><td>98.48%</td></tr></table>")
        md = table_from_html(html)
        self.assertIn("| Metric | Base | Seed 1 |", md)
        self.assertIn("| --- | --- | --- |", md)
        self.assertIn("| Safe success | 64.36% | 98.48% |", md)
        self.assertIsNone(table_from_html("not a table"))

    def test_table_from_html_handles_th_and_entities(self):
        html = ("<table><tr><th>A &amp; B</th></tr><tr><td>x &lt; y</td></tr></table>")
        md = table_from_html(html)
        self.assertIn("| A & B |", md)
        self.assertIn("| x < y |", md)

    def test_table_from_html_escapes_literal_pipes_inside_a_cell(self):
        html = (
            r"<table><tr><th>Model</th><th>WA</th></tr>"
            r"<tr><td>Qwen</td><td>\(|0.008|\)</td></tr></table>"
        )

        md = table_from_html(html)

        self.assertIn(r"| Qwen | \(\|0.008\|\) |", md)


class LatexUnescapeTests(unittest.TestCase):
    def test_double_backslash_becomes_single(self):
        # 模拟 JSON 双重转义：\\begin{array} -> \begin{array}
        raw = chr(92) * 2 + "begin{array} " + chr(92) * 2 + "mathbf"
        self.assertEqual(_unescape_latex(raw), chr(92) + "begin{array} " + chr(92) + "mathbf")

    def test_single_backslash_untouched(self):
        raw = chr(92) + "tag{1}"
        self.assertEqual(_unescape_latex(raw), chr(92) + "tag{1}")

    def test_empty_and_none_handled(self):
        self.assertEqual(_unescape_latex(""), "")
        self.assertEqual(_unescape_latex(chr(92) * 2), chr(92))


if __name__ == "__main__":
    unittest.main()
