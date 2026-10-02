from __future__ import annotations

from unittest import TestCase

from research_pulse.workbench.context_resolution import (
    ContextResolutionError,
    ContextResolver,
    ResolvableBlock,
)


class WorkbenchContextResolverTests(TestCase):
    def setUp(self) -> None:
        self.blocks = (
            ResolvableBlock("block:intro", "Intro", ("Introduction",), 1, 0),
            ResolvableBlock("block:method", "Method", ("Method",), 2, 1),
            ResolvableBlock("block:detail", "Detail", ("Method", "Details"), 2, 2),
            ResolvableBlock("block:result", "Result", ("Results",), 3, 3),
        )
        self.resolver = ContextResolver(max_blocks=3)

    def test_none_scope_reads_no_paper_blocks(self) -> None:
        resolved = self.resolver.resolve("none", self.blocks)

        self.assertEqual(resolved.blocks, ())
        self.assertFalse(resolved.truncated)

    def test_selection_returns_only_exact_full_block_id(self) -> None:
        resolved = self.resolver.resolve(
            "selection", self.blocks, block_id="block:detail"
        )

        self.assertEqual(
            tuple(block.block_id for block in resolved.blocks), ("block:detail",)
        )
        with self.assertRaises(ContextResolutionError):
            self.resolver.resolve("selection", self.blocks, block_id="detail")

    def test_selection_accepts_multiple_exact_block_ids_in_document_order(self) -> None:
        resolved = self.resolver.resolve(
            "selection", self.blocks,
            block_ids=("block:detail", "block:method"),
        )

        self.assertEqual(
            tuple(block.block_id for block in resolved.blocks),
            ("block:method", "block:detail"),
        )
        self.assertEqual(resolved.block_ids, ("block:detail", "block:method"))

    def test_section_includes_target_and_descendants_but_not_neighbor_sections(self) -> None:
        resolved = self.resolver.resolve(
            "section", self.blocks, section_path=("Method",)
        )

        self.assertEqual(
            tuple(block.block_id for block in resolved.blocks),
            ("block:method", "block:detail"),
        )
        self.assertEqual(resolved.section_path, ("Method",))

    def test_full_is_deterministic_and_records_truncation(self) -> None:
        resolved = self.resolver.resolve("full", tuple(reversed(self.blocks)))

        self.assertEqual(
            tuple(block.block_id for block in resolved.blocks),
            ("block:intro", "block:method", "block:detail"),
        )
        self.assertTrue(resolved.truncated)
        self.assertEqual(resolved.available_block_count, 4)
        self.assertEqual(resolved.included_block_count, 3)

    def test_unknown_scope_or_missing_target_is_rejected(self) -> None:
        with self.assertRaises(ContextResolutionError):
            self.resolver.resolve("explore", self.blocks)
        with self.assertRaises(ContextResolutionError):
            self.resolver.resolve("section", self.blocks, section_path=("Missing",))
