from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.agent_kernel import ScriptedAgentKernel
from research_pulse.workbench.agent_runtime import ToolCapability
from research_pulse.workbench.attempt_worker import AttemptWorker
from research_pulse.workbench.cancellation import CancellationRegistry
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.run_coordinator import ResearchRunCoordinator
from research_pulse.workbench.run_models import AttemptOutcome, AttemptStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class _Knowledge:
    def recent(self, *, limit: int): return ()
    def get_current(self, knowledge_id: str): return None


class _Runtime:
    def cancel(self, run_id: str) -> None: pass


class WorkbenchAttemptWorkerTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.repository.create(ResearchSession(
            session_id="session-1", research_question="worker", title="worker",
            created_at=datetime.now(UTC),
        ))

    def tearDown(self) -> None:
        self.connection.close()

    def test_http_returns_before_lifespan_worker_finishes_attempt(self) -> None:
        entered_kernel = Event()
        allow_finish = Event()

        def execute(context, sink, cancellation):
            entered_kernel.set()
            self.assertTrue(allow_finish.wait(2))
            return AttemptOutcome(context.attempt_id, AttemptStatus.COMPLETED)

        cancellation = CancellationRegistry()
        coordinator = ResearchRunCoordinator(
            self.repository, cancellation_registry=cancellation
        )
        worker = AttemptWorker(
            self.repository,
            coordinator,
            ScriptedAgentKernel(
                capabilities_by_profile={"literature": (ToolCapability("search", True),)},
                execute_script=execute,
            ),
            cancellation_registry=cancellation,
            poll_interval=0.01,
        )
        service = ExplorationService(self.repository, _Runtime())
        run = service.create(
            "session-1", "继续研究",
            config_snapshot={"profile": "literature"},
            budgets={
                "model_rounds": 2, "tool_calls": 2, "block_reads": 2,
                "wall_seconds": 2, "input_tokens": 20, "output_tokens": 20,
            },
        )
        first = self.repository.claim_next_attempt(
            "setup", now=datetime.now(UTC), lease_duration=timedelta(seconds=5)
        )
        coordinator.record_attempt_outcome(
            first.attempt_id, AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=first.generation,
        )
        app = create_app(
            knowledge_reader=_Knowledge(),
            workbench_session_service=None,
            workbench_exploration_service=service,
            workbench_attempt_worker=worker,
        )

        with TestClient(app) as client:
            response = client.post(
                f"/api/workbench/explorations/{run.run_id}/retry",
            )
            self.assertEqual(202, response.status_code)
            self.assertTrue(entered_kernel.wait(2))
            snapshot = coordinator.inspect(run.run_id)
            self.assertEqual(AttemptStatus.RUNNING, snapshot.status)
            allow_finish.set()
            self.assertTrue(worker.wait_until_idle(timeout=2))
            self.assertEqual(AttemptStatus.COMPLETED, coordinator.inspect(run.run_id).status)

    def test_completed_attempt_persists_answer_body_on_the_logical_run(self) -> None:
        coordinator = ResearchRunCoordinator(self.repository)
        worker = AttemptWorker(
            self.repository,
            coordinator,
            ScriptedAgentKernel(
                capabilities_by_profile={"literature": ()},
                execute_script=lambda context, sink, cancellation: AttemptOutcome(
                    context.attempt_id,
                    AttemptStatus.COMPLETED,
                    final_draft="你好，我是 Research Pulse。",
                ),
            ),
        )
        service = ExplorationService(self.repository, _Runtime())
        run = service.create(
            "session-1",
            "你好",
            config_snapshot={"profile": "literature"},
            budgets={
                "model_rounds": 2, "tool_calls": 2, "block_reads": 2,
                "wall_seconds": 2, "input_tokens": 20, "output_tokens": 20,
            },
        )

        self.assertTrue(worker.run_once())

        persisted = self.repository.get_exploration_run(run.run_id)
        self.assertIsNotNone(persisted)
        self.assertEqual("你好，我是 Research Pulse。", persisted.final_draft)
        self.assertEqual(AttemptStatus.COMPLETED, coordinator.inspect(run.run_id).status)
