from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.scope_resolver import InteractionContext
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.turn_runtime import CapabilityDecision, TurnResult


class _KnowledgeReader:
    def recent(self, *, limit: int):
        return ()

    def get_current(self, knowledge_id: str):
        return None


class _Runtime:
    def __init__(self) -> None:
        self.requests = []

    def start(self, request):
        self.requests.append(request)
        decision = CapabilityDecision(
            capability="paper",
            retrieval_plan="paper_local",
            execution_mode="sync",
            allowed_tools=("read_managed_blocks",),
            evidence_scope="current_paper",
            reason="选区动作",
        )
        return TurnResult(
            turn_id="turn-api-1",
            assistant_message="已解释",
            capability_decision=decision,
        )


class _DurableRuntime:
    def start(self, request):
        decision = CapabilityDecision(
            capability="web",
            retrieval_plan="web_lookup",
            execution_mode="durable",
            allowed_tools=("search_web",),
            evidence_scope="web_sources",
            reason="用户显式动作指定 web 能力",
        )
        return TurnResult(
            turn_id="turn-api-durable",
            assistant_message=None,
            capability_decision=decision,
            status="queued",
            durable_handle="run-web-1",
        )


class _BackgroundRuntime:
    def start(self, request):
        decision = CapabilityDecision(
            capability="paper",
            retrieval_plan="paper_local",
            execution_mode="background",
            allowed_tools=("read_managed_blocks",),
            evidence_scope="current_paper",
            reason="选区回答",
        )
        return TurnResult(
            turn_id="turn-api-background",
            assistant_message=None,
            capability_decision=decision,
            status="queued",
            durable_handle="assistant-message-1",
        )


class UnifiedTurnApiTests(TestCase):
    @staticmethod
    def _client(runtime):
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        repository = SQLiteWorkbenchRepository(connection)
        sessions = __import__("research_pulse.workbench.sessions", fromlist=["SessionService"]).SessionService(
            repository,
            session_id_factory=lambda: "session-api",
            clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
        )
        sessions.create_empty()
        class _UnusedChat:
            def list(self, session_id):
                return ()

            def send(self, *args, **kwargs):
                raise AssertionError("legacy chat path must not run")

        return TestClient(create_app(
            knowledge_reader=_KnowledgeReader(),
            workbench_session_service=sessions,
            workbench_chat_service=_UnusedChat(),
            workbench_turn_runtime=runtime,
        )), connection

    def test_api_translates_legacy_selection_fields_to_turn_request(self) -> None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        repository = SQLiteWorkbenchRepository(connection)
        sessions = __import__("research_pulse.workbench.sessions", fromlist=["SessionService"]).SessionService(
            repository,
            session_id_factory=lambda: "session-api",
            clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
        )
        sessions.create_empty()
        runtime = _Runtime()
        # Chat service is required to mount the messages router; it is never
        # called when the unified runtime is injected.
        class _UnusedChat:
            def list(self, session_id):
                return ()

            def send(self, *args, **kwargs):
                raise AssertionError("legacy chat path must not run")

        client = TestClient(create_app(
            knowledge_reader=_KnowledgeReader(),
            workbench_session_service=sessions,
            workbench_chat_service=_UnusedChat(),
            workbench_turn_runtime=runtime,
        ))
        response = client.post(
            "/api/workbench/sessions/session-api/messages",
            json={
                "query": "解释当前选区",
                "scope": "selection",
                "paper_id": "paper-1",
                "block_id": "block-1",
                "selected_text": "selected text",
                "action": "explain",
                "client_request_id": "req-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "turn")
        request = runtime.requests[0]
        self.assertEqual(request.explicit_action, "explain")
        self.assertEqual(request.interaction_context.selection.paper_id, "paper-1")
        self.assertEqual(request.interaction_context.selection.text, "selected text")
        connection.close()

    def test_api_returns_202_and_durable_handle_for_long_turn(self) -> None:
        client, connection = self._client(_DurableRuntime())
        response = client.post(
            "/api/workbench/sessions/session-api/messages",
            json={"query": "查网页上的最新进展", "action": "web", "client_request_id": "req-web-1"},
        )
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["kind"], "turn")
        self.assertEqual(payload["result"]["status"], "queued")
        self.assertEqual(payload["result"]["durable_handle"], "run-web-1")
        connection.close()

    def test_api_returns_202_for_background_paper_turn(self) -> None:
        client, connection = self._client(_BackgroundRuntime())
        response = client.post(
            "/api/workbench/sessions/session-api/messages",
            json={
                "query": "请概括当前选区",
                "scope": "selection",
                "paper_id": "paper-1",
                "block_id": "block-1",
                "selected_text": "selected text",
                "action": "ask_selection",
                "client_request_id": "req-paper-1",
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["status"], "queued")
        self.assertEqual(response.json()["result"]["durable_handle"], "assistant-message-1")
        connection.close()
