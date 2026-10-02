"""Public-contract tests for arXiv HTML -> normalized paper blocks."""

from __future__ import annotations

import unittest

from research_pulse.production.html_adapter import parse_arxiv_html


class HtmlTableNormalizationTests(unittest.TestCase):
    def test_table_html_is_emitted_as_a_complete_cell_grid(self) -> None:
        html = (
            '<html><body><figure id="T1" class="ltx_table">'
            '<table class="ltx_tabular"><tbody>'
            '<tr id="T1.r1"><th scope="col">Model</th><th>Acc</th></tr>'
            '<tr><td>MemGuard</td><td>55.8</td></tr>'
            '</tbody></table></figure></body></html>'
        )

        blocks = parse_arxiv_html("2608.21867", html)

        table = next(block for block in blocks if block["kind"] == "table")
        self.assertEqual(table["table_rows"], 2)
        self.assertEqual(table["table_columns"], 2)
        self.assertEqual(
            table["table_html"],
            (
                '<table><tbody><tr id="T1.r1"><th scope="col">Model</th><th>Acc</th></tr>'
                '<tr><td>MemGuard</td><td>55.8</td></tr></tbody></table>'
            ),
        )

    def test_optional_latex_html_cell_end_tags_are_canonicalized(self) -> None:
        html = (
            '<html><body><figure id="T1" class="ltx_table">'
            '<table><tbody>'
            '<tr><th>Model<th>Acc'
            '<tr><td>MemGuard<td>55.8'
            '</tbody></table></figure></body></html>'
        )

        blocks = parse_arxiv_html("2608.21867", html)

        table = next(block for block in blocks if block["kind"] == "table")
        self.assertEqual(table["table_rows"], 2)
        self.assertEqual(table["table_columns"], 2)
        self.assertEqual(
            table["table_html"],
            (
                '<table><tbody><tr><th>Model</th><th>Acc</th></tr>'
                '<tr><td>MemGuard</td><td>55.8</td></tr></tbody></table>'
            ),
        )

    def test_table_html_escapes_formula_text_that_looks_like_an_html_tag(self) -> None:
        html = (
            '<html><body><figure id="T1" class="ltx_table"><table><tbody>'
            '<tr><th>Model</th><th>p-value</th></tr>'
            '<tr><td>MemGuard</td><td>'
            '<math id="T1.r2.c2.m1" alttext="&lt;0.001">'
            '<semantics><mn>0.001</mn></semantics></math>'
            '</td></tr></tbody></table></figure></body></html>'
        )

        blocks = parse_arxiv_html("2608.21867", html)

        table = next(block for block in blocks if block["kind"] == "table")
        self.assertIn(r"\(&lt;0.001\)", table["table_html"])
        self.assertNotIn(r"\(<0.001\)", table["table_html"])


if __name__ == "__main__":
    unittest.main()
