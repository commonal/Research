from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path
from unittest import TestCase

from research_pulse.workbench.paper_ingest import PdfIngestStore
from research_pulse.workbench.preparation import PaperPreparationService, PreparationQueue
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


VALID_PDF = b"%PDF-1.7\nshared paper\n%%EOF\n"


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
        (output_dir / "blocks.jsonl").write_text("{}\n", encoding="utf-8")
        return output_dir


class PaperPreparationTests(TestCase):
    def setUp(self) -> None:
        self.root = Path("tests/.workbench-preparation")
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.ingest = PdfIngestStore(self.root / "layer-c")

    def tearDown(self) -> None:
        self.connection.close()
        if self.root.exists():
            for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.root.rmdir()

    def test_aliases_merge_on_content_hash_and_parse_is_queued_once(self) -> None:
        parser = _ScriptedParser()
        queue = PreparationQueue(parser, self.repository)
        service = PaperPreparationService(
            self.repository,
            queue,
            material_cache_root=self.root / "mineru",
        )
        local = self.ingest.ingest(BytesIO(VALID_PDF))

        first = service.register_pdf(local, source_alias="upload:local")
        second = service.register_pdf(
            local,
            source_alias="arxiv:2608.12345v1",
            source_url="https://arxiv.org/abs/2608.12345v1",
        )

        self.assertEqual(first.paper_id, second.paper_id)
        self.assertEqual(
            self.repository.list_paper_sources(first.paper_id),
            ("arxiv:2608.12345v1", "upload:local"),
        )
        self.assertEqual(queue.pending_count, 1)
        capabilities = service.capabilities(first.paper_id)
        self.assertTrue(capabilities.pdf_readable)
        self.assertFalse(capabilities.structured_ready)
        self.assertFalse(capabilities.can_ask_with_paper)
        self.assertFalse(capabilities.can_generate_note)

        queue.run_next()

        ready = service.get(first.paper_id)
        self.assertEqual(ready.pdf_status.value, "ready")
        self.assertEqual(ready.parse_status.value, "ready")
        self.assertEqual(len(parser.calls), 1)
        self.assertTrue(service.capabilities(first.paper_id).can_generate_note)

    def test_parse_failure_preserves_readable_pdf_and_safe_retry_state(self) -> None:
        parser = _ScriptedParser(fail=True)
        queue = PreparationQueue(parser, self.repository)
        service = PaperPreparationService(
            self.repository,
            queue,
            material_cache_root=self.root / "mineru",
        )
        registered = service.register_pdf(self.ingest.ingest(BytesIO(VALID_PDF)))

        queue.run_next()

        failed = service.get(registered.paper_id)
        self.assertEqual(failed.pdf_status.value, "ready")
        self.assertEqual(failed.parse_status.value, "failed")
        self.assertTrue(service.capabilities(failed.paper_id).pdf_readable)
        self.assertFalse(service.capabilities(failed.paper_id).structured_ready)
        self.assertNotIn("secret", failed.safe_error or "")
        self.assertNotIn("C:/private", failed.safe_error or "")

    def test_source_alias_cannot_be_silently_rebound_to_other_content(self) -> None:
        self.repository.insert_paper_stub("paper-a", "sha256:a")
        self.repository.insert_paper_stub("paper-b", "sha256:b")
        self.repository.add_paper_source("paper-a", "arxiv:2608.12345v1")

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.add_paper_source("paper-b", "arxiv:2608.12345v1")

        self.assertEqual(
            self.repository.list_paper_sources("paper-a"),
            ("arxiv:2608.12345v1",),
        )
