from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path
from unittest import TestCase
from uuid import uuid4

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.traceable_reading.service import TraceableReadingOutcome
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.workbench.agent_runtime import SafeAgentEvent
from research_pulse.workbench.models import CandidateFinding, ExplorationRun, ExplorationStatus, Paper, ParseStatus, PdfStatus, NoteRunStatus
from research_pulse.workbench.note_events import NoteRunEventProjector
from research_pulse.workbench.note_pipeline import ObservedReadingProvider
from research_pulse.workbench.note_runs import NoteRunService
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class _Pipeline:
    def __init__(self) -> None:
        self.fail = False
        self.raise_exc: Exception | None = None
        self.error: str | None = None
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.fail:
            payload = {"receipt_path": "receipts/failed.json"}
            if self.error is not None:
                payload["error"] = self.error
            return TraceableReadingOutcome(1, payload)
        return TraceableReadingOutcome(0, {
            "receipt_path": "receipts/published.json",
            "knowledge_id": "kp:arxiv:paper-1",
        })


class _Knowledge:
    def recent(self, *, limit: int): return ()
    def get_current(self, knowledge_id: str): return None


class _ReadableKnowledge:
    def recent(self, *, limit: int): return ()

    def get_current(self, knowledge_id: str):
        if knowledge_id != "kp:arxiv:paper-1":
            return None
        return object()


class _FilesystemDraftPipeline(_Pipeline):
    """Write the smallest canonical bundle needed to exercise approval."""

    def __init__(self, vault_root: Path) -> None:
        super().__init__()
        self.vault_root = vault_root

    def run(self, request):
        path = self.vault_root / "papers" / "paper-1" / "v1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '---\n'
            'knowledge_id: "kp:arxiv:paper-1"\n'
            'knowledge_version: "2026-09-16T00:00:00+00:00"\n'
            f'publication_status: "{request.publication_status}"\n'
            'title: "Paper 1"\n'
            'domain: "research"\n'
            'evidence_level: "source_linked_unverified"\n'
            'rag_eligible: false\n'
            'source_urls: ["https://example.test/paper"]\n'
            'schema_version: 1\n'
            '---\n\n# Draft\n',
            encoding="utf-8",
        )
        receipt = self.vault_root / "receipts" / "paper-1" / "v1.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            '{"knowledge_id":"kp:arxiv:paper-1", "publication_status":"published"}\n',
            encoding="utf-8",
        )
        return TraceableReadingOutcome(0, {
            "published_path": str(path),
            "receipt_path": str(receipt),
            "knowledge_id": "kp:arxiv:paper-1",
        })


class _MalformedFilesystemDraftPipeline(_FilesystemDraftPipeline):
    """Emit a draft that the knowledge reader must reject before approval."""

    def run(self, request):
        outcome = super().run(request)
        path = self.vault_root / "papers" / "paper-1" / "v1.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace('schema_version: 1\n', 'schema_version: "broken"\n'),
            encoding="utf-8",
        )
        return outcome


def _local_test_dir() -> Path:
    """Use the repository-local scratch root (Windows temp ACLs are restricted)."""
    root = Path(".codex-test-tmp") / f"note-run-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    return root


class WorkbenchNoteRunTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)
        sessions = SessionService(self.repository, session_id_factory=lambda: "session-1")
        sessions.create_empty()
        self.repository.upsert_paper(Paper(
            "paper-1", "sha256:paper-1", pdf_status=PdfStatus.READY, parse_status=ParseStatus.READY,
            source_url="https://example.test/paper", pdf_path="managed/source.pdf",
            material_root="managed/material",
        ))
        sessions.attach_paper("session-1", "paper-1")
        self.pipeline = _Pipeline()
        ids = iter(("note-1", "note-2", "note-3"))
        self.service = NoteRunService(
            self.repository, self.pipeline, vault_root=Path("knowledge"), cache_root=Path("data"),
            note_run_id_factory=lambda: next(ids),
            events=NoteRunEventProjector(self.repository),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _unrelated_state(self):
        return {
            "paper": self.repository.get_paper("paper-1"),
            "messages": self.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "explorations": self.connection.execute("SELECT COUNT(*) FROM exploration_runs").fetchone()[0],
        }

    def test_queued_generating_published_and_failed_retry_are_independent(self) -> None:
        before = self._unrelated_state()
        queued = self.service.create("session-1", "paper-1")
        draft = self.service.execute(queued.note_run_id)
        self.pipeline.fail = True
        self.repository.upsert_paper(Paper(
            "paper-2", "sha256:paper-2", pdf_status=PdfStatus.READY, parse_status=ParseStatus.READY,
            source_url="https://example.test/paper-2", pdf_path="managed/source-2.pdf",
            material_root="managed/material-2",
        ))
        self.repository.link_paper("session-1", "paper-2")
        failed = self.service.execute(self.service.create("session-1", "paper-2").note_run_id)
        retried = self.service.retry(failed.note_run_id)

        self.assertEqual(queued.status, NoteRunStatus.QUEUED)
        self.assertEqual(draft.status, NoteRunStatus.AWAITING_APPROVAL)
        self.assertIsNone(draft.knowledge_id)
        self.assertEqual(failed.status, NoteRunStatus.FAILED)
        self.assertEqual(retried.status, NoteRunStatus.QUEUED)
        self.assertEqual(retried.previous_note_run_id, failed.note_run_id)
        self.assertEqual([item.attempt for item in self.repository.list_note_runs("session-1")], [1, 1, 2])
        self.assertEqual(self._unrelated_state(), before)

    def test_successful_note_stops_at_draft_until_user_approves_publication(self) -> None:
        """A note run must not silently publish from the generation task."""
        queued = self.service.create("session-1", "paper-1")

        draft = self.service.execute(queued.note_run_id)

        self.assertEqual(draft.status.value, "awaiting_approval")
        self.assertEqual(draft.stage, "awaiting_approval")
        self.assertIsNone(draft.knowledge_id)

    def test_chat_exploration_and_candidate_text_never_cross_the_note_evidence_boundary(self) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO messages (message_id, session_id, role, text, scope, generation_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("m-secret", "session-1", "user", "CHAT-MUST-NOT-BE-EVIDENCE", "none", "completed", "2026-09-01T00:00:00+00:00"),
            )
        self.repository.insert_exploration_run(ExplorationRun(
            "run-secret", "session-1", "EXPLORE-MUST-NOT-BE-EVIDENCE", 1,
            ExplorationStatus.COMPLETED,
        ))
        self.repository.insert_candidate_finding(CandidateFinding(
            "finding-secret", "run-secret", "FINDING-MUST-NOT-BE-EVIDENCE"
        ))

        completed = self.service.execute(self.service.create("session-1", "paper-1").note_run_id)

        self.assertEqual(completed.status, NoteRunStatus.AWAITING_APPROVAL)
        request_text = repr(self.pipeline.requests[-1])
        self.assertNotIn("CHAT-MUST-NOT-BE-EVIDENCE", request_text)
        self.assertNotIn("EXPLORE-MUST-NOT-BE-EVIDENCE", request_text)
        self.assertNotIn("FINDING-MUST-NOT-BE-EVIDENCE", request_text)
        self.assertEqual(self.pipeline.requests[-1].material_root, Path("managed/material"))

    def test_note_run_api_projects_draft_progress_and_deduplicates_repeated_start(self) -> None:
        client = TestClient(create_app(
            knowledge_reader=_Knowledge(),
            workbench_note_run_service=self.service,
        ))

        created = client.post("/api/workbench/sessions/session-1/papers/paper-1/note-runs")
        draft = client.get("/api/workbench/note-runs/note-1")
        repeated = client.post("/api/workbench/sessions/session-1/papers/paper-1/note-runs")
        listed = client.get("/api/workbench/sessions/session-1/note-runs").json()["items"]

        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json()["status"], "queued")
        self.assertEqual(draft.json()["status"], "awaiting_approval")
        self.assertTrue(draft.json()["can_publish"])
        self.assertIsNone(draft.json()["knowledge_id"])
        self.assertIsNone(draft.json()["draft_preview"])
        self.assertEqual(repeated.status_code, 202)
        self.assertEqual(repeated.json()["note_run_id"], "note-1")
        self.assertEqual([item["status"] for item in listed], ["awaiting_approval"])
        # The UI receives state flags, never process-local vault paths.
        self.assertIsNone(draft.json()["draft_path"])
        self.assertIsNone(draft.json()["receipt_path"])
        self.assertIsNone(listed[0]["knowledge_id"])
        self.assertEqual(self.repository.get_paper("paper-1").parse_status, ParseStatus.READY)

    def test_note_run_api_rejects_a_paper_until_structured_material_is_ready(self) -> None:
        self.repository.upsert_paper(Paper(
            "paper-pending", "sha256:paper-pending", pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.QUEUED, source_url="https://example.test/pending",
            pdf_path="managed/pending.pdf", material_root="managed/pending-material",
        ))
        self.repository.link_paper("session-1", "paper-pending")
        client = TestClient(create_app(
            knowledge_reader=_Knowledge(),
            workbench_note_run_service=self.service,
        ))

        response = client.post("/api/workbench/sessions/session-1/papers/paper-pending/note-runs")

        self.assertEqual(response.status_code, 409)
        self.assertIn("not ready", response.json()["detail"])
        self.assertEqual(self.repository.list_note_runs("session-1"), ())

    def test_failed_note_run_preserves_real_error_from_payload(self) -> None:
        queued = self.service.create("session-1", "paper-1")
        self.pipeline.fail = True
        self.pipeline.error = "TraceableReadingError:core_reading_incomplete"
        failed = self.service.execute(queued.note_run_id)
        self.assertEqual(failed.status, NoteRunStatus.FAILED)
        self.assertEqual(failed.safe_error, "TraceableReadingError:core_reading_incomplete")
        self.assertNotEqual(failed.safe_error, "正式笔记生成失败，可重试")

    def test_pipeline_exception_marks_failed_and_never_sticks(self) -> None:
        queued = self.service.create("session-1", "paper-1")
        self.pipeline.raise_exc = RuntimeError("provider blew up")
        failed = self.service.execute(queued.note_run_id)
        self.assertEqual(failed.status, NoteRunStatus.FAILED)
        self.assertIn("provider blew up", failed.safe_error or "")
        failed_event = self.repository.list_note_run_events(failed.note_run_id)[-1]
        self.assertEqual(failed_event.event_type, "note_failed")
        self.assertIn("elapsed_ms", failed_event.counters)

    def test_note_run_emits_sanitised_event_stream(self) -> None:
        queued = self.service.create("session-1", "paper-1")
        draft = self.service.execute(queued.note_run_id)
        events = self.repository.list_note_run_events(draft.note_run_id)
        types = [event.event_type for event in events]
        self.assertIn("run_started", types)
        self.assertIn("phase_changed", types)
        self.assertIn("note_ready", types)
        self.assertNotIn("note_completed", types)
        self.assertEqual(events[-1].event_type, "note_ready")
        for event in events:
            self.assertNotIn("content", event.stable_ids)
            self.assertNotIn("prompt", event.stable_ids)

    def test_observed_provider_records_operation_usage_and_elapsed(self) -> None:
        calls: list[tuple] = []

        class _Inner:
            text_model = "deepseek-model"

            def call_json(self, operation, model, prompt, image=None):
                return {"usage": {"prompt_tokens": 100, "completion_tokens": 50}, "content": "ok"}

        provider = ObservedReadingProvider(_Inner(), lambda op, model, usage, ms: calls.append((op, model, usage, ms)))
        result = provider.call_json("traceable_note_write", "deepseek-model", "prompt here")
        self.assertEqual(result["content"], "ok")
        self.assertEqual(len(calls), 1)
        operation, model, usage, elapsed_ms = calls[0]
        self.assertEqual(operation, "traceable_note_write")
        self.assertEqual(model, "deepseek-model")
        self.assertEqual(usage["completion_tokens"], 50)
        self.assertGreaterEqual(elapsed_ms, 0)

    def test_published_asset_unresolvable_marks_failed_honestly(self) -> None:
        class _Reader:
            def get_current(self, knowledge_id: str): return None

        service = NoteRunService(
            self.repository, self.pipeline, vault_root=Path("knowledge"), cache_root=Path("data"),
            note_run_id_factory=lambda: "note-orphan", events=NoteRunEventProjector(self.repository),
            reader=_Reader(),
        )
        # Pipeline "succeeds" (exit_code 0) but the published asset cannot be
        # resolved by the reader (e.g. source_urls is an urn: URL) -> must be
        # FAILED with the real-resolvability reason, not a fake "published".
        self.pipeline.fail = False
        queued = service.create("session-1", "paper-1")
        failed = service.execute(queued.note_run_id)
        self.assertEqual(failed.status, NoteRunStatus.FAILED)
        self.assertIn("HTTP", failed.safe_error or "")
        self.assertIsNone(failed.knowledge_id)

    def test_publish_promotes_the_draft_and_is_idempotent(self) -> None:
        directory = _local_test_dir()
        try:
            vault = directory / "knowledge"
            pipeline = _FilesystemDraftPipeline(vault)
            service = NoteRunService(
                self.repository,
                pipeline,
                vault_root=vault,
                cache_root=directory / "data",
                note_run_id_factory=lambda: "note-publish",
                events=NoteRunEventProjector(self.repository),
                reader=FilesystemKnowledgeReader(vault),
            )
            draft = service.execute(service.create("session-1", "paper-1").note_run_id)

            self.assertEqual(draft.status, NoteRunStatus.AWAITING_APPROVAL)
            front_matter = (vault / "papers" / "paper-1" / "v1.md").read_text(encoding="utf-8").split("\n---\n", 1)[0]
            self.assertEqual(front_matter.count("publication_status:"), 1)
            self.assertEqual(
                (vault / "papers" / "paper-1" / "v1.md").read_text(encoding="utf-8").splitlines()[3],
                'publication_status: "needs_review"',
            )
            self.assertIsNone(service.reader.get_current("kp:arxiv:paper-1"))

            published = service.publish(draft.note_run_id)
            self.assertEqual(published.status, NoteRunStatus.PUBLISHED)
            self.assertEqual(published.knowledge_id, "kp:arxiv:paper-1")
            self.assertEqual(published.receipt_path, str((vault / "receipts" / "paper-1" / "v1.json").resolve()))
            self.assertIsNone(published.draft_path)
            self.assertIsNotNone(service.reader.get_current("kp:arxiv:paper-1"))
            import json
            receipt = json.loads((vault / "receipts" / "paper-1" / "v1.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["publication_status"], "published")
            self.assertEqual(service.publish(draft.note_run_id).status, NoteRunStatus.PUBLISHED)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_publish_repairs_legacy_duplicate_publication_status(self) -> None:
        directory = _local_test_dir()
        try:
            vault = directory / "knowledge"
            service = NoteRunService(
                self.repository,
                _FilesystemDraftPipeline(vault),
                vault_root=vault,
                cache_root=directory / "data",
                note_run_id_factory=lambda: "note-repair-duplicate-status",
                events=NoteRunEventProjector(self.repository),
                reader=FilesystemKnowledgeReader(vault),
            )
            draft = service.execute(service.create("session-1", "paper-1").note_run_id)
            path = vault / "papers" / "paper-1" / "v1.md"
            original = path.read_text(encoding="utf-8")
            marker = 'publication_status: "needs_review"\n'
            self.assertEqual(original.count("publication_status:"), 1)
            path.write_text(original.replace(marker, marker + marker, 1), encoding="utf-8")

            published = service.publish(draft.note_run_id)

            self.assertEqual(published.status, NoteRunStatus.PUBLISHED)
            self.assertEqual(path.read_text(encoding="utf-8").count("publication_status:"), 1)
            self.assertIn('publication_status: "published"', path.read_text(encoding="utf-8"))
            self.assertIsNotNone(service.reader.get_current("kp:arxiv:paper-1"))
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_malformed_draft_fails_before_it_reaches_approval(self) -> None:
        directory = _local_test_dir()
        try:
            vault = directory / "knowledge"
            service = NoteRunService(
                self.repository,
                _MalformedFilesystemDraftPipeline(vault),
                vault_root=vault,
                cache_root=directory / "data",
                note_run_id_factory=lambda: "note-malformed-draft",
                events=NoteRunEventProjector(self.repository),
                reader=FilesystemKnowledgeReader(vault),
            )

            failed = service.execute(service.create("session-1", "paper-1").note_run_id)

            self.assertEqual(failed.status, NoteRunStatus.FAILED)
            self.assertEqual(failed.stage, "failed")
            self.assertIn("schema_version", failed.safe_error or "")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_note_run_api_requires_confirmation_before_publishing(self) -> None:
        directory = _local_test_dir()
        try:
            vault = directory / "knowledge"
            service = NoteRunService(
                self.repository,
                _FilesystemDraftPipeline(vault),
                vault_root=vault,
                cache_root=directory / "data",
                note_run_id_factory=lambda: "note-api-publish",
                events=NoteRunEventProjector(self.repository),
                reader=FilesystemKnowledgeReader(vault),
            )
            client = TestClient(create_app(
                knowledge_reader=FilesystemKnowledgeReader(vault),
                workbench_note_run_service=service,
            ))

            created = client.post("/api/workbench/sessions/session-1/papers/paper-1/note-runs")
            draft = client.get("/api/workbench/note-runs/note-api-publish")
            published = client.post("/api/workbench/note-runs/note-api-publish/publish")

            self.assertEqual(created.status_code, 202)
            self.assertEqual(draft.json()["status"], "awaiting_approval")
            self.assertIn("# Draft", draft.json()["draft_preview"])
            self.assertEqual(published.status_code, 200)
            self.assertEqual(published.json()["status"], "published")
            self.assertFalse(published.json()["can_publish"])
        finally:
            shutil.rmtree(directory, ignore_errors=True)
