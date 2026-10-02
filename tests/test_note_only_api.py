"""Note-only API wiring tests (knowledge timeline, no database)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from research_pulse.api.note_only import create_note_only_app


class NoteOnlyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # one published note in the fixture vault
        pub = self.root / "knowledge" / "papers" / "abc123v1"
        pub.mkdir(parents=True)
        # NOTE: Windows forbids ':' in filenames, matching ReaderNotePublisher's
        # _safe_version which rewrites ':' to '-'.
        (pub / "2026-08-25T00-00-00+00-00.md").write_text(
            "---\n"
            'knowledge_id: "kp:arxiv:abc123v1"\n'
            'knowledge_version: "2026-08-25T00:00:00+00:00"\n'
            'publication_status: "published"\n'
            'evidence_level: "full_text_text"\n'
            'source_urls: ["https://arxiv.org/abs/abc123v1"]\n'
            'domain: "test"\n'
            'title: "Fixture Paper"\n'
            "schema_version: 1\n"
            "---\n"
            "# Fixture Paper\n\nbody\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_knowledge_timeline_and_detail_without_scheduler(self) -> None:
        app = create_note_only_app(vault_root=self.root / "knowledge", enable_scheduler=False)
        client = TestClient(app)
        listing = client.get("/api/knowledge?limit=10")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([i["knowledge_id"] for i in listing.json()["items"]], ["kp:arxiv:abc123v1"])
        detail = client.get("/api/knowledge/kp:arxiv:abc123v1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["title"], "Fixture Paper")
        self.assertIn("body", detail.json()["markdown"])

    def test_unbacked_endpoints_answer_503_without_scheduler(self) -> None:
        app = create_note_only_app(vault_root=self.root / "knowledge", enable_scheduler=False)
        client = TestClient(app)
        self.assertEqual(client.post("/api/chat", json={"query": "hi"}).status_code, 503)
        self.assertEqual(client.get("/api/research-topics").status_code, 503)

    def test_workbench_runtime_is_owned_by_lifespan_without_breaking_knowledge(self) -> None:
        database_path = self.root / "workbench" / "custom.sqlite3"
        app = create_note_only_app(
            vault_root=self.root / "knowledge",
            enable_scheduler=False,
            workbench_database_path=database_path,
        )

        with TestClient(app) as client:
            created = client.post(
                "/api/workbench/sessions",
                json={"research_question": "工作台是否可恢复？"},
            )
            knowledge = client.get("/api/knowledge")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(knowledge.status_code, 200)
        self.assertTrue(database_path.is_file())

    def test_scheduler_wiring_enabled_with_deepseek_configured(self) -> None:
        import os

        if not os.getenv("DEEPSEEK_API_KEY"):
            self.skipTest("DEEPSEEK_API_KEY not configured")
        app = create_note_only_app(
            vault_root=self.root / "knowledge",
            normalized_root=self.root,
            enable_scheduler=True,
        )
        client = TestClient(app)
        self.assertEqual(client.get("/api/knowledge").status_code, 200)
        status = client.get("/api/scheduler")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["enabled"])


if __name__ == "__main__":
    unittest.main()
