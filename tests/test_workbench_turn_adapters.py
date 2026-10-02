from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.capability_router import CapabilityRouter
from research_pulse.workbench.scope_resolver import InteractionContext, SelectionAnchor
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.turn_adapters import (
    BackgroundTurnAdapter,
    DurableTurnAdapter,
    SynchronousTurnAdapter,
)
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    TurnRequest,
    default_capability_registry,
)
from research_pulse.workbench.turn_service import TurnRuntime


class _ChatMessage:
    text = "解释结果 [normalized:paper-1:text:block-1]"
    message_id = "assistant-message-1"
    citations = ()


class _ChatService:
    def __init__(self) -> None:
        self.calls = []

    def send(self, session_id: str, **kwargs):
        self.calls.append((session_id, kwargs))
        return _ChatMessage()


class _Adapter:
    def __init__(self, status: str, handle: str | None = None) -> None:
        self.status = status
        self.handle = handle
        self.calls = 0

    def execute(self, request, decision):
        self.calls += 1
        from research_pulse.workbench.turn_runtime import TurnResult
        return TurnResult(
            turn_id=f"turn-{self.calls}",
            assistant_message="完成" if self.status == "completed" else None,
            capability_decision=decision,
            status=self.status,
            durable_handle=self.handle,
        )


class TurnAdapterTests(TestCase):
    def test_durable_research_adapter_uses_assistant_budget(self) -> None:
        """The production research route must not fall back to the V0 budget."""

        class _Exploration:
            def __init__(self) -> None:
                self.calls = []

            def create(self, session_id, question, *, config_snapshot, budgets):
                self.calls.append({
                    "session_id": session_id,
                    "question": question,
                    "config_snapshot": config_snapshot,
                    "budgets": budgets,
                })
                return type("Run", (), {"run_id": "run-research"})()

        exploration = _Exploration()
        adapter = DurableTurnAdapter(
            exploration,
            default_capability_registry(),
            turn_id_factory=lambda: "turn-research",
        )
        request = TurnRequest(
            session_id="session-1",
            message="研究这篇论文的局限并判断哪些方向值得继续验证",
            interaction_context=InteractionContext(
                surface="research_workspace",
                research_question_id="rq-1",
            ),
        )
        decision = CapabilityDecision(
            capability="research",
            retrieval_plan="research_exploration",
            execution_mode="durable",
            allowed_tools=("read_workspace_state",),
            evidence_scope="research_workspace",
            reason="研究探索",
        )

        result = adapter.execute(request, decision)

        self.assertEqual(result.status, "queued")
        self.assertEqual(exploration.calls[0]["budgets"], {
            "model_rounds": 24,
            "tool_calls": 64,
            "block_reads": 96,
            "wall_seconds": 600,
            "input_tokens": 200000,
            "output_tokens": 40000,
        })

    def test_background_paper_adapter_returns_before_slow_model_finishes(self) -> None:
        """A paper turn must expose a queued state before provider latency elapses."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from research_pulse.workbench.turn_runtime import TurnResult

        started = Event()
        release = Event()

        class _Prepared:
            assistant = type("Assistant", (), {"message_id": "assistant-queued"})()

        class _SlowChatService:
            def prepare(self, session_id: str, **kwargs):
                return _Prepared()

            def complete_prepared(self, prepared):
                started.set()
                release.wait(timeout=2)
                return _ChatMessage()

        adapter = BackgroundTurnAdapter(
            _SlowChatService(),
            executor=ThreadPoolExecutor(max_workers=1),
            turn_id_factory=lambda: "turn-background",
        )
        request = TurnRequest(
            session_id="session-1",
            message="解释当前选区",
            interaction_context=InteractionContext(
                surface="paper_reader",
                selection=SelectionAnchor(
                    paper_id="paper-1", block_id="block-1", text="selected text", page=2,
                ),
            ),
        )
        decision = CapabilityDecision(
            capability="paper",
            retrieval_plan="paper_local",
            execution_mode="background",
            allowed_tools=("read_managed_blocks",),
            evidence_scope="current_paper",
            reason="选区动作",
        )

        result = adapter.execute(request, decision)

        self.assertEqual(result.status, "queued")
        self.assertEqual(result.durable_handle, "assistant-queued")
        self.assertTrue(started.wait(timeout=1))
        release.set()

    def test_background_submission_failure_closes_prepared_message(self) -> None:
        class _Prepared:
            assistant = type("Assistant", (), {"message_id": "assistant-submit-failed"})()

        class _Chat:
            def __init__(self) -> None:
                self.failed = []

            def prepare(self, session_id: str, **kwargs):
                return _Prepared()

            def fail_prepared(self, prepared):
                self.failed.append(prepared.assistant.message_id)

        class _RejectedExecutor:
            def submit(self, fn, prepared):
                raise RuntimeError("pool closed")

        chat = _Chat()
        adapter = BackgroundTurnAdapter(chat, executor=_RejectedExecutor())
        request = TurnRequest(session_id="session-1", message="解释当前选区")
        decision = CapabilityDecision(
            capability="paper",
            retrieval_plan="paper_local",
            execution_mode="background",
            allowed_tools=("read_managed_blocks",),
            evidence_scope="current_paper",
            reason="选区动作",
        )

        with self.assertRaises(RuntimeError):
            adapter.execute(request, decision)
        self.assertEqual(chat.failed, ["assistant-submit-failed"])

    def test_sync_adapter_maps_selection_to_existing_chat_scope(self) -> None:
        chat = _ChatService()
        adapter = SynchronousTurnAdapter(chat, turn_id_factory=lambda: "turn-1")
        request = TurnRequest(
            session_id="session-1",
            message="解释当前选区",
            interaction_context=InteractionContext(
                surface="paper_reader",
                selection=SelectionAnchor(
                    paper_id="paper-1", block_id="block-1", text="selected text", page=2,
                ),
            ),
        )
        decision = CapabilityDecision(
            capability="paper",
            retrieval_plan="paper_local",
            execution_mode="sync",
            allowed_tools=("read_managed_blocks",),
            evidence_scope="current_paper",
            reason="选区动作",
        )
        result = adapter.execute(request, decision)
        self.assertEqual(result.turn_id, "turn-1")
        self.assertEqual(chat.calls[0][1]["scope"], "selection")
        self.assertEqual(chat.calls[0][1]["selected_text"], "selected text")

    def test_runtime_chooses_one_adapter_and_deduplicates_client_request(self) -> None:
        sync = _Adapter("completed")
        durable = _Adapter("queued", "run-1")
        runtime = TurnRuntime(
            router=CapabilityRouter(),
            synchronous=sync,
            durable=durable,
            turn_id_factory=iter(("turn-1", "turn-2")).__next__,
        )
        request = TurnRequest(
            session_id="session-1",
            message="什么是 RLHF？",
            client_request_id="client-1",
        )
        first = runtime.start(request)
        second = runtime.start(request)
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.turn_id, second.turn_id)
        self.assertEqual(sync.calls, 1)
        self.assertEqual(durable.calls, 0)

    def test_runtime_uses_durable_adapter_for_research_context(self) -> None:
        sync = _Adapter("completed")
        durable = _Adapter("queued", "run-2")
        runtime = TurnRuntime(synchronous=sync, durable=durable)
        result = runtime.start(TurnRequest(
            session_id="session-1",
            message="比较多篇论文的相关工作",
            interaction_context=InteractionContext(
                surface="research_workspace", research_question_id="rq-1"
            ),
        ))
        self.assertEqual(result.status, "queued")
        self.assertEqual(result.durable_handle, "run-2")
        self.assertEqual(sync.calls, 0)
        self.assertEqual(durable.calls, 1)

    def test_runtime_persists_audit_and_does_not_duplicate_across_repository_idempotency(self) -> None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        repository = SQLiteWorkbenchRepository(connection)
        repository.create(ResearchSession(
            session_id="session-1", research_question=None, title="测试",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        ))
        sync = _Adapter("completed")
        runtime = TurnRuntime(
            synchronous=sync,
            durable=_Adapter("queued", "run-3"),
            audit_repository=repository,
            turn_id_factory=lambda: "turn-persisted",
        )
        request = TurnRequest(
            session_id="session-1", message="普通回答", client_request_id="client-2"
        )
        runtime.start(request)
        runtime2 = TurnRuntime(
            synchronous=sync,
            durable=_Adapter("queued", "run-4"),
            audit_repository=repository,
            turn_id_factory=lambda: "turn-other",
        )
        repeated = runtime2.start(request)
        self.assertEqual(repeated.turn_id, "turn-persisted")
        self.assertEqual(sync.calls, 1)

    def test_runtime_projects_adapter_failure_as_safe_failed_turn(self) -> None:
        class _BrokenAdapter:
            def execute(self, request, decision):
                raise RuntimeError("provider secret-token and traceback")

        runtime = TurnRuntime(
            router=CapabilityRouter(),
            synchronous=_BrokenAdapter(),
            turn_id_factory=lambda: "turn-safe-failure",
        )
        result = runtime.start(TurnRequest(session_id="session-1", message="普通回答"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["code"], "turn_execution_failed")
        self.assertNotIn("secret-token", str(result.to_dict()))
        self.assertNotIn("traceback", str(result.to_dict()).lower())

    def test_runtime_rejects_paper_action_without_context_before_adapter(self) -> None:
        class _ShouldNotRun:
            def execute(self, request, decision):
                raise AssertionError("paper adapter must not run without context")

        runtime = TurnRuntime(
            router=CapabilityRouter(),
            synchronous=_ShouldNotRun(),
            turn_id_factory=lambda: "turn-paper-context",
        )
        result = runtime.start(TurnRequest(
            session_id="session-1", message="翻译这段", explicit_action="translate",
        ))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["code"], "paper_context_required")

    def test_runtime_reports_unconfigured_web_without_creating_a_durable_run(self) -> None:
        durable = _Adapter("queued", "must-not-be-created")
        runtime = TurnRuntime(
            router=CapabilityRouter(),
            durable=durable,
            available_capabilities={"basic", "paper", "research"},
            turn_id_factory=lambda: "turn-web-unavailable",
        )
        result = runtime.start(TurnRequest(
            session_id="session-1", message="查网页上的最新进展", explicit_action="web",
        ))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error["code"], "capability_unavailable")
        self.assertEqual(durable.calls, 0)
