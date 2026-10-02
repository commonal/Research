from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.knowledge.reader import KnowledgeSummary
from research_pulse.workbench.models import Paper, ParseStatus, PdfStatus
from research_pulse.workbench.paper_access import PaperAccessService
from research_pulse.workbench.preparation import PreparationQueue
from research_pulse.workbench.sessions import ResearchSession, SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class _EmptyKnowledgeReader:
    def __init__(self, summaries=()):
        self.summaries = tuple(summaries)

    def recent(self, *, limit: int):
        return self.summaries[:limit]

    def get_current(self, knowledge_id: str):
        return None


class _UnusedParser:
    def parse_pdf(self, pdf_path, *, source_id, source_url, output_dir):
        raise AssertionError("retry API must enqueue, not parse inline")


class WorkbenchPaperApiTests(TestCase):
    def setUp(self) -> None:
        self.root = Path("tests/.workbench-paper-api")
        self.layer_c = self.root / "layer-c"
        self.material_cache = self.root / "mineru"
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.session_service = SessionService(
            self.repository,
            session_id_factory=lambda: "session-1",
            clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
        )
        self.session_service.create_empty()
        self.queue = PreparationQueue(_UnusedParser(), self.repository)
        access = PaperAccessService(
            self.repository,
            self.session_service,
            self.queue,
            layer_c_root=self.layer_c,
            material_cache_root=self.material_cache,
        )
        self.knowledge_reader = _EmptyKnowledgeReader()
        self.client = TestClient(
            create_app(
                knowledge_reader=self.knowledge_reader,
                workbench_session_service=self.session_service,
                workbench_paper_service=access,
            )
        )

        self.pdf_path = self.layer_c / "paper-1" / "source.pdf"
        self.pdf_path.parent.mkdir(parents=True)
        self.pdf_bytes = b"%PDF-1.7\nrange-test\n%%EOF\n"
        self.pdf_path.write_bytes(self.pdf_bytes)
        self.material_root = self.material_cache / "paper-1" / "material"
        self.material_root.mkdir(parents=True)
        block = {
            "block_id": "normalized:paper-1:text:abcdef123456",
            "kind": "text",
            "text": "Evidence",
            "section_path": ["Results"],
            "page_start": 2,
            "page_end": 2,
            "bbox": [10, 20, 30, 40],
            "sources": [{"parser": "mineru_api", "locator": "content_list.json#/1"}],
            "alignment": "mineru_only",
            "parse_status": "available",
            "confidence": 0.8,
        }
        (self.material_root / "blocks.jsonl").write_text(
            json.dumps(block) + "\n", encoding="utf-8"
        )
        (self.material_root / "manifest.json").write_text(
            json.dumps({"complete": True, "block_count": 1}), encoding="utf-8"
        )
        self.repository.upsert_paper(
            Paper(
                paper_id="paper-1",
                source_identity="sha256:paper-1",
                pdf_path=str(self.pdf_path),
                pdf_status=PdfStatus.READY,
                parse_status=ParseStatus.READY,
                material_root=str(self.material_root),
            )
        )

    def test_paper_projection_includes_the_current_published_note(self) -> None:
        self.knowledge_reader.summaries = (
            KnowledgeSummary(
                knowledge_id="kp:arxiv:2608.16447v1",
                knowledge_version="2026-09-01T00:00:00+00:00",
                title="A parsed paper note",
                domain="recommendation",
                evidence_level="full_text_text",
                source_url="https://arxiv.org/abs/2608.16447v1",
                provenance_status="complete",
            ),
        )
        self.repository.save_paper(
            Paper(
                paper_id="paper-1",
                source_identity="arxiv:2608.16447v1",
                source_url="https://arxiv.org/abs/2608.16447v1",
                pdf_path=str(self.pdf_path),
                pdf_status=PdfStatus.READY,
                parse_status=ParseStatus.READY,
                material_root=str(self.material_root),
            )
        )
        self.repository.add_paper_source(
            "paper-1", "arxiv:2608.16447v1", "https://arxiv.org/abs/2608.16447v1"
        )
        self.client.post("/api/workbench/sessions/session-1/papers/paper-1")

        response = self.client.get("/api/workbench/sessions/session-1/papers")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["note_status"], "published")
        self.assertEqual(response.json()["items"][0]["note_url"], "kp:arxiv:2608.16447v1")

    def tearDown(self) -> None:
        self.connection.close()
        if self.root.exists():
            for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.root.rmdir()

    def test_attach_list_workspace_shares_papers_and_remove_keeps_material(self) -> None:
        attached = self.client.post("/api/workbench/sessions/session-1/papers/paper-1")
        listed = self.client.get("/api/workbench/sessions/session-1/papers")

        second_pdf = self.layer_c / "paper-2" / "source.pdf"
        second_pdf.parent.mkdir(parents=True)
        second_pdf.write_bytes(self.pdf_bytes)
        self.repository.upsert_paper(
            Paper(
                paper_id="paper-2",
                source_identity="sha256:paper-2",
                pdf_path=str(second_pdf),
                pdf_status=PdfStatus.READY,
                parse_status=ParseStatus.IDLE,
            )
        )
        # A workspace (research project) holds several shared papers.
        second = self.client.post("/api/workbench/sessions/session-1/papers/paper-2")
        listed_after = self.client.get("/api/workbench/sessions/session-1/papers")
        removed = self.client.delete("/api/workbench/sessions/session-1/papers/paper-1")

        self.assertEqual(attached.status_code, 200)
        self.assertEqual(
            attached.json()["pdf_url"],
            "/api/workbench/sessions/session-1/papers/paper-1/pdf",
        )
        self.assertEqual([item["paper_id"] for item in listed.json()["items"]], ["paper-1"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            [item["paper_id"] for item in listed_after.json()["items"]],
            ["paper-1", "paper-2"],
        )
        self.assertEqual(removed.status_code, 204)
        self.assertTrue(self.pdf_path.is_file())
        self.assertTrue(self.material_root.is_dir())

    def test_pdf_supports_byte_range_without_exposing_server_path(self) -> None:
        self.client.post("/api/workbench/sessions/session-1/papers/paper-1")

        response = self.client.get(
            "/api/workbench/sessions/session-1/papers/paper-1/pdf",
            headers={"Range": "bytes=0-7"},
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.pdf_bytes[:8])
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertNotIn(str(self.root), response.text)

    def test_locator_requires_exact_block_and_reports_detached_after_remove(self) -> None:
        self.client.post("/api/workbench/sessions/session-1/papers/paper-1")
        exact = self.client.get(
            "/api/workbench/sessions/session-1/papers/paper-1/blocks/normalized:paper-1:text:abcdef123456"
        )
        abbreviated = self.client.get(
            "/api/workbench/sessions/session-1/papers/paper-1/blocks/abcdef"
        )
        self.client.delete("/api/workbench/sessions/session-1/papers/paper-1")
        detached = self.client.get(
            "/api/workbench/sessions/session-1/papers/paper-1/blocks/normalized:paper-1:text:abcdef123456"
        )

        self.assertEqual(exact.json()["status"], "resolved")
        self.assertEqual(exact.json()["page"], 2)
        self.assertEqual(exact.json()["bbox"], [10.0, 20.0, 30.0, 40.0])
        self.assertEqual(abbreviated.json()["status"], "unresolved")
        self.assertEqual(detached.json()["status"], "detached")

    def test_blocks_projection_exposes_order_and_bbox_contract(self) -> None:
        self.client.post("/api/workbench/sessions/session-1/papers/paper-1")

        response = self.client.get(
            "/api/workbench/sessions/session-1/papers/paper-1/blocks"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [{
            "block_id": "normalized:paper-1:text:abcdef123456",
            "text": "Evidence",
            "section_path": ["Results"],
            "page": 2,
            "order": 0,
            "bbox": [10.0, 20.0, 30.0, 40.0],
            "bbox_format": "xyxy",
            "bbox_space": "page_points",
        }])

    def test_mineru_api_blocks_expose_normalized_page_coordinate_contract(self) -> None:
        manifest = json.loads((self.material_root / "manifest.json").read_text(encoding="utf-8"))
        manifest["parser"] = "mineru_api"
        (self.material_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.client.post("/api/workbench/sessions/session-1/papers/paper-1")

        response = self.client.get(
            "/api/workbench/sessions/session-1/papers/paper-1/blocks"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["bbox_space"], "normalized_1000")
        self.assertEqual(response.json()["items"][0]["bbox_dimensions"], [1000.0, 1000.0])

    def test_bbox_projection_recovers_reversed_parser_coordinates(self) -> None:
        payload = json.loads((self.material_root / "blocks.jsonl").read_text(encoding="utf-8"))
        payload["bbox"] = [30, 40, 10, 20]
        (self.material_root / "blocks.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.client.post("/api/workbench/sessions/session-1/papers/paper-1")

        response = self.client.get(
            "/api/workbench/sessions/session-1/papers/paper-1/blocks"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["bbox"], [10.0, 20.0, 30.0, 40.0])

    def test_retry_failed_parse_enqueues_and_keeps_pdf_ready(self) -> None:
        failed = self.repository.get_paper("paper-1")
        self.repository.save_paper(
            Paper(
                **{
                    **failed.__dict__,
                    "parse_status": ParseStatus.FAILED,
                    "safe_error": "old error",
                }
            )
        )
        self.client.post("/api/workbench/sessions/session-1/papers/paper-1")

        retried = self.client.post(
            "/api/workbench/sessions/session-1/papers/paper-1/retry"
        )

        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json()["pdf_status"], "ready")
        self.assertEqual(retried.json()["parse_status"], "queued")
        self.assertEqual(self.queue.pending_count, 1)

    def test_unmanaged_pdf_path_is_rejected_without_path_disclosure(self) -> None:
        outside = Path("tests/.outside-workbench-paper.pdf")
        outside.write_bytes(self.pdf_bytes)
        try:
            self.repository.upsert_paper(
                Paper(
                    paper_id="paper-outside",
                    source_identity="sha256:outside",
                    pdf_path=str(outside),
                    pdf_status=PdfStatus.READY,
                    parse_status=ParseStatus.IDLE,
                )
            )
            self.session_service.attach_paper("session-1", "paper-outside")

            response = self.client.get(
                "/api/workbench/sessions/session-1/papers/paper-outside/pdf"
            )
        finally:
            outside.unlink(missing_ok=True)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(str(outside), response.text)
