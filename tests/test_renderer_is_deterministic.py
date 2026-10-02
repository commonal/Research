"""确定性 Renderer 测试（T6）：不调 LLM、不猜语义、缺失即记录、逐次幂等。"""

from __future__ import annotations

import unittest

from research_pulse.pedagogical.contracts import AssetAnchor, NoteDraftWithAnchors
from research_pulse.pedagogical.renderer import DeterministicRenderer, PublishedAsset


class DeterministicRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assets = {
            "fig_1": PublishedAsset("fig_1", "figure", image_path="/tmp/fig1.jpg", caption="图1：闭环"),
            "tbl_1": PublishedAsset("tbl_1", "table", markdown="| A | B |\n|---|---|\n| 1 | 2 |"),
        }

    def test_replaces_anchors_and_records(self) -> None:
        renderer = DeterministicRenderer(self.assets)
        draft = NoteDraftWithAnchors(
            markdown="开头…{{asset:fig_1}}…中间…{{asset:tbl_1}}",
            anchors=(AssetAnchor("a1", "fig_1", "sec", "after_paragraph"), AssetAnchor("a2", "tbl_1", "sec")),
        )
        out = renderer.render(draft)
        self.assertIn("![图 1](assets/fig1.jpg)", out.markdown)
        self.assertIn("| A | B |", out.markdown)
        statuses = {r.anchor_id: r.status for r in out.render_results}
        self.assertEqual(statuses, {"fig_1": "rendered", "tbl_1": "rendered"})
        self.assertNotIn("{{asset:", out.markdown)

    def test_asset_renders_as_standalone_block(self) -> None:
        # 资产（尤其表/公式/图）必须被空行包成独立块，否则内嵌在句子里 GFM 不认作表格/公式块。
        renderer = DeterministicRenderer(self.assets)
        draft = NoteDraftWithAnchors("如表{{asset:tbl_1}}所示。", (AssetAnchor("a2", "tbl_1", "sec"),))
        out = renderer.render(draft)
        self.assertIn("\n\n| A | B |\n", out.markdown)
        self.assertIn("\n|---|---|\n", out.markdown)

    def test_missing_asset_gives_missing_status(self) -> None:
        renderer = DeterministicRenderer(self.assets)
        draft = NoteDraftWithAnchors("{{asset:nope}}", (AssetAnchor("a", "nope", "sec"),))
        out = renderer.render(draft)
        self.assertEqual(out.render_results[0].status, "missing")

    def test_unavailable_image_gives_unavailable(self) -> None:
        renderer = DeterministicRenderer({"fig_x": PublishedAsset("fig_x", "figure", image_path=None)})
        out = renderer.render(NoteDraftWithAnchors("{{asset:fig_x}}", (AssetAnchor("a", "fig_x", "sec"),)))
        self.assertEqual(out.render_results[0].status, "unavailable")

    def test_is_deterministic(self) -> None:
        renderer = DeterministicRenderer(self.assets)
        draft = NoteDraftWithAnchors("{{asset:fig_1}}", (AssetAnchor("a", "fig_1", "sec"),))
        self.assertEqual(renderer.render(draft), renderer.render(draft))

    def test_renderer_takes_no_model(self) -> None:
        # 契约：Renderer 不依赖 LLM —— 构造签名里根本没有 model 参数。
        import inspect

        sig = inspect.signature(DeterministicRenderer.__init__)
        self.assertNotIn("model", sig.parameters)
        self.assertNotIn("llm", sig.parameters)

    def test_image_does_not_repeat_full_english_caption_in_chinese_note(self) -> None:
        caption = "Figure 1: Overview of the complete research pipeline."
        renderer = DeterministicRenderer({
            "fig_1": PublishedAsset("fig_1", "figure", image_path="/tmp/fig1.jpg", caption=caption),
        })

        out = renderer.render(NoteDraftWithAnchors("如图所示：{{asset:fig_1}}"))

        self.assertNotIn(caption, out.markdown)
        self.assertIn("![图 1](assets/fig1.jpg)", out.markdown)

    def test_wrapped_anchor_can_render_latex_without_regex_replacement_error(self) -> None:
        renderer = DeterministicRenderer({
            "tbl_latex": PublishedAsset(
                "tbl_latex",
                "table",
                markdown="| metric | value |\n|---|---|\n| score | $1 \\pm 0.1$ |",
            ),
        })

        out = renderer.render(NoteDraftWithAnchors(
            "![模型生成的包装]({{asset:tbl_latex}})",
            (AssetAnchor("a", "tbl_latex", "results"),),
        ))

        self.assertIn(r"$1 \pm 0.1$", out.markdown)
        self.assertNotIn("![模型生成的包装]", out.markdown)


if __name__ == "__main__":
    unittest.main()
