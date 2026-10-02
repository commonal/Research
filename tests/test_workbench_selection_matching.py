from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.selection_matching import (
    MatchableBlock,
    NormalizedBox,
    PdfSelection,
    match_selection_to_block,
    normalize_selection_text,
)


class WorkbenchSelectionMatchingTests(TestCase):
    def setUp(self) -> None:
        self.blocks = (
            MatchableBlock(
                block_id="B-left",
                text="An office-aware memory retriever ranks candidates.",
                page=1,
                bbox=NormalizedBox(0.10, 0.20, 0.45, 0.30),
                order=1,
            ),
            MatchableBlock(
                block_id="B-right",
                text="A complementary signal improves recall.",
                page=1,
                bbox=NormalizedBox(0.55, 0.20, 0.90, 0.30),
                order=2,
            ),
            MatchableBlock(
                block_id="B-page-2",
                text="A complementary signal improves recall.",
                page=2,
                bbox=NormalizedBox(0.10, 0.10, 0.90, 0.20),
                order=3,
            ),
        )

    def test_normalizes_whitespace_line_hyphenation_and_ligatures(self) -> None:
        self.assertEqual(
            normalize_selection_text("  Ofﬁce-\n aware   memory  "),
            "officeaware memory",
        )
        self.assertEqual(normalize_selection_text("office- aware"), "officeaware")

    def test_text_match_is_limited_to_same_page(self) -> None:
        result = match_selection_to_block(
            PdfSelection("complementary signal", page=2), self.blocks
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.block_id, "B-page-2")
        self.assertEqual(result.method, "text")

    def test_bbox_disambiguates_identical_text_without_crossing_columns(self) -> None:
        repeated = self.blocks + (
            MatchableBlock(
                block_id="B-left-repeat",
                text="A complementary signal improves recall.",
                page=1,
                bbox=NormalizedBox(0.10, 0.20, 0.45, 0.30),
                order=4,
            ),
        )
        result = match_selection_to_block(
            PdfSelection(
                "complementary signal",
                page=1,
                bbox=NormalizedBox(0.56, 0.21, 0.78, 0.27),
            ),
            repeated,
        )

        self.assertEqual(result.block_id, "B-right")

    def test_bbox_fallback_and_unresolved_are_explicit(self) -> None:
        fallback = match_selection_to_block(
            PdfSelection(
                "unseen formula",
                page=1,
                bbox=NormalizedBox(0.11, 0.21, 0.30, 0.27),
            ),
            self.blocks,
        )
        missing = match_selection_to_block(
            PdfSelection("missing", page=4), self.blocks
        )

        self.assertEqual(fallback.block_id, "B-left")
        self.assertEqual(fallback.method, "bbox")
        self.assertEqual(missing.status, "unresolved")
        self.assertIsNone(missing.block_id)

    def test_invalid_or_zero_area_boxes_do_not_create_false_match(self) -> None:
        result = match_selection_to_block(
            PdfSelection(
                "missing",
                page=1,
                bbox=NormalizedBox(0.2, 0.2, 0.2, 0.3),
            ),
            self.blocks,
        )

        self.assertEqual(result.status, "unresolved")
