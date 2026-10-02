from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from unittest import TestCase
import uuid

from fastapi.testclient import TestClient

from research_pulse.acceptance.validation import (
    AcceptanceValidationError,
    build_acceptance_app,
    snapshot_knowledge,
    snapshot_raw_material,
    verify_no_new_raw_material,
    verify_note_published,
    verify_note_readable,
)
from research_pulse.reader_production import ReaderNotePublisher


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "2608.00001v1"
SOURCE_URL = f"https://arxiv.org/abs/{SOURCE_ID}"


class _Candidate:
    source_id = SOURCE_ID
    source_url = SOURCE_URL
    domain = "llm_agent_memory"
    title = "A Real Paper"


class _Receipt:
    """A stub ReadingReceipt whose status is used to route the note bucket."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.completed_at = "2026-08-22T08:00:00Z"
        self.started_at = "2026-08-22T08:00:00Z"
        self.stop_reason = "coverage"


def _note_markdown() -> str:
    return (
        "# A Real Paper\n\n"
        "## 核心思路\n\nAgent memory method.\n\n"
        "## 实验设计\n\nAccuracy improves by 10%.\n\n"
        "## 结论\n\nThe paper introduces a task-conditioned permission boundary.\n"
    )


class AcceptanceNoteValidationTests(TestCase):
    def setUp(self) -> None:
        self.vault = ROOT / "data" / f"test-acceptance-note-vault-{uuid.uuid4().hex}"
        self.publisher = ReaderNotePublisher(self.vault)

    def tearDown(self) -> None:
        if self.vault.exists():
            for path in sorted(self.vault.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.vault.rmdir()

    def test_published_receipt_writes_canonical_paper_and_verifies(self) -> None:
        before = snapshot_knowledge(self.vault)
        self.publisher.publish(_Candidate(), _note_markdown(), _Receipt("completed"))

        verification = verify_note_published(vault_root=self.vault, before=before, source_id=SOURCE_ID)

        self.assertIsNotNone(verification.markdown_relative_path)
        self.assertEqual(verification.knowledge_id, f"kp:arxiv:{SOURCE_ID}")
        self.assertEqual(verification.knowledge_version, "2026-08-22T08:00:00Z")
        self.assertEqual(verification.content_sha256, sha256(_note_markdown().encode("utf-8")).hexdigest())
        self.assertTrue(verification.markdown.startswith("# A Real Paper"))

    def test_failed_receipt_writes_no_canonical_paper_and_rejects(self) -> None:
        before = snapshot_knowledge(self.vault)
        self.publisher.publish(_Candidate(), _note_markdown(), _Receipt("failed"))

        with self.assertRaises(AcceptanceValidationError):
            verify_note_published(vault_root=self.vault, before=before, source_id=SOURCE_ID)

    def test_wrong_source_id_is_rejected(self) -> None:
        self.publisher.publish(_Candidate(), _note_markdown(), _Receipt("completed"))
        with self.assertRaisesRegex(AcceptanceValidationError, "does not match"):
            verify_note_published(vault_root=self.vault, before=frozenset(), source_id="other")

    def test_reading_api_returns_the_published_note(self) -> None:
        self.publisher.publish(_Candidate(), _note_markdown(), _Receipt("completed"))
        note = verify_note_published(vault_root=self.vault, before=frozenset(), source_id=SOURCE_ID)
        client = TestClient(build_acceptance_app(interactive_graph=None, vault_root=self.vault))

        api = verify_note_readable(client, note)

        self.assertEqual(api.list_status, 200)
        self.assertEqual(api.detail_status, 200)

    def test_raw_material_inventory_reports_but_does_not_delete(self) -> None:
        root = self.vault / "managed"
        root.mkdir(parents=True)
        before = snapshot_raw_material([root])
        raw = root / "source.pdf"
        raw.write_bytes(b"%PDF-test")
        with self.assertRaisesRegex(AcceptanceValidationError, "root-0/source.pdf"):
            verify_no_new_raw_material(before=before, roots=[root])
        self.assertTrue(raw.exists())
