"""P0 最小教学闭环端到端测试（T7）：S1→S2→S5→S6 用 fake 组件跑通，产出 RenderedNote。"""

from __future__ import annotations

import unittest

from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock
from research_pulse.pedagogical.contracts import Experiment, PaperModel, TeachingPlan, TeachingSection
from research_pulse.pedagogical.pipeline import pipeline_from_parts
from research_pulse.pedagogical.renderer import PublishedAsset


def _fake_ir() -> CanonicalPaperIR:
    blocks = (
        PaperIRBlock("p1", "paragraph", "Intro", "Agents may overreach beyond the task.", 1),
        PaperIRBlock("p2", "paragraph", "Method", "A broker audits before and after execution.", 2),
        PaperIRBlock("p3", "paragraph", "Results", "Safe success rises from 64% to 98%.", 3),
        PaperIRBlock("f1", "figure", "Method", "Figure 1: pipeline", 4),
    )
    return CanonicalPaperIR("paper-writer", "Writer", blocks, source_url="https://example.com/writer")


class _FakeWriterModel:
    def write_pedagogical_note(self, value: dict) -> str:
        return (
            "# 教学化精读\n\n"
            "作者用一个 broker 在动作前后做审计。\n\n"
            "![示意]({{asset:fig_1}})\n\n"
            "安全成功率从 64% 提升到 98%，但不替代沙箱。"
        )


class PedagogicalClosedLoopTests(unittest.TestCase):
    def test_closed_loop_produces_rendered_note(self) -> None:
        model = _FakeWriterModel()
        pipeline = pipeline_from_parts(
            paper_model_builder=lambda ir: PaperModel(
                thesis=ir.title,
                central_idea="audit before/after execution",
                experiments=(Experiment(question="does it help?", results=("64%->98%",)),),
                limitations=("does not replace sandboxing",),
            ),
            teaching_plan_builder=lambda pm: TeachingPlan(
                paper_archetype=("method",),
                domain=("security",),
                reader_goal="理解如何把安全判断变成可训练信号",
                sections=(TeachingSection("core", "核心机制", "讲清 broker 审计"),),
            ),
            writer_model=model,
            assets={"fig_1": PublishedAsset("fig_1", "figure", image_path="/tmp/f1.jpg", caption="图1：闭环")},
            asset_base_url="assets",
        )
        ir = _fake_ir()
        out = pipeline.run(ir)
        self.assertNotIn("{{asset:", out.markdown)
        self.assertIn("![图 1](assets/f1.jpg)", out.markdown)
        self.assertNotIn("![示意](", out.markdown)
        self.assertIn("64%", out.markdown)
        self.assertIn("不替代沙箱", out.markdown)
        self.assertEqual(out.render_results[0].status, "rendered")

    def test_closed_loop_publishes_note_asset(self) -> None:
        pipeline = pipeline_from_parts(
            paper_model_builder=lambda ir: PaperModel(thesis=ir.title),
            teaching_plan_builder=lambda pm: TeachingPlan(
                paper_archetype=("method",), domain=(), reader_goal="g"
            ),
            writer_model=_FakeWriterModel(),
            assets={"fig_1": PublishedAsset("fig_1", "figure", image_path="/tmp/f1.jpg", caption="c")},
        )
        out = pipeline.run(_fake_ir())
        self.assertIn("教学化精读", out.markdown)


if __name__ == "__main__":
    unittest.main()
