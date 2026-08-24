from __future__ import annotations

import os
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.runtime import create_runtime_app


ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(TestCase):
    def test_runtime_starts_without_deepseek_key_for_read_only_rag(self) -> None:
        database_url = os.environ.get("RESEARCH_PULSE_TEST_DATABASE_URL")
        if not database_url:
            self.skipTest("Set RESEARCH_PULSE_TEST_DATABASE_URL to test the runtime factory.")
        previous_database_url = os.environ.get("DATABASE_URL")
        previous_api_key = os.environ.get("DEEPSEEK_API_KEY")
        markdown = ROOT / "knowledge" / "papers" / "test-runtime" / "2026-08-22-provenance-sample.md"
        sidecar = markdown.with_suffix(".provenance.json")
        os.environ["DATABASE_URL"] = database_url
        os.environ["DEEPSEEK_API_KEY"] = ""
        created_topic_id = None
        try:
            markdown.parent.mkdir(parents=True, exist_ok=True)
            markdown.write_bytes(
                (ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.md").read_bytes()
            )
            sidecar.write_bytes(
                (ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.provenance.json").read_bytes()
            )
            client = TestClient(create_runtime_app())
            self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
            self.assertEqual(client.get("/api/knowledge").status_code, 200)
            detail = client.get("/api/knowledge/kp%3Afixture%3Aprovenance")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("# Provenance Sample", detail.json()["markdown"])
            created = client.post(
                "/api/research-topics",
                json={"name": "Runtime no-key topic", "query": "LLM agent memory"},
            )
            self.assertEqual(created.status_code, 202)
            created_topic_id = created.json()["topic"]["topic_id"]
            run_id = created.json()["run"]["run_id"]
            run = client.get(f"/api/production-runs/{run_id}")
            self.assertEqual(run.status_code, 200)
            self.assertEqual(run.json()["error_code"], "deepseek_not_configured")
        finally:
            markdown.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            if created_topic_id:
                import psycopg

                with psycopg.connect(database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("DELETE FROM research_topics WHERE topic_id = %s", (created_topic_id,))
            if previous_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_database_url
            if previous_api_key is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = previous_api_key
