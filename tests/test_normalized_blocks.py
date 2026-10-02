from __future__ import annotations

from pathlib import Path
import json
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.production.normalized import normalize_extractions


class NormalizedExtractionTests(TestCase):
    def _temporary_directory(self) -> TemporaryDirectory:
        # The managed Windows temp root can inherit an ACL that blocks the
        # bundled interpreter; keep test-only fixtures inside the workspace.
        return TemporaryDirectory(dir=Path(__file__).resolve().parents[1])

    def test_merges_content_and_locator_without_losing_formula_table_or_figure(self) -> None:
        with self._temporary_directory() as directory:
            root = Path(directory)
            mineru = root / "content.json"
            docling = root / "document.json"
            mineru.write_text(json.dumps([
                [
                    {"type": "title", "content": {"title_content": [{"type": "text", "content": "I. INTRODUCTION"}]}, "bbox": [10, 10, 100, 20]},
                    {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "We study excess-authority errors."}]}, "bbox": [10, 30, 200, 50]},
                ],
                [
                    {"type": "equation", "content": {"latex": "\\mathbf{z}(a_t)=[z_{write},z_{exec}]"}, "bbox": [10, 10, 200, 30]},
                    {"type": "table", "content": {"table_caption": [{"type": "text", "content": "TABLE II"}], "html": "<table><tr><td>Metric</td><td>Seed 1</td></tr><tr><td>Safe success</td><td>98.48%</td></tr></table>"}, "bbox": [10, 40, 200, 100]},
                    {"type": "image", "content": {"image_source": {"path": "images/fig-1.jpg"}, "image_caption": [{"type": "text", "content": "Fig. 1. The loop."}]}, "bbox": [10, 110, 200, 160]},
                ],
            ], ensure_ascii=False), encoding="utf-8")
            docling.write_text(json.dumps({
                "pages": {"1": {"size": {"width": 210, "height": 297}}, "2": {"size": {"width": 210, "height": 297}}},
                "texts": [
                    {"self_ref": "#/texts/0", "label": "section_header", "text": "I. INTRODUCTION", "prov": [{"page_no": 1, "bbox": {"l": 10, "t": 20, "r": 100, "b": 10, "coord_origin": "BOTTOMLEFT"}}]},
                    {"self_ref": "#/texts/1", "label": "text", "text": "We study excess-authority errors.", "prov": [{"page_no": 1, "bbox": {"l": 10, "t": 50, "r": 200, "b": 30, "coord_origin": "BOTTOMLEFT"}}]},
                    {"self_ref": "#/texts/2", "label": "caption", "text": "TABLE II", "prov": [{"page_no": 2, "bbox": {"l": 10, "t": 250, "r": 100, "b": 240, "coord_origin": "BOTTOMLEFT"}}]},
                    {"self_ref": "#/texts/3", "label": "caption", "text": "Fig. 1. The loop.", "prov": [{"page_no": 2, "bbox": {"l": 10, "t": 180, "r": 100, "b": 170, "coord_origin": "BOTTOMLEFT"}}]},
                    {"self_ref": "#/texts/4", "label": "formula", "orig": "z(a_t)", "text": "", "prov": [{"page_no": 2, "bbox": {"l": 10, "t": 267, "r": 200, "b": 237, "coord_origin": "BOTTOMLEFT"}}]},
                ],
                "tables": [{"prov": [{"page_no": 2, "bbox": {"l": 10, "t": 257, "r": 200, "b": 197, "coord_origin": "BOTTOMLEFT"}}], "captions": [{"$ref": "#/texts/2"}], "data": {"table_cells": [
                    {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "Metric"},
                    {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "Seed 1"},
                    {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "Safe success"},
                    {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "98.48%"},
                ]}}],
                "pictures": [{"prov": [{"page_no": 2, "bbox": {"l": 10, "t": 187, "r": 200, "b": 137, "coord_origin": "BOTTOMLEFT"}}], "captions": [{"$ref": "#/texts/3"}]}],
            }, ensure_ascii=False), encoding="utf-8")

            normalized = normalize_extractions(
                source_id="2608.18351v1",
                source_url="https://arxiv.org/abs/2608.18351v1",
                mineru_content_list=mineru,
                docling_document=docling,
            )
            self.assertGreaterEqual(normalized.alignment_counts["aligned"], 3)
            formula = next(block for block in normalized.blocks if block.kind == "formula")
            self.assertIn("\\mathbf", formula.latex or "")
            self.assertEqual(formula.parse_status, "available")
            table = next(block for block in normalized.blocks if block.kind == "table")
            self.assertIn("98.48%", table.table_html or "")
            self.assertEqual((table.table_rows, table.table_columns), (2, 2))
            figure = next(block for block in normalized.blocks if block.kind == "figure")
            self.assertEqual(figure.image_path, "images/fig-1.jpg")
            self.assertTrue(all(block.sources for block in normalized.blocks))
            self.assertEqual(len(normalized.input_hashes["mineru_content_list"]), 64)

    def test_unmatched_docling_formula_is_retained_as_unparsed_single_source(self) -> None:
        with self._temporary_directory() as directory:
            root = Path(directory)
            (root / "content.json").write_text(json.dumps([[]]), encoding="utf-8")
            (root / "document.json").write_text(json.dumps({"texts": [{"label": "formula", "orig": "x+y", "text": "", "prov": [{"page_no": 1, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}]}]}), encoding="utf-8")
            normalized = normalize_extractions(source_id="s", source_url="https://example.com/s", mineru_content_list=root / "content.json", docling_document=root / "document.json")
            self.assertEqual(len(normalized.blocks), 1)
            self.assertEqual(normalized.blocks[0].alignment, "docling_only")
            self.assertEqual(normalized.blocks[0].parse_status, "unparsed")

    def test_write_emits_jsonl_and_metadata_manifest_only(self) -> None:
        with self._temporary_directory() as directory:
            root = Path(directory)
            mineru = root / "content.json"; docling = root / "document.json"
            mineru.write_text(json.dumps([[{"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "A statement."}]}, "bbox": [1, 1, 20, 10]}]]), encoding="utf-8")
            docling.write_text(json.dumps({"texts": []}), encoding="utf-8")
            normalized = normalize_extractions(source_id="s", source_url="https://example.com/s", mineru_content_list=mineru, docling_document=docling)
            blocks_path, manifest_path = normalized.write(root / "normalized")
            self.assertEqual(len(blocks_path.read_text(encoding="utf-8").splitlines()), 1)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["block_count"], 1)
            self.assertIs(manifest["complete"], True)
            self.assertEqual(list((root / "normalized").glob("*.tmp")), [])
            self.assertNotIn("A statement.", manifest_path.read_text(encoding="utf-8"))
