from __future__ import annotations

from pathlib import Path
import json
import shutil
import unittest
from uuid import uuid4

from worker.discover import parse_arxiv_feed
from worker.fulltext import (
    ParsedDocumentBlock,
    ParsedFullText,
    FullTextError,
    TRANSIENT_ROOT,
    _extract_document_blocks,
    arxiv_pdf_url,
    parse_fulltext,
    write_parse_receipt,
)

from test_discover import ATOM_FEED


class _Document:
    def export_to_markdown(self) -> str:
        return "# Parsed paper\n\n$E=mc^2$"

    def iterate_items(self):
        return ()


class _Result:
    document = _Document()


class _Converter:
    def convert(self, source: object) -> _Result:
        payload = source.stream.read()
        source.stream.seek(0)
        if not payload.startswith(b"%PDF"):
            raise AssertionError("The converter should receive an in-memory PDF stream.")
        return _Result()


class _Label:
    value = "table"


class _TableData:
    def __init__(self, grid: list[list[str]] | None = None) -> None:
        self.grid = grid


class _TableItem:
    label = _Label()

    def __init__(
        self,
        *,
        grid: list[list[str]] | None = None,
        text: str = "",
        caption: str | None = None,
        exported: str | None = None,
        export_error: bool = False,
    ) -> None:
        self.data = _TableData(grid)
        self.text = text
        self.caption = caption
        self.exported = exported
        self.export_error = export_error
        self.prov = ()

    def export_to_markdown(self, _document=None) -> str:
        if self.export_error:
            raise RuntimeError("opaque table export failure")
        return self.exported or ""


class _TableDocument:
    def __init__(self, item: _TableItem) -> None:
        self.item = item

    def iterate_items(self):
        return ((self.item, 0),)


class FullTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = parse_arxiv_feed(ATOM_FEED)[0]

    def test_arxiv_pdf_url_preserves_version(self) -> None:
        self.assertEqual(
            arxiv_pdf_url(self.candidate),
            "https://arxiv.org/pdf/2608.00001v1",
        )

    def test_parse_uses_temporary_pdf_and_keeps_markdown_in_memory(self) -> None:
        calls: list[object] = []
        workspaces: list[Path] = []

        def fake_download(url: str, destination: Path, timeout: int) -> None:
            calls.extend([url, timeout])
            workspaces.append(destination.parent)
            destination.write_bytes(b"%PDF-1.7 test")

        def fake_converter(formulas: bool, timeout: int) -> _Converter:
            calls.extend([formulas, timeout])
            return _Converter()

        parsed = parse_fulltext(
            self.candidate,
            with_formulas=True,
            timeout_seconds=19,
            downloader=fake_download,
            converter_factory=fake_converter,
        )

        self.assertEqual(calls, ["https://arxiv.org/pdf/2608.00001v1", 19, True, 19])
        self.assertTrue(parsed.formula_enriched)
        self.assertIn("$E=mc^2$", parsed.markdown)
        self.assertEqual(parsed.character_count, len(parsed.markdown))
        self.assertEqual(len(workspaces), 1)
        self.assertFalse(workspaces[0].exists())

    def test_parse_reuses_complete_source_cache_without_download_or_docling(self) -> None:
        cache_root = TRANSIENT_ROOT / f"cache-first-{uuid4()}"
        source_root = cache_root / self.candidate.source_id
        (source_root / "normalized").mkdir(parents=True)
        (source_root / "source.pdf").write_bytes(b"%PDF-1.7 cached")
        (source_root / "parse.md").write_text("# Cached paper\n\nCached full text.", encoding="utf-8")
        (source_root / "fetch.json").write_text(
            json.dumps({"source_id": self.candidate.source_id, "sha256": "cached"}),
            encoding="utf-8",
        )
        (source_root / "parsed.json").write_text(
            json.dumps(
                {
                    "source_id": self.candidate.source_id,
                    "pdf_url": arxiv_pdf_url(self.candidate),
                    "formula_enriched": True,
                    "blocks": [],
                }
            ),
            encoding="utf-8",
        )
        (source_root / "normalized" / "blocks.jsonl").write_text("", encoding="utf-8")
        (source_root / "normalized" / "manifest.json").write_text(
            json.dumps({"source_id": self.candidate.source_id, "status": "complete"}),
            encoding="utf-8",
        )

        def fail_download(*_args, **_kwargs):
            raise AssertionError("a complete source cache must not download")

        def fail_converter(*_args, **_kwargs):
            raise AssertionError("a complete source cache must not run Docling")

        try:
            parsed = parse_fulltext(
                self.candidate,
                source_cache_root=cache_root,
                downloader=fail_download,
                converter_factory=fail_converter,
            )
        finally:
            shutil.rmtree(cache_root, ignore_errors=True)

        self.assertEqual(parsed.markdown, "# Cached paper\n\nCached full text.")
        self.assertTrue(parsed.formula_enriched)
        self.assertEqual(parsed.source_id, self.candidate.source_id)

    def test_parse_drops_docling_error_context_before_workspace_cleanup(self) -> None:
        workspaces: list[Path] = []

        class _FailingConverter:
            def convert(self, source: object) -> _Result:
                raise RuntimeError("opaque Docling parser failure")

        def fake_download(_url: str, destination: Path, _timeout: int) -> None:
            workspaces.append(destination.parent)
            destination.write_bytes(b"%PDF-1.7 test")

        with self.assertRaises(FullTextError) as caught:
            parse_fulltext(
                self.candidate,
                downloader=fake_download,
                converter_factory=lambda _formulas, _timeout: _FailingConverter(),
            )

        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(len(workspaces), 1)
        self.assertFalse(workspaces[0].exists())

    def test_source_cache_preserves_pdf_and_failure_metadata_when_docling_fails(self) -> None:
        cache_root = TRANSIENT_ROOT / f"cache-failure-{uuid4()}"
        calls: list[str] = []

        def fake_download(_url: str, destination: Path, _timeout: int) -> None:
            calls.append("download")
            destination.write_bytes(b"%PDF-1.7 retained")

        class _FailingConverter:
            def convert(self, _source: object) -> object:
                calls.append("convert")
                raise RuntimeError("opaque parser failure")

        try:
            with self.assertRaises(FullTextError):
                parse_fulltext(
                    self.candidate,
                    source_cache_root=cache_root,
                    downloader=fake_download,
                    converter_factory=lambda _formulas, _timeout: _FailingConverter(),
                    stream_factory=lambda _path: object(),
                )
            source_root = cache_root / self.candidate.source_id
            self.assertTrue((source_root / "source.pdf").is_file())
            failure = json.loads((source_root / "fetch.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["stage"], "docling")
            self.assertEqual(calls, ["download", "convert"])
        finally:
            shutil.rmtree(cache_root, ignore_errors=True)

    def test_receipt_contains_metadata_not_extracted_text(self) -> None:
        parsed = ParsedFullText(
            self.candidate.source_id,
            "https://arxiv.org/pdf/2608.00001v1",
            "# Parsed paper\n\n$E=mc^2$",
            False,
            blocks=(ParsedDocumentBlock("formula", "private parsed formula body", section_path=("Method",)),),
        )
        TRANSIENT_ROOT.mkdir(parents=True, exist_ok=True)
        receipt_path = TRANSIENT_ROOT / f"test-receipt-{uuid4()}.json"
        try:
            write_parse_receipt(parsed, receipt_path)
            content = receipt_path.read_text(encoding="utf-8")
            payload = json.loads(content)
        finally:
            receipt_path.unlink(missing_ok=True)

        self.assertEqual(payload["persistence"], "metadata_only; source PDF and extracted full text were temporary")
        self.assertNotIn("E=mc", content)
        self.assertNotIn("private parsed formula body", content)

    def test_native_table_projection_preserves_complete_bounded_comparison(self) -> None:
        item = _TableItem(
            grid=[
                ["Metric", "Baseline", "Proposed"],
                ["Success", "67.75%", "90.50%"],
            ],
            caption="Table V. Continuation results",
        )

        block = _extract_document_blocks(_TableDocument(item))[0]

        self.assertEqual(block.kind, "table")
        self.assertEqual(
            block.text,
            "| Metric | Baseline | Proposed |\n| --- | --- | --- |\n| Success | 67.75% | 90.50% |",
        )
        self.assertEqual(block.caption, "Table V. Continuation results")

    def test_table_with_caption_only_remains_an_incomplete_projection(self) -> None:
        block = _extract_document_blocks(
            _TableDocument(_TableItem(caption="Table V. Continuation results"))
        )[0]

        self.assertEqual(block.text, "Table V. Continuation results")
        self.assertNotIn("|", block.text)

    def test_table_with_title_only_remains_an_incomplete_projection(self) -> None:
        block = _extract_document_blocks(
            _TableDocument(_TableItem(text="TABLE V. CONTINUATION RESULTS"))
        )[0]

        self.assertEqual(block.text, "TABLE V. CONTINUATION RESULTS")
        self.assertNotIn("|", block.text)

    def test_large_table_is_rejected_instead_of_prefix_truncated(self) -> None:
        private_tail = "PRIVATE-TAIL-MUST-NOT-SURVIVE"
        grid = [["Metric", "Baseline", "Proposed"]]
        grid.extend([[f"metric-{index}", str(index), private_tail if index == 30 else str(index + 1)] for index in range(31)])

        block = _extract_document_blocks(
            _TableDocument(_TableItem(grid=grid, caption="Large comparison"))
        )[0]

        self.assertEqual(block.text, "Large comparison")
        self.assertNotIn(private_tail, block.text)

    def test_large_table_cannot_fall_back_to_unbounded_item_text(self) -> None:
        private_tail = "PRIVATE-RAW-TABLE-TAIL-MUST-NOT-SURVIVE"
        raw_table = "| Metric | Baseline | Proposed |\n" + "\n".join(
            f"| metric-{index} | {index} | {private_tail if index == 30 else index + 1} |"
            for index in range(31)
        )
        grid = [["Metric", "Baseline", "Proposed"]]
        grid.extend([[f"metric-{index}", str(index), str(index + 1)] for index in range(31)])

        block = _extract_document_blocks(
            _TableDocument(_TableItem(grid=grid, text=raw_table, caption="Large comparison"))
        )[0]

        self.assertEqual(block.text, "Large comparison")
        self.assertNotIn(private_tail, block.text)

    def test_table_export_error_falls_back_to_bounded_title(self) -> None:
        block = _extract_document_blocks(
            _TableDocument(
                _TableItem(
                    text="TABLE VI. EXPORT FAILURE",
                    caption="Table VI",
                    export_error=True,
                )
            )
        )[0]

        self.assertEqual(block.text, "TABLE VI. EXPORT FAILURE")
        self.assertNotIn("opaque", block.text)


if __name__ == "__main__":
    unittest.main()
