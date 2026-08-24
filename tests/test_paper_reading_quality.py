from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class GoldenPaperReadingFixtureTests(TestCase):
    def test_2608_golden_fixture_is_narrative_first_and_evidence_aware(self) -> None:
        rubric = json.loads(
            (ROOT / "evals" / "real-e2e" / "2608.18351v1-golden-rubric.json").read_text(encoding="utf-8")
        )
        answer = (ROOT / "evals" / "real-e2e" / rubric["golden_answer"]).read_text(encoding="utf-8")

        for heading in rubric["required_sections"]:
            self.assertIn(heading, answer)
        for concept in rubric["required_concepts"]:
            self.assertIn(concept, answer)
        for anchor in rubric["required_source_anchors"]:
            self.assertIn(anchor, answer)

    def test_current_published_note_is_not_used_as_the_golden_fixture(self) -> None:
        current_path = max((ROOT / "knowledge" / "papers" / "2608.18351v1").glob("*.md"), key=lambda path: path.stat().st_mtime)
        current = current_path.read_text(encoding="utf-8")
        golden = (
            ROOT / "evals" / "real-e2e" / "2608.18351v1-golden-reading.md"
        ).read_text(encoding="utf-8")

        self.assertNotEqual(current, golden)
        for heading in (
            "一句话先说清楚",
            "背景：作者发现了什么问题",
            "论文真正要解决的问题",
            "核心想法：",
            "方法如何落地",
            "实验如何验证这个想法",
            "应该怎样解读，而不是过度宣传",
        ):
            self.assertIn(heading, current)
        for concept in ("Qwen3.5-4B", "安全成功", "64.36%", "98.48%", "4.56%", "0.79%"):
            self.assertIn(concept, current)
        self.assertNotIn("文中未见基线、指标或数值结果", current)
        self.assertIn("64.36%", golden)
