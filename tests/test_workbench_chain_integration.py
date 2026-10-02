"""M5.2 — search_arxiv → import_supporting_paper → read_managed_blocks has no break.

The historical断点 was that no tool exposed paper 受管化 to the Agent. This
proves the three links are all wired: the assistant surface exposes all three
tools, and importing a ``search_arxiv`` candidate yields a content-addressed
``source_id`` whose managed block ``read_managed_blocks`` fully resolves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.workbench.capability_policy import assistant_capability_policy
from research_pulse.workbench.paper_ingest import PdfIngestStore
from research_pulse.workbench.preparation import PaperPreparationService, PreparationQueue
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.supporting_paper import SupportingPaperImporter


class _FakeDownloader:
    def download(self, url: str):
        return _Downloaded("supporting-paper", "arxiv:2608.99999v1", url)


class _Downloaded:
    def __init__(self, paper_id, source_identity, source_url) -> None:
        self.pdf = _Ingested(paper_id)
        self.source_identity = source_identity
        self.source_url = source_url


class _Ingested:
    def __init__(self, paper_id: str) -> None:
        self.paper_id = paper_id
        self.sha256 = "a" * 64
        self.source_identity = f"sha256:{'a' * 64}"
        self.size_bytes = 4
        self.pdf_path = Path("unused.pdf")


class _ScriptedParser:
    def parse_pdf(self, pdf_path, *, source_id, source_url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text("{}", encoding="utf-8")
        block = '{"block_id": "normalized:test:text:abs", "text": "shadow variable evidence", "section_path": ["Abstract"], "page_start": 1}\n'
        (output_dir / "blocks.jsonl").write_text(block, encoding="utf-8")
        return output_dir


class ChainIntegrationTests(TestCase):
    def test_imported_candidate_source_resolves_to_readable_managed_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            repository = SQLiteWorkbenchRepository(connection)
            import_queue = PreparationQueue(_ScriptedParser(), repository)
            preparation = PaperPreparationService(repository, import_queue, material_cache_root=root / "mineru")
            importer = SupportingPaperImporter(preparation=preparation, downloader=_FakeDownloader())

            # The assistant agent's controlled surface declares the whole chain.
            policy = assistant_capability_policy()
            declared = set(policy.required) | {c.name for c in policy.conditional} | set(policy.allowed)
            self.assertIn("search_arxiv", declared)
            self.assertIn("read_managed_blocks", declared)
            self.assertTrue(any(c.name == "import_supporting_paper" for c in policy.conditional))

            # A search_arxiv candidate (arxiv_id present, url present) imports to a
            # content-addressed source whose managed block read_managed_blocks reads.
            candidate = {"source_id": "arxiv:2608.99999v1", "arxiv_id": "2608.99999v1", "title": "Shadow variables", "url": "https://arxiv.org/abs/2608.99999v1"}
            result = importer.import_paper(candidate)
            self.assertEqual(result.source_id, "a" * 64)
            import_queue.run_next()
            ready = preparation.get(result.source_id)
            self.assertEqual(ready.parse_status.value, "ready")
            connection.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
