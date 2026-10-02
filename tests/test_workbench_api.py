from __future__ import annotations

from datetime import UTC, datetime
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.models import Paper, ParseStatus, PdfStatus
from research_pulse.workbench.sessions import SessionService


NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)


class _EmptyKnowledgeReader:
    def recent(self, *, limit: int):
        return ()

    def get_current(self, knowledge_id: str):
        return None


class _PaperKnowledgeReader:
    def recent(self, *, limit: int):
        return ()

    def get_current(self, knowledge_id: str):
        if knowledge_id not in {
            "kp:arxiv:2608.16447v1",
            "legacy-note",
            "generic-note",
            "kp:arxiv:9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b",
        }:
            return None
        source_urls = ("https://arxiv.org/abs/2608.16447v1",)
        if knowledge_id == "generic-note":
            source_urls = ("https://papers.example.test/dpo4rec.pdf",)
        if knowledge_id.startswith("kp:arxiv:9c7c"):
            source_urls = (
                "urn:sha256:9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b",
            )
        return type("Knowledge", (), {
            "source_urls": source_urls,
        })()


class _MemoryRepository:
    def __init__(self) -> None:
        self.sessions = {}
        self.paper_links = {}
        self.papers = {}
        self.source_aliases = {}

    def create(self, session):
        self.sessions[session.session_id] = session

    def get(self, session_id):
        return self.sessions.get(session_id)

    def list(self, lifecycle=None):
        sessions = tuple(self.sessions.values())
        if lifecycle is not None:
            sessions = tuple(item for item in sessions if item.lifecycle == lifecycle)
        return sessions

    def save(self, session):
        self.sessions[session.session_id] = session

    def delete(self, session_id):
        self.sessions.pop(session_id, None)
        self.paper_links.pop(session_id, None)

    def link_paper(self, session_id, paper_id):
        self.paper_links.setdefault(session_id, []).append(paper_id)

    def list_paper_ids(self, session_id):
        return tuple(self.paper_links.get(session_id, ()))

    def get_paper(self, paper_id):
        return self.papers.get(paper_id)

    def get_paper_by_source_alias(self, source_alias):
        return self.papers.get(self.source_aliases.get(source_alias, source_alias))

    def get_paper_by_source_url(self, source_url):
        return next(
            (paper for paper in self.papers.values() if paper.source_url == source_url),
            None,
        )


class _FakeNoteImporter:
    """Register one note source without touching the network in API tests."""

    def __init__(self, repository: _MemoryRepository) -> None:
        self.repository = repository
        self.calls: list[dict[str, object]] = []

    def import_paper(self, candidate: dict[str, object]):
        self.calls.append(candidate)
        source_id = str(candidate["source_id"])
        source_url = str(candidate["url"])
        paper = Paper(
            paper_id=f"paper-{source_id}",
            source_identity=f"arxiv:{source_id}",
            title=str(candidate.get("title") or "Imported paper"),
            source_url=source_url,
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.READY,
            material_root="managed/material",
        )
        self.repository.papers[paper.paper_id] = paper
        self.repository.source_aliases[f"arxiv:{source_id}"] = paper.paper_id
        return type("ImportResult", (), {"source_id": paper.paper_id})()


class WorkbenchSessionApiTests(TestCase):
    def setUp(self) -> None:
        self.repository = _MemoryRepository()
        ids = iter(("session-empty", "session-question", "session-paper"))
        service = SessionService(
            self.repository,
            session_id_factory=lambda: next(ids),
            clock=lambda: NOW,
        )
        self.client = TestClient(
            create_app(
                knowledge_reader=_EmptyKnowledgeReader(),
                workbench_session_service=service,
            )
        )

    def test_create_empty_question_first_and_paper_first_sessions(self) -> None:
        empty = self.client.post("/api/workbench/sessions", json={})
        question = self.client.post(
            "/api/workbench/sessions", json={"research_question": "  研究什么？ "}
        )
        paper = self.client.post(
            "/api/workbench/sessions", json={"paper_id": "paper-1"}
        )

        self.assertEqual(empty.status_code, 201)
        self.assertEqual(empty.json()["session_id"], "session-empty")
        self.assertEqual(question.json()["research_question"], "研究什么？")
        self.assertEqual(question.json()["title"], "研究什么？")
        self.assertEqual(paper.json()["paper_ids"], ["paper-1"])

    def test_knowledge_note_enters_only_an_already_prepared_paper(self) -> None:
        repository = _MemoryRepository()
        repository.papers["arxiv:2608.16447v1"] = Paper(
            paper_id="paper-from-note",
            source_identity="https://arxiv.org/abs/2608.16447v1",
            title="A parsed paper",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.READY,
            material_root="managed/material",
        )
        service = SessionService(repository, session_id_factory=lambda: "session-from-note", clock=lambda: NOW)
        client = TestClient(create_app(knowledge_reader=_PaperKnowledgeReader(), workbench_session_service=service))

        response = client.post("/api/knowledge/kp:arxiv:2608.16447v1/workbench-session", json={})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["paper_ids"], ["paper-from-note"])
        self.assertTrue(response.json()["paper_panel_open"])
        self.assertEqual(response.json()["active_paper_id"], "paper-from-note")

    def test_reopening_knowledge_note_reuses_active_paper_session(self) -> None:
        repository = _MemoryRepository()
        repository.papers["arxiv:2608.16447v1"] = Paper(
            paper_id="paper-from-note",
            source_identity="https://arxiv.org/abs/2608.16447v1",
            title="A parsed paper",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.READY,
            material_root="managed/material",
        )
        ids = iter(("session-from-note-1", "session-from-note-2"))
        service = SessionService(repository, session_id_factory=lambda: next(ids), clock=lambda: NOW)
        client = TestClient(create_app(knowledge_reader=_PaperKnowledgeReader(), workbench_session_service=service))

        first = client.post("/api/knowledge/kp:arxiv:2608.16447v1/workbench-session", json={})
        second = client.post("/api/knowledge/kp:arxiv:2608.16447v1/workbench-session", json={})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["session_id"], first.json()["session_id"])
        self.assertEqual(len(repository.sessions), 1)

    def test_knowledge_note_rejects_a_paper_that_is_still_parsing(self) -> None:
        repository = _MemoryRepository()
        repository.papers["arxiv:2608.16447v1"] = Paper(
            paper_id="paper-not-ready",
            source_identity="https://arxiv.org/abs/2608.16447v1",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.PARSING,
        )
        service = SessionService(repository, session_id_factory=lambda: "session-not-ready", clock=lambda: NOW)
        client = TestClient(create_app(knowledge_reader=_PaperKnowledgeReader(), workbench_session_service=service))

        response = client.post("/api/knowledge/kp:arxiv:2608.16447v1/workbench-session", json={})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "paper_not_ready")

    def test_knowledge_note_status_exposes_importable_unregistered_source(self) -> None:
        repository = _MemoryRepository()
        importer = _FakeNoteImporter(repository)
        service = SessionService(repository, session_id_factory=lambda: "session-status", clock=lambda: NOW)
        client = TestClient(create_app(
            knowledge_reader=_PaperKnowledgeReader(),
            workbench_session_service=service,
            workbench_paper_importer=importer,
        ))

        response = client.get("/api/knowledge/kp:arxiv:2608.16447v1/workbench-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_registered")
        self.assertTrue(response.json()["can_import"])
        self.assertEqual(response.json()["source_url"], "https://arxiv.org/abs/2608.16447v1")

    def test_knowledge_note_status_allows_retry_for_failed_import(self) -> None:
        repository = _MemoryRepository()
        failed = Paper(
            paper_id="failed-paper",
            source_identity="sha256:failed",
            title="Failed paper",
            source_url="https://arxiv.org/abs/2608.16447v1",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.FAILED,
            material_root="managed/material",
            safe_error="论文解析失败，可重试",
        )
        repository.papers[failed.paper_id] = failed
        repository.source_aliases["arxiv:2608.16447v1"] = failed.paper_id
        importer = _FakeNoteImporter(repository)
        service = SessionService(repository, session_id_factory=lambda: "session-status", clock=lambda: NOW)
        client = TestClient(create_app(
            knowledge_reader=_PaperKnowledgeReader(),
            workbench_session_service=service,
            workbench_paper_importer=importer,
        ))

        response = client.get("/api/knowledge/kp:arxiv:2608.16447v1/workbench-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertTrue(response.json()["can_import"])

    def test_import_note_registers_source_and_opens_session(self) -> None:
        repository = _MemoryRepository()
        importer = _FakeNoteImporter(repository)
        service = SessionService(repository, session_id_factory=lambda: "session-imported", clock=lambda: NOW)
        client = TestClient(create_app(
            knowledge_reader=_PaperKnowledgeReader(),
            workbench_session_service=service,
            workbench_paper_importer=importer,
        ))

        response = client.post("/api/knowledge/kp:arxiv:2608.16447v1/workbench-import", json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["paper_ids"], ["paper-2608.16447v1"])
        self.assertEqual(len(importer.calls), 1)
        self.assertTrue(response.json()["paper_panel_open"])

    def test_import_note_is_idempotent_after_registration(self) -> None:
        repository = _MemoryRepository()
        importer = _FakeNoteImporter(repository)
        service = SessionService(repository, session_id_factory=lambda: "session-imported", clock=lambda: NOW)
        client = TestClient(create_app(
            knowledge_reader=_PaperKnowledgeReader(),
            workbench_session_service=service,
            workbench_paper_importer=importer,
        ))

        first = client.post("/api/knowledge/kp:arxiv:2608.16447v1/workbench-import", json={})
        second = client.post("/api/knowledge/kp:arxiv:2608.16447v1/workbench-import", json={})

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["session_id"], first.json()["session_id"])
        self.assertEqual(len(importer.calls), 1)

    def test_legacy_note_url_resolves_the_arxiv_source_alias(self) -> None:
        repository = _MemoryRepository()
        repository.papers["arxiv:2608.16447v1"] = Paper(
            paper_id="paper-from-legacy-note",
            source_identity="arxiv:2608.16447v1",
            title="A parsed paper",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.READY,
            material_root="managed/material",
        )
        service = SessionService(repository, session_id_factory=lambda: "session-legacy-note", clock=lambda: NOW)
        client = TestClient(create_app(knowledge_reader=_PaperKnowledgeReader(), workbench_session_service=service))

        response = client.post("/api/knowledge/legacy-note/workbench-session", json={})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["paper_ids"], ["paper-from-legacy-note"])

    def test_generic_public_pdf_note_resolves_by_registered_source_url(self) -> None:
        repository = _MemoryRepository()
        repository.papers["sha256:generic"] = Paper(
            paper_id="paper-from-generic-note",
            source_identity="sha256:generic",
            source_url="https://papers.example.test/dpo4rec.pdf",
            title="A parsed generic paper",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.READY,
            material_root="managed/material",
        )
        service = SessionService(repository, session_id_factory=lambda: "session-generic-note", clock=lambda: NOW)
        client = TestClient(create_app(knowledge_reader=_PaperKnowledgeReader(), workbench_session_service=service))

        response = client.post("/api/knowledge/generic-note/workbench-session", json={})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["paper_ids"], ["paper-from-generic-note"])

    def test_content_addressed_note_with_legacy_arxiv_prefix_resolves_uploaded_paper(self) -> None:
        digest = "9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b"
        repository = _MemoryRepository()
        repository.papers[digest] = Paper(
            paper_id=digest,
            source_identity=f"sha256:{digest}",
            title="Direct Preference Optimization for LLM-Enhanced Recommendation Systems",
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.READY,
            material_root="managed/material",
        )
        repository.source_aliases[f"sha256:{digest}"] = digest
        service = SessionService(repository, session_id_factory=lambda: "session-uploaded-note", clock=lambda: NOW)
        client = TestClient(create_app(knowledge_reader=_PaperKnowledgeReader(), workbench_session_service=service))

        response = client.post(
            f"/api/knowledge/kp:arxiv:{digest}/workbench-session",
            json={},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["paper_ids"], [digest])

    def test_list_detail_update_question_title_and_layout(self) -> None:
        session_id = self.client.post("/api/workbench/sessions", json={}).json()["session_id"]

        question = self.client.patch(
            f"/api/workbench/sessions/{session_id}",
            json={"research_question": "  新研究问题  "},
        )
        renamed = self.client.patch(
            f"/api/workbench/sessions/{session_id}", json={"title": "  手动标题 "}
        )
        layout = self.client.patch(
            f"/api/workbench/sessions/{session_id}",
            json={"paper_panel_open": True, "active_paper_id": "paper-1"},
        )
        listed = self.client.get("/api/workbench/sessions?lifecycle=active")
        detail = self.client.get(f"/api/workbench/sessions/{session_id}")

        self.assertEqual(question.json()["research_question"], "新研究问题")
        self.assertEqual(renamed.json()["title"], "手动标题")
        self.assertTrue(layout.json()["paper_panel_open"])
        self.assertEqual(layout.json()["active_paper_id"], "paper-1")
        self.assertEqual(len(listed.json()["items"]), 1)
        self.assertEqual(detail.json()["session_id"], session_id)

    def test_archive_restore_delete_and_missing_resource(self) -> None:
        session_id = self.client.post("/api/workbench/sessions", json={}).json()["session_id"]

        archived = self.client.post(f"/api/workbench/sessions/{session_id}/archive")
        active_list = self.client.get("/api/workbench/sessions?lifecycle=active")
        archived_list = self.client.get("/api/workbench/sessions?lifecycle=archived")
        restored = self.client.post(f"/api/workbench/sessions/{session_id}/restore")
        deleted = self.client.delete(f"/api/workbench/sessions/{session_id}")
        missing = self.client.get(f"/api/workbench/sessions/{session_id}")

        self.assertEqual(archived.json()["lifecycle"], "archived")
        self.assertEqual(active_list.json()["items"], [])
        self.assertEqual(len(archived_list.json()["items"]), 1)
        self.assertEqual(restored.json()["lifecycle"], "active")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(missing.status_code, 404)
