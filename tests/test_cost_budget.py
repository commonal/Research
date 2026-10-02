"""T16 成本预算测试：单次 run 的 LLM 调用次数上限（不联网）。

当前调用序列（无 vision）：S1(1) + S2(1) + S3(1) + S4(1, 有公式/表时)
+ S5(1) + S7(1) + S8(1) = 7 次；有界 targeted repair 最多再 +3
（S5+S7+S8）= 10 次。若两轮 Evidence judge 都返回 ``false + 空 issues``，
每轮各允许一次一致性重试，极端上限为 12 次。图视觉未接 → 0 vision 调用。
"""

from __future__ import annotations

import unittest

from research_pulse.pedagogical.pipeline import PedagogicalService
from research_pulse.production.reading import CanonicalPaperIR, PaperIRBlock


def _ir_with_assets(zero_anchor_first: bool = True) -> CanonicalPaperIR:
    blocks = (
        PaperIRBlock("p1", "paragraph", "Intro", "A broker audits agents.", 1),
        PaperIRBlock("fm1", "formula", "Method", "x = 1", 2, latex="x = 1"),
        PaperIRBlock("t1", "table", "Results", "| A | B |\n| 1 | 2 |", 3,
                     table_html="<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>"),
    )
    return CanonicalPaperIR("p", "Writer", blocks, source_url="https://example.com/writer")


class _CountingModel:
    """按 operation 返回响应的假模型，记录每次调用。"""

    def __init__(self, *, blind_first: str = "pass", first_anchored: bool = True) -> None:
        self.operations: list[str] = []
        self.text_model = "fake"
        self.vision_model = None
        self._blind_first = blind_first
        self._first_anchored = first_anchored
        self._blind_calls = 0

    def call_json(self, operation: str, model: str, prompt: str, image: str | None = None) -> dict:
        self.operations.append(operation)
        if operation == "pedagogical_paper_model":
            return {"thesis": "A broker audits agents."}
        if operation == "pedagogical_teaching_plan":
            return {"paper_archetype": ["method"], "domain": ["security"], "reader_goal": "g"}
        if operation == "pedagogical_asset_briefs":
            return {"formulas": [{"asset_id": "formula-01", "role": "r", "plain_explanation": "e"}],
                    "tables": [{"asset_id": "table-01", "role": "r", "plain_explanation": "e"}]}
        if operation == "pedagogical_note_write":
            # 首轮可按需零锚定（触发 zero-anchor repair）；repair 轮一律带锚定
            if '"repair"' in prompt:
                return {"markdown": "# 笔记\n\n公式见 {{asset:formula-01}} 与表 {{asset:table-01}}。"}
            if self._first_anchored:
                return {"markdown": "# 笔记\n\n公式见 {{asset:formula-01}} 与表 {{asset:table-01}}。"}
            return {"markdown": "# 笔记\n\n纯文字，无锚定。"}
        if operation == "evidence_gate":
            return {"passed": True, "issues": []}
        if operation == "blind_reader":
            self._blind_calls += 1
            if self._blind_calls == 1:
                return {"overall": self._blind_first,
                        "background": "clear", "prior_gap": "clear", "mechanism": "clear",
                        "formalism": "clear", "experiment": "clear", "visual": "clear", "boundary": "clear"}
            return {"overall": "pass",
                    "background": "clear", "prior_gap": "clear", "mechanism": "clear",
                    "formalism": "clear", "experiment": "clear", "visual": "clear", "boundary": "clear"}
        return {}


class CostBudgetTests(unittest.TestCase):
    def test_single_run_uses_exactly_seven_calls(self) -> None:
        model = _CountingModel(blind_first="pass", first_anchored=True)
        result = PedagogicalService(model).run(_ir_with_assets())
        ops = model.operations
        self.assertEqual(len(ops), 7, f"单轮应 7 次调用，实际 {ops}")
        self.assertEqual(ops.count("pedagogical_paper_model"), 1)      # S1
        self.assertEqual(ops.count("pedagogical_teaching_plan"), 1)    # S2
        self.assertEqual(ops.count("pedagogical_asset_plan"), 1)       # S3
        self.assertEqual(ops.count("pedagogical_asset_briefs"), 1)     # S4
        self.assertEqual(ops.count("pedagogical_note_write"), 1)       # S5
        self.assertEqual(ops.count("evidence_gate"), 1)                # S7
        self.assertEqual(ops.count("blind_reader"), 1)                 # S8
        self.assertFalse(any("vision" in op for op in ops), "无 vision 模型 → 0 图视觉调用")
        self.assertFalse(result.zero_anchors, "首轮带锚定 → 不触发零锚定 repair")

    def test_repair_round_is_bounded_to_ten_calls(self) -> None:
        # 首轮盲读 needs_targeted_revision + 零锚定 → repair 轮 +3（write+evidence+blind）
        model = _CountingModel(blind_first="needs_targeted_revision", first_anchored=False)
        result = PedagogicalService(model).run(_ir_with_assets())
        ops = model.operations
        self.assertLessEqual(len(ops), 10, f"repair 后最多 10 次，实际 {len(ops)}")
        self.assertEqual(ops.count("pedagogical_note_write"), 2)       # 初写 + repair 重写
        self.assertEqual(ops.count("evidence_gate"), 2)
        self.assertEqual(ops.count("blind_reader"), 2)
        self.assertTrue(result.repaired)
        self.assertFalse(result.zero_anchors, "repair 轮带锚定 → 零锚定解除")

    def test_vision_model_absent_means_zero_pixel_inspections(self) -> None:
        model = _CountingModel()
        PedagogicalService(model).run(_ir_with_assets())
        self.assertFalse(any("inspect" in op or "vision" in op for op in model.operations))


if __name__ == "__main__":
    unittest.main()
