from __future__ import annotations

from pathlib import Path
from hashlib import sha256
from datetime import UTC, datetime
from unittest import TestCase

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from research_pulse.api.app import create_app
from research_pulse.knowledge.models import DurableEvidenceAnchor, ReadingSectionEvidence
from research_pulse.knowledge.reader import FilesystemKnowledgeReader, KnowledgeDetail, KnowledgeReader
from research_pulse.rag.contracts import EvidenceHit, SearchRequest
from research_pulse.scheduling import SchedulerStatus
from research_pulse.topics.models import RunStatus, RunTrigger
from research_pulse.topics.service import TopicRunService
from tests.test_topics import _MemoryRepository, _Runner
from research_pulse.workflows.interactive import (
    CitationOnlyAnswerGenerator,
    InteractiveGraphDependencies,
    SupplementationResult,
    build_interactive_graph,
)


ROOT = Path(__file__).resolve().parents[1]


class _Rag:
    def __init__(self, hits: list[EvidenceHit]) -> None:
        self.hits = hits

    def publish(self, asset):
        return None

    def search(self, request: SearchRequest):
        return self.hits


class _Supplementer:
    def __init__(self, rag: _Rag, hits: list[EvidenceHit]) -> None:
        self.rag = rag
        self.hits = hits

    def supplement(self, *, query: str, domain: str | None, reason: str) -> SupplementationResult:
        self.rag.hits = self.hits
        return SupplementationResult("gap:api", "completed", tuple(hit.knowledge_id for hit in self.hits))


def _hit(number: int) -> EvidenceHit:
    excerpt = f"Evidence {number}"
    source_url = f"https://example.com/{number}"
    source_anchor = DurableEvidenceAnchor(
        anchor_id=f"source:{number}",
        source_url=source_url,
        evidence_excerpt=excerpt,
        excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
        section="Results",
    )
    return EvidenceHit(
        chunk_id=f"chunk:{number}",
        knowledge_id=f"kp:api:{number}",
        knowledge_version="2026-08-22T00:00:00Z",
        title=f"API Paper {number}",
        text=excerpt,
        source_url=source_url,
        anchor_id=f"anchor:{number}",
        dense_score=None,
        keyword_score=0.1,
        fused_score=0.1,
        claim_id=f"claim:{number}",
        claim_type="source_fact",
        source_anchors=(source_anchor,),
    )


class ApiTests(TestCase):
    def _client(
        self,
        initial_hits: list[EvidenceHit],
        supplemented_hits: list[EvidenceHit],
        reader: KnowledgeReader | None = None,
    ) -> TestClient:
        rag = _Rag(initial_hits)
        graph = build_interactive_graph(
            InteractiveGraphDependencies(rag, _Supplementer(rag, supplemented_hits), CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        return TestClient(
            create_app(
                interactive_graph=graph,
                knowledge_reader=reader or FilesystemKnowledgeReader(ROOT / "knowledge"),
            )
        )

    def test_health_and_empty_or_fixture_timeline(self) -> None:
        client = self._client([], [])

        self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
        response = client.get("/api/knowledge?limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    def test_mixed_timeline_shows_legacy_and_valid_v2_but_hides_broken_v2(self) -> None:
        vault = ROOT / "data" / "test-mixed-vault"
        legacy_target = vault / "papers" / "legacy" / "legacy.md"
        valid_target = vault / "papers" / "valid" / "2026-08-22-provenance-sample.md"
        sidecar_target = valid_target.with_suffix(".provenance.json")
        broken_target = vault / "papers" / "broken" / "broken.md"
        paths = (legacy_target, valid_target, sidecar_target, broken_target)
        try:
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
            legacy_target.write_bytes(
                (ROOT / "knowledge" / "fixtures" / "2026-08-22-validated-agent-memory.md").read_bytes()
            )
            valid_target.write_bytes(
                (ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.md").read_bytes()
            )
            sidecar_target.write_bytes(
                (ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.provenance.json").read_bytes()
            )
            broken_target.write_text(
                valid_target.read_text(encoding="utf-8").replace(
                    'provenance_file: "2026-08-22-provenance-sample.provenance.json"',
                    'provenance_file: "missing.provenance.json"',
                ),
                encoding="utf-8",
            )

            records = FilesystemKnowledgeReader(vault).recent(limit=10)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

        self.assertEqual(len(records), 2)
        self.assertEqual(
            {record.provenance_status for record in records},
            {"complete", "legacy_missing_provenance"},
        )

    def test_detail_returns_current_markdown_and_sources(self) -> None:
        vault = ROOT / "data" / "test-api-detail-vault"
        markdown = vault / "papers" / "valid" / "2026-08-22-provenance-sample.md"
        sidecar = markdown.with_suffix(".provenance.json")
        try:
            markdown.parent.mkdir(parents=True, exist_ok=True)
            markdown.write_bytes(
                (ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.md").read_bytes()
            )
            sidecar.write_bytes(
                (ROOT / "knowledge" / "fixtures" / "2026-08-22-provenance-sample.provenance.json").read_bytes()
            )
            client = self._client([], [], FilesystemKnowledgeReader(vault))

            response = client.get("/api/knowledge/kp%3Afixture%3Aprovenance")
        finally:
            markdown.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["knowledge_id"], "kp:fixture:provenance")
        self.assertIn("# Provenance Sample", payload["markdown"])
        self.assertEqual(payload["source_urls"], ["https://arxiv.org/abs/2608.00001v1"])
        self.assertEqual(payload["anchors"][0]["section"], "Results")
        self.assertIsNone(payload["anchors"][0]["page_start"])
        self.assertEqual(payload["evidence_model"], "legacy_v2")
        self.assertEqual(payload["reading_sections"], [])

    def test_detail_returns_section_to_durable_anchor_mapping_for_v3(self) -> None:
        anchor = _hit(1).source_anchors[0]
        detail = KnowledgeDetail(
            knowledge_id="kp:api:section-aware",
            knowledge_version="2026-08-23T00:00:00Z",
            title="Section-aware paper",
            domain="test",
            evidence_level="full_text_text",
            source_urls=(anchor.source_url,),
            markdown="# Section-aware paper\n\n## 实验与结果\n\nEvidence 1\n",
            provenance_status="complete",
            anchors=(anchor,),
            reading_mode="deep_reading",
            evidence_model="section_anchors",
            reading_sections=(ReadingSectionEvidence("experiments", (anchor.anchor_id,)),),
        )

        class DetailReader:
            def recent(self, *, limit: int):
                return ()

            def get_current(self, knowledge_id: str):
                return detail if knowledge_id == detail.knowledge_id else None

        response = self._client([], [], DetailReader()).get("/api/knowledge/kp%3Aapi%3Asection-aware")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["reading_sections"],
            [{"section_name": "experiments", "anchor_ids": [anchor.anchor_id]}],
        )

    def test_detail_not_found_does_not_expose_a_file_path(self) -> None:
        response = self._client([], []).get("/api/knowledge/kp%3Amissing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "knowledge not found"})
        self.assertNotIn(str(ROOT), response.text)

    def test_chat_returns_citations_when_evidence_is_sufficient(self) -> None:
        client = self._client([_hit(1), _hit(2)], [])

        response = client.post("/api/chat", json={"query": "What is known?", "domain": "test"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(len(payload["citations"]), 2)
        self.assertEqual(payload["citations"][0]["anchor_id"], "anchor:1")
        self.assertEqual(payload["citations"][0]["claim_id"], "claim:1")
        self.assertEqual(payload["citations"][0]["source_anchors"][0]["anchor_id"], "source:1")

    def test_chat_pause_can_resume_after_user_approval(self) -> None:
        client = self._client([], [_hit(1), _hit(2)])

        paused = client.post("/api/chat", json={"query": "Need more knowledge"}).json()
        completed = client.post(f"/api/chat/{paused['thread_id']}/resume", json={"approved": True}).json()

        self.assertEqual(paused["status"], "needs_confirmation")
        self.assertEqual(completed["status"], "completed")
        self.assertIn("Evidence 1", completed["answer"])


class TopicApiTests(TestCase):
    def _client(self, runner: _Runner | None = None):
        repository = _MemoryRepository()
        service = TopicRunService(
            repository,
            runner=runner,
            topic_id_factory=lambda: f"topic-{len(repository.topics) + 1}",
            run_id_factory=lambda: f"run-{len(repository.runs) + 1}",
        )
        rag = _Rag([])
        graph = build_interactive_graph(
            InteractiveGraphDependencies(rag, _Supplementer(rag, []), CitationOnlyAnswerGenerator()),
            checkpointer=MemorySaver(),
        )
        client = TestClient(
            create_app(
                interactive_graph=graph,
                knowledge_reader=FilesystemKnowledgeReader(ROOT / "knowledge"),
                topic_service=service,
                scheduler_status=lambda: SchedulerStatus(
                    True,
                    "Asia/Shanghai",
                    "08:00",
                    datetime(2026, 8, 23, tzinfo=UTC),
                ),
            )
        )
        return client, service

    def test_create_returns_202_then_list_exposes_persisted_topic_and_latest_run(self) -> None:
        client, _ = self._client(_Runner())

        response = client.post(
            "/api/research-topics",
            json={"name": "Agent 记忆", "query": "LLM agent memory"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["topic"]["domain"], "topic:topic-1")
        self.assertEqual(response.json()["run"]["limit"], 3)
        self.assertTrue(response.json()["topic"]["enabled"])
        self.assertEqual(response.json()["topic"]["daily_limit"], 3)
        self.assertEqual(response.json()["run"]["trigger"], RunTrigger.INITIAL)
        listed = client.get("/api/research-topics").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["topic"]["name"], "Agent 记忆")
        self.assertEqual(listed[0]["latest_run"]["status"], "completed")

    def test_updates_subscription_and_exposes_safe_scheduler_status(self) -> None:
        client, service = self._client(_Runner())
        topic, run = service.create_topic(name="Agent 记忆", query="LLM agent memory")
        service.execute(run.run_id)

        updated = client.patch(
            f"/api/research-topics/{topic.topic_id}",
            json={"enabled": False, "daily_limit": 2},
        )
        scheduler = client.get("/api/scheduler")

        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.json()["enabled"])
        self.assertEqual(updated.json()["daily_limit"], 2)
        self.assertEqual(scheduler.status_code, 200)
        self.assertEqual(scheduler.json()["next_run_at"], "2026-08-23T00:00:00Z")
        self.assertNotIn("key", scheduler.text.lower())
        self.assertNotIn("database", scheduler.text.lower())

    def test_update_topic_rejects_empty_or_invalid_patch_and_missing_topic(self) -> None:
        client, _ = self._client(_Runner())

        self.assertEqual(client.patch("/api/research-topics/missing", json={"enabled": False}).status_code, 404)
        self.assertEqual(client.patch("/api/research-topics/missing", json={}).status_code, 422)
        self.assertEqual(client.patch("/api/research-topics/missing", json={"daily_limit": 4}).status_code, 422)

    def test_invalid_input_creates_neither_topic_nor_run(self) -> None:
        client, _ = self._client(_Runner())

        response = client.post("/api/research-topics", json={"name": " ", "query": "memory"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(client.get("/api/research-topics").json(), {"items": []})

    def test_active_run_returns_409_and_terminal_run_can_retry(self) -> None:
        client, service = self._client(_Runner())
        topic, active = service.create_topic(name="Agent 记忆", query="LLM agent memory")

        conflict = client.post(f"/api/research-topics/{topic.topic_id}/runs")

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["run_id"], active.run_id)
        service.execute(active.run_id)
        retried = client.post(f"/api/research-topics/{topic.topic_id}/runs")
        self.assertEqual(retried.status_code, 202)
        self.assertNotEqual(retried.json()["run_id"], active.run_id)

    def test_missing_resources_return_404(self) -> None:
        client, _ = self._client(_Runner())

        self.assertEqual(client.get("/api/production-runs/missing").status_code, 404)
        self.assertEqual(client.post("/api/research-topics/missing/runs").status_code, 404)

    def test_missing_deepseek_is_safe_terminal_run(self) -> None:
        client, _ = self._client(None)

        created = client.post(
            "/api/research-topics",
            json={"name": "Agent 记忆", "query": "LLM agent memory"},
        ).json()
        result = client.get(f"/api/production-runs/{created['run']['run_id']}")

        self.assertEqual(result.status_code, 200)
        payload = result.json()
        self.assertEqual(payload["status"], RunStatus.FAILED)
        self.assertEqual(payload["error_code"], "deepseek_not_configured")
        self.assertNotIn("key", result.text.lower())
        self.assertNotIn(str(ROOT).lower(), result.text.lower())
