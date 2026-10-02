from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.scope_resolver import InteractionContext
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    TurnAuditRecord,
)
from research_pulse.workbench.turn_events import UnsequencedTurnEvent, project_turn_events


class TurnAuditRepositoryTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            session_id="session-1",
            research_question=None,
            title="测试",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        ))

    def test_route_decision_round_trips_and_is_idempotent(self) -> None:
        decision = CapabilityDecision(
            capability="web",
            retrieval_plan="web_lookup",
            execution_mode="sync",
            allowed_tools=("search_web",),
            evidence_scope="web_sources",
            reason="用户明确要求网页检索",
        )
        record = TurnAuditRecord(
            turn_id="turn-1",
            session_id="session-1",
            decision=decision,
            context_snapshot=InteractionContext(surface="global_chat").to_dict(),
            client_request_id="client-1",
        )
        inserted = self.repository.create_or_get_turn_audit(record)
        repeated = self.repository.create_or_get_turn_audit(
            TurnAuditRecord(
                turn_id="turn-2",
                session_id="session-1",
                decision=decision,
                context_snapshot={},
                client_request_id="client-1",
            )
        )
        self.assertEqual(inserted.turn_id, "turn-1")
        self.assertEqual(repeated.turn_id, "turn-1")
        loaded = self.repository.get_turn_audit("turn-1")
        assert loaded is not None
        self.assertEqual(loaded.decision.capability, "web")
        self.assertEqual(loaded.context_snapshot["surface"], "global_chat")

    def test_save_turn_audit_updates_status_and_run_association_only(self) -> None:
        decision = CapabilityDecision(
            capability="research",
            retrieval_plan="research_exploration",
            execution_mode="durable",
            allowed_tools=("search_sources",),
            evidence_scope="research_workspace",
            reason="研究入口",
        )
        record = TurnAuditRecord(
            turn_id="turn-2",
            session_id="session-1",
            decision=decision,
        )
        self.repository.create_or_get_turn_audit(record)
        updated = TurnAuditRecord(
            turn_id="turn-2",
            session_id="session-1",
            decision=decision,
            status="running",
            run_id="run-1",
            attempt_id="attempt-1",
        )
        self.repository.save_turn_audit(updated)
        loaded = self.repository.get_turn_audit("turn-2")
        assert loaded is not None
        self.assertEqual(loaded.status, "running")
        self.assertEqual(loaded.run_id, "run-1")
        self.assertEqual(loaded.attempt_id, "attempt-1")
        self.assertEqual(loaded.decision.reason, "研究入口")

    def test_recover_interrupted_background_turns_closes_only_in_process_answers(self) -> None:
        background = CapabilityDecision(
            capability="paper",
            retrieval_plan="paper_local",
            execution_mode="background",
            allowed_tools=("read_paper_metadata",),
            evidence_scope="current_paper",
            reason="选区回答",
        )
        durable = CapabilityDecision(
            capability="research",
            retrieval_plan="research_exploration",
            execution_mode="durable",
            allowed_tools=("search_sources",),
            evidence_scope="research_workspace",
            reason="研究入口",
        )
        self.repository.create_or_get_turn_audit(TurnAuditRecord(
            turn_id="turn-background-recovery",
            session_id="session-1",
            decision=background,
            status="running",
        ))
        self.repository.create_or_get_turn_audit(TurnAuditRecord(
            turn_id="turn-durable-recovery",
            session_id="session-1",
            decision=durable,
            status="running",
        ))

        self.assertEqual(self.repository.recover_interrupted_background_turns(), 1)
        assert self.repository.get_turn_audit("turn-background-recovery") is not None
        assert self.repository.get_turn_audit("turn-durable-recovery") is not None
        self.assertEqual(
            self.repository.get_turn_audit("turn-background-recovery").status,
            "failed",
        )
        self.assertEqual(
            self.repository.get_turn_audit("turn-durable-recovery").status,
            "running",
        )

    def test_turn_events_are_safe_and_persisted_with_store_assigned_order(self) -> None:
        decision = CapabilityDecision(
            capability="basic", retrieval_plan="direct", execution_mode="sync",
            allowed_tools=(), evidence_scope="none", reason="兜底",
        )
        self.repository.create_or_get_turn_audit(TurnAuditRecord(
            turn_id="turn-events", session_id="session-1", decision=decision,
        ))
        first = self.repository.append_turn_event(
            "turn-events", UnsequencedTurnEvent("route_decided", "已路由")
        )
        second = self.repository.append_turn_event(
            "turn-events", UnsequencedTurnEvent("turn_completed", "已完成")
        )
        self.assertEqual((first.sequence_no, second.sequence_no), (1, 2))
        listed = self.repository.list_turn_events("turn-events")
        self.assertEqual([item.summary for item in listed], ["已路由", "已完成"])
        projected = project_turn_events((
            UnsequencedTurnEvent("turn_started", "开始"),
            UnsequencedTurnEvent("turn_failed", "失败"),
        ))
        self.assertEqual([item.sequence_no for item in projected], [1, 2])
