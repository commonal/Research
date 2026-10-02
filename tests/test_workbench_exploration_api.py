from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.models import ExplorationStatus
from research_pulse.workbench.run_events import AttemptEventStore, UnsequencedEvent
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.tool_execution import (
    Retryability, SideEffectState, ToolErrorCode, ToolOutcome, ToolOutcomeStatus,
)


class _Runtime:
    def cancel(self, run_id): pass


class _Knowledge:
    def recent(self, *, limit: int): return ()
    def get_current(self, knowledge_id: str): return None


class WorkbenchExplorationApiTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            session_id="session-1", research_question="原问题", title="研究",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        ))
        self.service = ExplorationService(self.repository, _Runtime())
        self.client = TestClient(create_app(knowledge_reader=_Knowledge(), workbench_exploration_service=self.service))

    def tearDown(self) -> None:
        self.connection.close()

    def test_payload_projects_tools_used_and_token_usage(self) -> None:
        run = self.service.create("session-1", "如何治理长期记忆？", config_snapshot={}, budgets={})
        attempt = self.service.coordinator.inspect(run.run_id).current_attempt
        event_store = AttemptEventStore(self.repository)
        event_store.append(attempt.attempt_id, attempt.generation, UnsequencedEvent(
            "tool_started", "调用只读工具 search_sources", {"stable_ids": {"tool_name": "search_sources"}}))
        event_store.append(attempt.attempt_id, attempt.generation, UnsequencedEvent(
            "tool_completed", "只读工具 search_sources 已完成", {
                "stable_ids": {"tool_name": "search_sources"},
                "counters": {"input_tokens": 100, "output_tokens": 50},
            }))
        event_store.append(attempt.attempt_id, attempt.generation, UnsequencedEvent(
            "tool_started", "调用只读工具 read_managed_blocks", {
                "stable_ids": {"tool_name": "read_managed_blocks"},
                "counters": {"input_tokens": 150, "output_tokens": 75},
                "extras": ({
                    "kind": "web_search_usage",
                    "model": "deepseek-v4-flash",
                    "response_id": "resp-1",
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "total_tokens": 25,
                },),
            }))
        event_store.append(attempt.attempt_id, attempt.generation, UnsequencedEvent(
            "context_read", "读取受管证据块", {
            "stable_ids": {
                    "source_id": "paper-1",
                    "block_id": "normalized:paper-1:text:block-1",
                },
                "counters": {"input_tokens": 150, "output_tokens": 75},
            }))

        response = self.client.get(f"/api/workbench/explorations/{run.run_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["tools_used"], [
            {"tool": "read_managed_blocks", "calls": 1},
            {"tool": "search_sources", "calls": 1},
        ])
        # Counters are cumulative per event; the final total is the LAST
        # event's snapshot (150/75), not the sum across events.
        self.assertEqual(payload["token_usage"], {"input_tokens": 150, "output_tokens": 75})
        self.assertEqual(payload["web_search_usage"], {
            "calls": 1,
            "input_tokens": 20,
            "output_tokens": 5,
            "total_tokens": 25,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        })
        self.assertEqual(payload["sources"], [{
            "source_id": "paper-1",
            "title": "已读论文证据",
            "kind": "managed",
            "url": "",
            "relevance": "",
        }])
        self.assertEqual(payload["continuation_mode"], "persisted_results")
        self.assertEqual(payload["continuation_label"], "基于已有结果继续执行")
        self.assertNotIn("checkpoint", payload["continuation_label"].lower())
        self.assertEqual([event["event_type"] for event in payload["events"]], [
            "tool_started", "tool_completed", "tool_started", "context_read",
        ])
        self.assertEqual(payload["events"][0]["summary"], "调用只读工具 search_sources")
        self.assertEqual(payload["events"][0]["counters"], {})
        self.assertEqual(payload["events"][0]["attempt_id"], attempt.attempt_id)
        self.assertIsNotNone(payload["events"][0]["occurred_at"])
        event_keys = {
            (event["attempt_id"], event["sequence_no"])
            for event in payload["events"]
        }
        self.assertEqual(len(event_keys), len(payload["events"]))

    def test_payload_without_tool_events_uses_empty_defaults(self) -> None:
        run = self.service.create("session-1", "mock question", config_snapshot={}, budgets={})
        attempt = self.service.coordinator.inspect(run.run_id).current_attempt
        AttemptEventStore(self.repository).append(
            attempt.attempt_id,
            attempt.generation,
            UnsequencedEvent("final_draft", "探索草稿已生成", {"stable_ids": {"run_id": run.run_id}}),
        )

        payload = self.client.get(f"/api/workbench/explorations/{run.run_id}").json()
        self.assertEqual(payload["tools_used"], [])
        self.assertEqual(payload["token_usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_legacy_completed_run_without_draft_is_projected_as_visible_failure(self) -> None:
        run = self.service.create("session-1", "legacy empty result", config_snapshot={}, budgets={})
        lease = self.repository.claim_next_attempt(
            "worker", now=datetime.now(UTC),
            lease_duration=timedelta(seconds=30),
        )
        self.service.coordinator.record_attempt_outcome(
            lease.attempt_id,
            AttemptStatus.COMPLETED,
            expected_generation=lease.generation,
            budget_used={},
            final_draft=None,
        )

        payload = self.client.get(f"/api/workbench/explorations/{run.run_id}").json()

        self.assertEqual("failed", payload["status"])
        self.assertEqual("未生成可显示正文", payload["phase"])
        self.assertEqual("terminal_failure", payload["failure"]["category"])
        self.assertEqual("inspect_diagnostic", payload["failure"]["recommended_action"])

    def test_awaiting_user_checkpoint_does_not_project_stale_stop_as_failure(self) -> None:
        run = self.service.create(
            "session-1", "awaiting decision", config_snapshot={}, budgets={}
        )
        lease = self.repository.claim_next_attempt(
            "worker", now=datetime.now(UTC), lease_duration=timedelta(seconds=30)
        )
        self.service.coordinator.record_attempt_outcome(
            lease.attempt_id,
            AttemptStatus.AWAITING_USER,
            expected_generation=lease.generation,
            safe_error="agent attempt stopped",  # legacy projection to mask
            final_draft="草稿\n\n请选择研究方向。",
        )

        payload = self.client.get(f"/api/workbench/explorations/{run.run_id}").json()

        self.assertEqual("awaiting_user_decision", payload["status"])
        self.assertIsNone(payload["failure"])
        self.assertIsNone(payload["current_attempt"]["failure"])
        self.assertIsNone(payload["safe_error"])

    def test_checkpoint_event_projects_decision_id_for_clients(self) -> None:
        run = self.service.create(
            "session-1", "checkpoint identity", config_snapshot={}, budgets={}
        )
        attempt = self.service.coordinator.inspect(run.run_id).current_attempt
        AttemptEventStore(self.repository).append(
            attempt.attempt_id,
            attempt.generation,
            UnsequencedEvent(
                "awaiting_decision",
                "已形成初始理解；等待用户选择研究方向",
                {"stable_ids": {"run_id": run.run_id, "decision_id": "D-42"}},
            ),
        )

        payload = self.client.get(f"/api/workbench/explorations/{run.run_id}").json()

        self.assertEqual("D-42", payload["decision_id"])
        self.assertEqual("D-42", payload["events"][0]["stable_ids"]["decision_id"])

    def test_historical_event_sequence_failure_is_not_offered_as_continue(self) -> None:
        run = self.service.create(
            "session-1", "historical sequence failure", config_snapshot={}, budgets={}
        )
        attempt = self.service.coordinator.inspect(run.run_id).current_attempt
        with self.connection:
            self.connection.execute(
                "UPDATE exploration_attempts SET status = 'failed', "
                "safe_error = ? WHERE attempt_id = ?",
                (
                    "ValueError: exploration event sequence must be contiguous and append-only",
                    attempt.attempt_id,
                ),
            )
            self.connection.execute(
                "UPDATE exploration_runs SET status = 'failed', safe_error = ? WHERE run_id = ?",
                (
                    "ValueError: exploration event sequence must be contiguous and append-only",
                    run.run_id,
                ),
            )

        payload = self.client.get(f"/api/workbench/explorations/{run.run_id}").json()

        self.assertEqual("legacy_failure", payload["failure"]["category"])
        self.assertEqual(
            "旧版探索记录的事件序列不完整，无法继续；请重新发起问题。",
            payload["failure"]["safe_message"],
        )
        self.assertEqual(payload["failure"]["safe_message"], payload["safe_error"])
        self.assertEqual("inspect_diagnostic", payload["failure"]["recommended_action"])
        self.assertEqual("legacy_failure", payload["current_attempt"]["failure"]["category"])

    def test_create_continue_and_cancel_require_attempt_identity_and_idempotency(self) -> None:
        created = self.client.post(
            "/api/workbench/sessions/session-1/explorations",
            json={
                "question": "如何增强恢复鲁棒性？",
                "config_snapshot": {"profile": "literature"},
                "budgets": {
                    "model_rounds": 2, "tool_calls": 4, "block_reads": 4,
                    "wall_seconds": 30, "input_tokens": 1000,
                    "output_tokens": 500,
                },
            },
        )
        self.assertEqual(202, created.status_code)
        run_id = created.json()["run_id"]
        first_attempt = created.json()["current_attempt_id"]

        lease = self.repository.claim_next_attempt(
            "worker", now=datetime.now(UTC),
            lease_duration=timedelta(seconds=30),
        )
        self.repository.save_tool_outcome(ToolOutcome(
            tool_call_id="call-secret-failure",
            attempt_id=lease.attempt_id,
            tool_name="search_sources",
            status=ToolOutcomeStatus.REJECTED,
            error_code=ToolErrorCode.INVALID_ARGUMENT,
            retryability=Retryability.AFTER_CORRECTION,
            side_effect_state=SideEffectState.NONE,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            safe_message="工具参数无效，请修正后重试",
            diagnostic_id="diag-1",
        ))
        self.service.coordinator.record_attempt_outcome(
            lease.attempt_id, AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=lease.generation,
            safe_error="工具预算已耗尽",
            budget_used={"tool_calls": 4, "recovery_attempts": 1},
        )
        exhausted = self.client.get(f"/api/workbench/explorations/{run_id}").json()
        self.assertEqual(first_attempt, exhausted["current_attempt"]["attempt_id"])
        self.assertEqual(1, len(exhausted["attempt_history"]))
        self.assertEqual({"tool_calls": 4, "recovery_attempts": 1}, exhausted["budget"]["current"])
        self.assertEqual(exhausted["budget"]["current"], exhausted["budget"]["cumulative"])
        self.assertEqual("budget_exhausted", exhausted["current_attempt"]["status"])
        self.assertEqual("budget_exhausted", exhausted["failure"]["category"])
        self.assertEqual("continue", exhausted["failure"]["recommended_action"])
        detail = exhausted["tool_details"][0]
        self.assertEqual("invalid_argument", detail["error_code"])
        self.assertEqual("after_correction", detail["retryability"])
        self.assertEqual("none", detail["side_effect_state"])
        self.assertEqual("diag-1", detail["diagnostic_id"])
        self.assertEqual("correct_parameters", detail["recommended_action"])
        serialized = str(exhausted["tool_details"]).lower()
        for forbidden in ("traceback", "api_key", "hidden prompt", "full paper"):
            self.assertNotIn(forbidden, serialized)
        missing_body = self.client.post(f"/api/workbench/explorations/{run_id}/continue")
        self.assertEqual(422, missing_body.status_code)

        request = {
            "expected_attempt_id": first_attempt,
            "idempotency_key": "continue-click-1",
        }
        continued = self.client.post(
            f"/api/workbench/explorations/{run_id}/continue", json=request
        )
        duplicate = self.client.post(
            f"/api/workbench/explorations/{run_id}/continue", json=request
        )
        self.assertEqual(202, continued.status_code)
        self.assertEqual(
            continued.json()["current_attempt_id"],
            duplicate.json()["current_attempt_id"],
        )

        stale = self.client.post(
            f"/api/workbench/explorations/{run_id}/cancel",
            json={
                "expected_attempt_id": first_attempt,
                "idempotency_key": "stale-cancel",
            },
        )
        self.assertEqual(409, stale.status_code)

        second_attempt = continued.json()["current_attempt_id"]
        cancel_request = {
            "expected_attempt_id": second_attempt,
            "idempotency_key": "cancel-click-1",
        }
        cancelled = self.client.post(
            f"/api/workbench/explorations/{run_id}/cancel", json=cancel_request
        )
        duplicate_cancel = self.client.post(
            f"/api/workbench/explorations/{run_id}/cancel", json=cancel_request
        )
        self.assertEqual(200, cancelled.status_code)
        self.assertEqual(cancelled.json()["status"], duplicate_cancel.json()["status"])

    def test_repeated_budget_exhaustion_is_projected_as_scope_change(self) -> None:
        run = self.service.create(
            "session-1", "反复预算耗尽", config_snapshot={}, budgets={}
        )
        first_lease = self.repository.claim_next_attempt(
            "worker", now=datetime.now(UTC), lease_duration=timedelta(seconds=30)
        )
        self.service.coordinator.record_attempt_outcome(
            first_lease.attempt_id,
            AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=first_lease.generation,
        )
        continued = self.service.coordinator.continue_run(
            run.run_id,
            expected_attempt_id=first_lease.attempt_id,
            idempotency_key="scope-continue-1",
        )
        second_lease = self.repository.claim_next_attempt(
            "worker", now=datetime.now(UTC), lease_duration=timedelta(seconds=30)
        )
        self.service.coordinator.record_attempt_outcome(
            second_lease.attempt_id,
            AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=second_lease.generation,
        )

        payload = self.client.get(f"/api/workbench/explorations/{run.run_id}").json()

        self.assertEqual(continued.current_attempt.attempt_id, second_lease.attempt_id)
        self.assertEqual("budget_exhausted_stalled", payload["failure"]["category"])
        self.assertEqual("narrow_scope", payload["failure"]["recommended_action"])
