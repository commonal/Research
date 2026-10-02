"""零锚定门禁测试：有资产候选但正文零锚定 → 不发布（PedagogicalService 层 + 生产层）。

背景：2312 实测 v4-flash 偶发不写 asset anchor（正文纯文字、盲读仍全 clear），
零锚定被放行发布。本测试锁定：候选资产存在但 rendered=0 → zero_anchors=True；
repair 后恢复锚定 → 放行；生产链路 zero_anchors → 回退 generic。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_pulse.pedagogical.contracts import (
    BlindReaderResult,
    EvidenceGateResult,
    PedagogicalResult,
    RenderedNote,
    RenderResult,
)
from research_pulse.pedagogical.pipeline import PedagogicalService
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import CanonicalPaperIR, DeepSeekPaperReadingModel, PaperIRBlock
from research_pulse.reader_production import ReaderConfig, ReaderProduction, ReaderProductionService
from unittest import mock


def _ir_with_assets() -> CanonicalPaperIR:
    """含 formula + table 可渲染块（不依赖本地图片文件）。"""
    blocks = (
        PaperIRBlock("p1", "paragraph", "Intro", "A broker audits agents.", 1),
        PaperIRBlock("fm1", "formula", "Method", "x = 1", 2, latex=r"x=1"),
        PaperIRBlock("tb1", "table", "Results", "a | b", 3,
                     table_html=("<table><tr><td>a</td><td>b</td></tr>"
                                 "<tr><td>1</td><td>2</td></tr></table>")),
    )
    return CanonicalPaperIR("p", "t", blocks, source_url="https://example.com/t")


class _FakeReadingModel:
    """按 operation 返回响应的假模型：S1/S2 固定，S5 可编程（首轮/repair 轮），门禁恒过。"""

    def __init__(self, first_note: str, repair_note: str | None = None) -> None:
        self.first_note = first_note
        self.repair_note = repair_note
        self.text_model = "fake"
        self.vision_model = None
        self.repair_writes = 0

    def call_json(self, operation: str, model: str, prompt: str, image: str | None = None) -> dict:
        if operation == "pedagogical_paper_model":
            return {"thesis": "A broker audits agents before and after execution."}
        if operation == "pedagogical_teaching_plan":
            return {"paper_archetype": ["method"], "domain": ["security"], "reader_goal": "理解审计机制"}
        if operation == "pedagogical_note_write":
            if '"repair"' in prompt:
                self.repair_writes += 1
                return {"markdown": (self.repair_note or self.first_note)}
            return {"markdown": self.first_note}
        if operation == "evidence_gate":
            return {"passed": True, "issues": []}
        if operation == "blind_reader":
            return {"overall": "pass", "background": "clear", "prior_gap": "clear", "mechanism": "clear",
                    "formalism": "clear", "experiment": "clear", "visual": "clear", "boundary": "clear"}
        return {}


class ZeroAnchorGateTests(unittest.TestCase):
    def _run(self, first_note: str, repair_note: str | None = None) -> tuple[PedagogicalResult, _FakeReadingModel]:
        model = _FakeReadingModel(first_note, repair_note)
        service = PedagogicalService(model)
        result = service.run(_ir_with_assets())
        return result, model

    def test_zero_anchor_blocks_release_even_when_gates_pass(self) -> None:
        # 纯文字、无任何锚定：证据+盲读都过 → 仍 zero_anchors=True（不发布）
        result, _ = self._run("纯文字笔记：讲机制、讲实验，但一个资产都不引用。")
        self.assertTrue(result.zero_anchors)
        self.assertTrue(result.evidence.passed)
        self.assertEqual(result.blind.overall, "pass")
        self.assertTrue(result.repaired, "零锚定应触发 repair")

    def test_repair_recovers_anchor_then_releases(self) -> None:
        # repair 轮补上锚定 → 放行（zero_anchors=False, repaired=True）
        result, model = self._run(
            "纯文字笔记：讲机制、讲实验，但一个资产都不引用。",
            repair_note="# 教学化笔记\n\n机制如上。\n\n{{asset:formula-01}}\n\n{{asset:table-01}}\n",
        )
        self.assertFalse(result.zero_anchors)
        self.assertTrue(result.repaired)
        self.assertEqual(model.repair_writes, 1)
        self.assertTrue(any(r.status == "rendered" for r in result.note.render_results))

    def test_partial_asset_usage_triggers_repair_until_all_selected_assets_are_anchored(self) -> None:
        result, model = self._run(
            "# 笔记\n\n只解释公式。\n\n{{asset:formula-01}}",
            repair_note=(
                "# 笔记\n\n解释公式。\n\n{{asset:formula-01}}\n\n"
                "再用表格对照结果。\n\n{{asset:table-01}}"
            ),
        )

        self.assertTrue(result.repaired)
        self.assertEqual(1, model.repair_writes)
        self.assertEqual((), result.asset_usage.unreferenced_selected_ids)

    def test_no_candidates_means_not_zero_anchor(self) -> None:
        # 论文本来就没有可渲染资产 → 不算零锚定（纯理论短文合法）
        blocks = (PaperIRBlock("p1", "paragraph", "Intro", "text only", 1),)
        paper = CanonicalPaperIR("p", "t", blocks, source_url="u")
        model = _FakeReadingModel("纯文字笔记,但论文本来就没有图/表/公式。")
        result = PedagogicalService(model).run(paper)
        self.assertFalse(result.zero_anchors)
        self.assertFalse(result.repaired)

    def test_asset_usage_ledger_distinguishes_selected_anchored_and_rendered(self) -> None:
        result, _ = self._run("# 笔记\n\n{{asset:formula-01}}")

        self.assertEqual(set(result.asset_usage.selected_ids), {"formula-01", "table-01"})
        self.assertEqual(result.asset_usage.anchored_ids, ("formula-01",))
        self.assertEqual(result.asset_usage.rendered_ids, ("formula-01",))
        self.assertEqual(result.asset_usage.unreferenced_selected_ids, ("table-01",))
        self.assertEqual(result.asset_usage.failed_render_ids, ())

    def test_service_uses_semantic_asset_plan_after_building_paper_model(self) -> None:
        class _SemanticModel(_FakeReadingModel):
            def call_json(self, operation, model, prompt, image=None):
                if operation == "pedagogical_asset_plan":
                    self.assert_semantic_context = (
                        '"thesis": "A broker audits agents before and after execution."' in prompt
                        and '"reader_goal": "理解审计机制"' in prompt
                    )
                    return {
                        "choices": [{
                            "asset_id": "formula-01",
                            "decision": "inline",
                            "rationale": "解释核心审计机制",
                        }]
                    }
                return super().call_json(operation, model, prompt, image)

        model = _SemanticModel("# 笔记\n\n用公式解释审计机制。\n\n{{asset:formula-01}}")
        result = PedagogicalService(model).run(_ir_with_assets())

        self.assertTrue(model.assert_semantic_context)
        self.assertEqual(result.asset_usage.selected_ids, ("formula-01",))
        self.assertFalse(result.repaired)


class ZeroAnchorProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "knowledge"
        self._from_env = mock.patch.object(
            DeepSeekPaperReadingModel, "from_environment", return_value=object()
        )
        self._from_env.start()

    def tearDown(self) -> None:
        self._from_env.stop()
        self._tmp.cleanup()

    def test_zero_anchor_falls_back_to_generic_with_reason(self) -> None:
        class _ZeroAnchorPipeline:
            def run(self, paper):
                return PedagogicalResult(
                    note=RenderedNote(markdown="纯文字"),
                    evidence=EvidenceGateResult(passed=True),
                    blind=BlindReaderResult(overall="pass"),
                    zero_anchors=True,
                )

        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
            pedagogical=_ZeroAnchorPipeline(),
        )
        service.reader.resolver.resolve = lambda candidate: _ir_with_assets()  # type: ignore[method-assign]
        from types import SimpleNamespace

        service.reader.read = lambda candidate: SimpleNamespace(  # type: ignore[method-assign]
            draft=SimpleNamespace(markdown="# generic 兜底\n\n这是通过最终发布门禁的完整正文。"),
            receipt=SimpleNamespace(status="completed", stop_reason="completed"),
        )
        result = service.process(PaperCandidate("p", "t", "u", "security"))
        self.assertEqual(result["publication_status"], "published")
        self.assertIn("gates:zero_anchors", result["pedagogical_fallbacks"])
        self.assertIn("generic 兜底", Path(result["published_path"]).read_text(encoding="utf-8"))

    def test_published_title_strips_html_tags(self) -> None:
        # MinerU 标题残留 <sub>/<sup> → 发布 frontmatter 必须剥掉
        service = ReaderProductionService(
            ReaderConfig(normalized_root=self.root, mode="pedagogical"),
            self.vault,
        )
        candidate = PaperCandidate(
            "p", "Mamba: Linear-Time Se<sub>qu</sub>ence Modelin<sub>g</sub>", "u", "security"
        )
        from types import SimpleNamespace

        markdown = ReaderProduction.note_asset_markdown(
            "# body", candidate, SimpleNamespace(status="completed", completed_at="2026-01-01T00:00:00+00:00")
        )
        self.assertIn('title: "Mamba: Linear-Time Sequence Modeling"', markdown)
        self.assertNotIn("<sub>", markdown)


if __name__ == "__main__":
    unittest.main()
