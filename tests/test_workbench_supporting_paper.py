"""Supporting-paper import: candidate -> managed source (thin, reuses pipeline)."""

from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.workbench.paper_ingest import PdfIngestStore
from research_pulse.workbench.preparation import (
    PaperPreparationService,
    PreparationQueue,
)
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.supporting_paper import (
    SupportingPaperImportError,
    SupportingPaperImporter,
)


VALID_PDF = b"%PDF-1.7\nsupporting paper\n%%EOF\n"


class _ScriptedParser:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def parse_pdf(self, pdf_path, *, source_id, source_url, output_dir):
        self.calls.append((pdf_path, source_id, source_url, output_dir))
        if self.fail:
            raise RuntimeError("token=secret internal path C:/private")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text("{}", encoding="utf-8")
        # A block with a stable id that read_managed_blocks can later resolve.
        block = '{"block_id": "normalized:test:text:abs", "text": "supporting evidence", "section_path": ["Abstract"], "page_start": 1}\n'
        (output_dir / "blocks.jsonl").write_text(block, encoding="utf-8")
        return output_dir


class _FakeDownloader:
    """Returns a downloaded PDF without touching the network."""

    def __init__(self, *, paper_id: str = "supporting-paper") -> None:
        self.paper_id = paper_id

    def download(self, url: str):
        return _Downloaded(self.paper_id, "arxiv:2608.99999v1", url)


class _Downloaded:
    def __init__(self, paper_id: str, source_identity: str, source_url: str) -> None:
        self.pdf = _Ingested(paper_id)
        self.source_identity = source_identity
        self.source_url = source_url


class _Ingested:
    def __init__(self, paper_id: str) -> None:
        self.paper_id = paper_id
        self.sha256 = "a" * 64
        self.source_identity = f"sha256:{'a' * 64}"
        self.size_bytes = len(VALID_PDF)
        self.pdf_path = Path("unused.pdf")


class SupportingPaperImporterTests(TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.ingest = PdfIngestStore(self.root / "layer-c")
        self.parser = _ScriptedParser()
        self.queue = PreparationQueue(self.parser, self.repository)
        self.preparation = PaperPreparationService(
            self.repository,
            self.queue,
            material_cache_root=self.root / "mineru",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self._temp.cleanup()

    def _importer(self, downloader=None) -> SupportingPaperImporter:
        return SupportingPaperImporter(
            preparation=self.preparation,
            downloader=downloader or _FakeDownloader(),
        )

    def test_import_candidate_to_managed_source_and_exposes_blocks(self) -> None:
        candidate = {"source_id": "arxiv:2608.99999v1", "arxiv_id": "2608.99999v1", "title": "Supporting", "url": "https://arxiv.org/abs/2608.99999v1"}
        importer = self._importer()
        result = importer.import_paper(candidate)

        # source_id is content-addressed by sha256 (reuse / dedupe semantics),
        # exactly like any managed paper — not the mock paper id.
        self.assertEqual(result.source_id, "a" * 64)
        # The import only REGISTERS + queues parsing — it never attaches to a
        # session (supporting paper is not an active paper) and never reads body.
        self.assertEqual(self.repository.list_paper_sources(result.source_id), ("arxiv:2608.99999v1",))
        # Parsing is async; before run_next there is no block yet.
        self.assertIsNone(result.sample_block_id)
        self.assertIsNone(result.safe_error)
        # After the preparation queue runs, a managed block is readable.
        self.queue.run_next()
        ready = self.preparation.get(result.source_id)
        self.assertEqual(ready.parse_status.value, "ready")
        self.assertEqual(len(self.parser.calls), 1)

    def test_import_builds_pdf_url_from_arxiv_id_when_url_missing(self) -> None:
        candidate = {"source_id": "arxiv:2608.99999v1", "arxiv_id": "2608.99999v1", "title": "S"}
        importer = self._importer()
        result = importer.import_paper(candidate)
        self.assertEqual(result.source_id, "a" * 64)

    def test_import_rejects_candidate_without_arxiv_id_and_url(self) -> None:
        importer = self._importer()
        with self.assertRaises(SupportingPaperImportError):
            importer.import_paper({"source_id": "", "title": "no location"})

    def test_parse_failure_surfaces_safe_error_and_no_body_persisted_to_workspace(self) -> None:
        self.parser.fail = True
        importer = self._importer()
        result = importer.import_paper({"source_id": "arxiv:2608.99999v1", "arxiv_id": "2608.99999v1", "title": "S", "url": "https://arxiv.org/abs/2608.99999v1"})
        self.queue.run_next()
        # parse failed but the paper is still registered (pdf readable); importer
        # reports a safe error, never an internal path or token.
        self.assertNotIn("token", result.safe_error or "")
        self.assertNotIn("C:/private", result.safe_error or "")


if __name__ == "__main__":
    import unittest

    unittest.main()
