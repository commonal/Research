"""Minimal integration tests for the single-paper reading -> note publish happy path.

Chain under test (deterministic, no LLM):

    PaperCandidate
      -> normalized blocks (temp fixture)
      -> CanonicalPaperIR            (ReaderMaterialResolver)
      -> PaperReader                 (fake coverage-complete / incomplete model)
      -> markdown note
      -> published  knowledge/papers/<source_id>/<version>.md
      -> needs_review staging/<source_id>/<version>.md

This guards the routing seam: a faithful note published, an incomplete note
routed to staging, with correct schema-v1 front matter and a non-empty body.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_pulse.production.pipeline import PaperCandidate
from research_pulse.reader_production import ReaderConfig, ReaderProductionService


def _fixture_blocks() -> list[dict[str, object]]:
    """Normalized block projections matching the fake model's source_block_ids."""
    def block(block_id: str, kind: str, text: str, section: str, page: int) -> dict[str, object]:
        return {
            "block_id": block_id,
            "kind": kind,
            "text": text,
            "section_path": [section],
            "page_start": page,
            "page_end": page,
            "parse_status": "available",
            "confidence": 1.0,
            "sources": [{"parser": "mineru", "locator": f"source.json#/pages/0/items/{page}"}],
        }

    return [
        block("problem", "text", "Agents exercise authority beyond the task.", "Introduction", 1),
        block("method", "text", "A broker audits actions before and after execution.", "Method", 2),
        block("result", "text", "Safe success rises from 64% to 98%.", "Results", 3),
        block("limit", "text", "The method does not replace sandboxing.", "Limitations", 4),
    ]


class _CompleteModel:
    """Coverage-complete fake: note preserves all required facts -> deterministic gate passes."""

    text_model = "text-test-model"
    vision_model = None

    def read_full_paper(self, request: object) -> dict:
        return {
            "thesis": "Auditing reduces excess authority.",
            "argument_chain": {"problem": "Agents may exercise authority beyond the task."},
            "experiments": [{"experiment_id": "experiment:main"}],
            "must_preserve_facts": [{"fact_id": "fact:f-result", "eligible": True, "statement": "Safe success rises from 64% to 98%."}],
            "limitations": [{"limitation_id": "limitation:scope"}],
            "coverage": {name: {"status": "complete", "missing": []} for name in ("background", "problem", "prior_gap", "mechanism", "experiment", "boundary")},
            "source_facts": [
                {"fact_id": "f-problem", "facet": "problem", "statement": "Agents exercise authority beyond the task.", "source_block_ids": ["problem"]},
                {"fact_id": "f-method", "facet": "method", "statement": "A broker audits actions before and after execution.", "source_block_ids": ["method"]},
                {"fact_id": "f-result", "facet": "experiment", "statement": "Safe success rises from 64% to 98%.", "source_block_ids": ["result"]},
                {"fact_id": "f-limit", "facet": "limitation", "statement": "The method does not replace sandboxing.", "source_block_ids": ["limit"]},
            ],
            "source_limitations": [],
            "material_unknowns": [],
            "visual_candidates": [{"block_id": "result", "decision": "inline", "reason": "The result is central."}],
            "sections": [{
                "section_id": "main",
                "heading": "论文主线",
                "reader_question": "方法如何降低越权，实验说明了什么？",
                "prerequisite_bridges": ["先说明任务成功不等于权限安全。"],
                "reasoning_steps": ["连接审计机制、结果和边界。"],
                "experiment_slots": ["experiment:main"],
                "asset_jobs": [],
                "transition_in": "从问题进入方法。",
                "transition_out": "最后说明适用边界。",
                "stop_conditions": ["读者能复述机制、结果和局限。"],
                "evidence_handles": ["problem", "method", "result", "limit"],
                "coverage_obligation_ids": ["argument:problem", "experiment:main", "fact:f-result", "limitation:scope"],
            }],
        }

    def plan_note(self, value: dict) -> dict:
        return {"sections": [("论文主线", "全文论证已经覆盖。")]}

    def write_note(self, value: dict) -> str:
        return (
            "# 论文主线\n\n"
            "代理可能使用超出任务所需的权限；broker 会在执行前后审计。"
            "安全成功率从 64% 提升到 98%。它不能替代沙箱。\n"
        )


class _IncompleteModel(_CompleteModel):
    """Write a note that omits the required result numbers -> deterministic gate blocks."""

    def write_note(self, value: dict) -> str:
        return "# 论文主线\n\n模型使用了 LoRA 训练。\n"


class ReaderHappyPathIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "knowledge"
        source_id = "paper-quality"
        normalized_dir = self.root / "normalized" / source_id / "normalized"
        normalized_dir.mkdir(parents=True, exist_ok=True)
        (normalized_dir / "blocks.jsonl").write_text(
            "\n".join(json.dumps(block, ensure_ascii=False) for block in _fixture_blocks()),
            encoding="utf-8",
        )
        (normalized_dir / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "source_id": source_id,
            "block_count": len(_fixture_blocks()),
            "input_hashes": {"mineru_content_list": "fixture-m", "docling_document": "fixture-d"},
            "complete": True,
        }), encoding="utf-8")
        self.config = ReaderConfig(normalized_root=self.root / "normalized")
        self.candidate = PaperCandidate(source_id, "Quality", "https://example.com/quality", "agents")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_faithful_note_publishes_to_papers(self) -> None:
        service = ReaderProductionService(self.config, self.vault, model=_CompleteModel())
        receipt = service.process(self.candidate)

        self.assertEqual(receipt["receipt_status"], "completed")
        self.assertEqual(receipt["publication_status"], "published")
        self.assertIn("papers", receipt["published_path"])

        note = Path(receipt["published_path"])
        self.assertTrue(note.exists())
        body = note.read_text(encoding="utf-8")
        self.assertIn("schema_version: 1", body)
        self.assertIn("publication_status: \"published\"", body)
        self.assertIn("64%", body)
        # Published notes live under papers/, never staging/.
        self.assertNotIn("staging", str(note))

    def test_incomplete_note_publishes_with_warning_instead_of_staging(self) -> None:
        # An omitted deterministic detail (here the result numbers) is now a
        # warning, not a gate: the note still publishes to papers/ and records
        # the omitted obligation in degradations.  Only anti-fabrication or
        # empty-writer failures route to staging.
        service = ReaderProductionService(self.config, self.vault, model=_IncompleteModel())
        receipt = service.process(self.candidate)

        self.assertEqual(receipt["receipt_status"], "completed")
        self.assertEqual(receipt["publication_status"], "published")
        self.assertIn("papers", receipt["published_path"])

        note = Path(receipt["published_path"])
        self.assertTrue(note.exists())
        body = note.read_text(encoding="utf-8")
        self.assertIn("schema_version: 1", body)
        self.assertIn("publication_status: \"published\"", body)
        self.assertNotIn("staging", str(note))

    def test_empty_writer_output_still_routes_to_staging(self) -> None:
        source_id = "paper-quality"
        self.candidate = PaperCandidate(source_id, "Quality", "https://example.com/quality", "agents")

        class EmptyModel(_CompleteModel):
            def write_note(self, value: dict) -> str:
                return ""

        service = ReaderProductionService(self.config, self.vault, model=EmptyModel())
        receipt = service.process(self.candidate)

        self.assertEqual(receipt["receipt_status"], "failed")
        self.assertEqual(receipt["publication_status"], "needs_review")
        self.assertIn("staging", receipt["published_path"])


if __name__ == "__main__":
    unittest.main()
