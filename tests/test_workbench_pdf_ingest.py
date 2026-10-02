from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest import TestCase

from research_pulse.workbench.paper_ingest import (
    DEFAULT_MAX_PDF_BYTES,
    PdfIngestError,
    PdfIngestStore,
)


VALID_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


class PdfIngestStoreTests(TestCase):
    def setUp(self) -> None:
        self.root = Path("tests/.workbench-pdf-ingest")
        self.created_paths: list[Path] = []

    def tearDown(self) -> None:
        for path in reversed(self.created_paths):
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent != self.root and parent.exists():
                parent.rmdir()
        if self.root.exists():
            for temporary in self.root.glob(".upload-*.tmp"):
                temporary.unlink(missing_ok=True)
            self.root.rmdir()

    def test_default_limit_is_configurable_100_mib(self) -> None:
        self.assertEqual(DEFAULT_MAX_PDF_BYTES, 100 * 1024 * 1024)
        self.assertEqual(PdfIngestStore(self.root, max_pdf_bytes=123).max_pdf_bytes, 123)

    def test_valid_pdf_is_hashed_and_atomically_committed(self) -> None:
        result = PdfIngestStore(self.root).ingest(BytesIO(VALID_PDF))
        self.created_paths.append(result.pdf_path)

        self.assertTrue(result.created)
        self.assertEqual(result.source_identity, f"sha256:{result.sha256}")
        self.assertEqual(result.paper_id, result.sha256)
        self.assertEqual(result.pdf_path.read_bytes(), VALID_PDF)
        self.assertEqual(list(self.root.glob(".upload-*.tmp")), [])

    def test_rejects_non_pdf_truncated_and_oversize_without_residue(self) -> None:
        store = PdfIngestStore(self.root, max_pdf_bytes=len(VALID_PDF) - 1)

        with self.assertRaisesRegex(PdfIngestError, "PDF 文件头"):
            store.ingest(BytesIO(b"not a pdf"))
        with self.assertRaisesRegex(PdfIngestError, "PDF 文件不完整"):
            store.ingest(BytesIO(b"%PDF-1.7\ntruncated"))
        with self.assertRaisesRegex(PdfIngestError, "超过 100 MiB|超过配置上限"):
            store.ingest(BytesIO(VALID_PDF))

        self.assertEqual(list(self.root.rglob("*")) if self.root.exists() else [], [])

    def test_duplicate_content_reuses_existing_pdf(self) -> None:
        store = PdfIngestStore(self.root)
        first = store.ingest(BytesIO(VALID_PDF))
        second = store.ingest(BytesIO(VALID_PDF))
        self.created_paths.append(first.pdf_path)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.paper_id, first.paper_id)
        self.assertEqual(second.pdf_path, first.pdf_path)
        self.assertEqual(list(self.root.glob(".upload-*.tmp")), [])
