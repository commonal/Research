from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
import json
from datetime import UTC, datetime

from research_pulse.production.adapters import NormalizedSourceParser
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.normalized import NormalizedBlock, SourceRef


class NormalizedReadingInputTests(TestCase):
    def _temporary_directory(self) -> TemporaryDirectory:
        return TemporaryDirectory(dir=Path(__file__).resolve().parents[1])

    def _candidate(self) -> PaperCandidate:
        return PaperCandidate(
            source_id="2608.18351v1",
            title="A real paper",
            source_url="https://arxiv.org/abs/2608.18351v1",
            domain="security",
            published_at=datetime(2026, 8, 18, tzinfo=UTC),
        )

    def test_parser_reads_cache_and_preserves_multimodal_quality_boundaries(self) -> None:
        with self._temporary_directory() as directory:
            root = Path(directory) / "experiments" / "2608.18351v1" / "normalized"
            root.mkdir(parents=True)
            blocks = [
                NormalizedBlock(
                    "normalized:2608.18351v1:text:1", "text", "The method addresses excess-authority errors.",
                    section_path=("Introduction",), page_start=1, page_end=1, bbox=(1, 1, 10, 10),
                    sources=(SourceRef("mineru", "mineru:content#/0"), SourceRef("docling", "docling:document#/texts/1")),
                    alignment="aligned", confidence=1.0,
                ),
                NormalizedBlock(
                    "normalized:2608.18351v1:table:1", "table", "| Metric | Base | Seed 1 |\n| --- | --- | --- |\n| Safe success | 64.36% | 98.48% |",
                    section_path=("Experiments",), page_start=5, page_end=5, bbox=(1, 1, 10, 10), caption="TABLE II",
                    table_html="<table>...</table>", table_rows=2, table_columns=3,
                    sources=(SourceRef("mineru", "mineru:content#/1"), SourceRef("docling", "docling:document#/tables/1")),
                    alignment="aligned", confidence=0.9,
                ),
                NormalizedBlock(
                    "normalized:2608.18351v1:formula:1", "formula", "\\mathbf{z}(a_t)=...",
                    section_path=("Method",), page_start=2, page_end=2, bbox=(1, 1, 10, 10), latex="\\mathbf{z}(a_t)=...",
                    sources=(SourceRef("mineru", "mineru:content#/2"), SourceRef("docling", "docling:document#/texts/2")),
                    alignment="aligned", confidence=0.9,
                ),
            ]
            (root / "blocks.jsonl").write_text("".join(json.dumps(block.to_dict(), ensure_ascii=False) + "\n" for block in blocks), encoding="utf-8")

            material = NormalizedSourceParser(Path(directory) / "experiments").parse(self._candidate())

            self.assertEqual(material.evidence_level, "full_text_multimodal")
            self.assertEqual(set(material.source_fragments), {blocks[0].block_id, blocks[1].block_id})
            self.assertEqual(material.evidence_blocks[blocks[1].block_id].supported_facets, ("experiment",))
            self.assertFalse(material.evidence_blocks[blocks[2].block_id].eligible_for_fact)
            self.assertEqual(material.evidence_blocks[blocks[2].block_id].rejection_reason, "formula_not_auto_interpreted")
            self.assertEqual(material.evidence_candidates[blocks[1].block_id].parser, "normalized:mineru+docling")

    def test_parser_rejects_blocks_from_another_source(self) -> None:
        with self._temporary_directory() as directory:
            root = Path(directory) / "experiments" / "2608.18351v1" / "normalized"
            root.mkdir(parents=True)
            (root / "blocks.jsonl").write_text(json.dumps({
                "block_id": "normalized:other-paper:text:1", "kind": "text", "text": "wrong source",
                "sources": [{"parser": "mineru", "locator": "x"}], "alignment": "mineru_only",
                "parse_status": "available", "confidence": 0.5,
            }) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_id"):
                NormalizedSourceParser(Path(directory) / "experiments").parse(self._candidate())
