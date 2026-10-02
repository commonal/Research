"""Public service seam for ExplorationRun lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol
from uuid import uuid4

from research_pulse.workbench.models import ExplorationRun
from research_pulse.workbench.run_coordinator import (
    CreateRunCommand,
    ResearchRunCoordinator,
    RunConflictError,
    RunNotFoundError,
)
from research_pulse.workbench.cancellation import CancellationRegistry


class ExplorationRepository(Protocol):
    def get(self, session_id: str): ...
    def get_exploration_run(self, run_id: str) -> ExplorationRun | None: ...
    def list_exploration_runs(self, session_id: str) -> tuple[ExplorationRun, ...]: ...


class ExplorationNotFoundError(LookupError):
    pass


class ExplorationRetryError(ValueError):
    pass


class ExplorationService:
    def __init__(
        self,
        repository: ExplorationRepository,
        runtime: object | None = None,
        *,
        run_id_factory: Callable[[], str] = lambda: str(uuid4()),
        attempt_id_factory: Callable[[], str] = lambda: str(uuid4()),
        cancellation_registry: CancellationRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.run_id_factory = run_id_factory
        self.coordinator = ResearchRunCoordinator(
            repository,
            run_id_factory=run_id_factory,
            attempt_id_factory=attempt_id_factory,
            cancellation_registry=cancellation_registry,
        )

    def create(
        self,
        session_id: str,
        question: str,
        *,
        config_snapshot: Mapping[str, object],
        budgets: Mapping[str, int],
    ) -> ExplorationRun:
        if self.repository.get(session_id) is None:
            raise KeyError(session_id)
        normalized = " ".join(question.split())
        if not normalized:
            raise ValueError("exploration question must not be blank")
        snapshot = self.coordinator.create(CreateRunCommand(
            session_id,
            normalized,
            dict(config_snapshot),
            dict(budgets),
        ))
        run = self.repository.get_exploration_run(snapshot.run_id)
        if run is None:
            raise ExplorationNotFoundError(snapshot.run_id)
        return run

    def get(self, run_id: str) -> ExplorationRun:
        run = self.repository.get_exploration_run(run_id)
        if run is None:
            raise ExplorationNotFoundError(run_id)
        return run

    def list_for_session(self, session_id: str) -> tuple[ExplorationRun, ...]:
        if self.repository.get(session_id) is None:
            raise KeyError(session_id)
        return self.repository.list_exploration_runs(session_id)

    def cancel(self, run_id: str) -> ExplorationRun:
        self.get(run_id)
        snapshot = self.coordinator.inspect(run_id)
        return self.cancel_current(
            run_id,
            expected_attempt_id=snapshot.current_attempt.attempt_id,
            idempotency_key=f"legacy-cancel:{snapshot.current_attempt.attempt_id}",
        )

    def cancel_current(
        self, run_id: str, *, expected_attempt_id: str, idempotency_key: str
    ) -> ExplorationRun:
        try:
            self.coordinator.cancel_current(
                run_id,
                expected_attempt_id=expected_attempt_id,
                idempotency_key=idempotency_key,
            )
        except RunNotFoundError as error:
            raise ExplorationNotFoundError(run_id) from error
        except RunConflictError as error:
            raise ExplorationRetryError(str(error)) from error
        return self.get(run_id)

    def retry(self, run_id: str) -> ExplorationRun:
        self.get(run_id)
        try:
            current_attempt = self.coordinator.inspect(run_id).current_attempt
        except (RunNotFoundError, RunConflictError) as error:
            raise ExplorationRetryError(str(error)) from error

        return self.continue_run(
            run_id,
            expected_attempt_id=current_attempt.attempt_id,
            idempotency_key=f"legacy-continue:{current_attempt.attempt_id}",
        )

    def continue_run(
        self, run_id: str, *, expected_attempt_id: str, idempotency_key: str
    ) -> ExplorationRun:
        try:
            self.coordinator.continue_run(
                run_id,
                expected_attempt_id=expected_attempt_id,
                idempotency_key=idempotency_key,
            )
        except RunNotFoundError as error:
            raise ExplorationNotFoundError(run_id) from error
        except RunConflictError as error:
            raise ExplorationRetryError(str(error)) from error
        return self.get(run_id)
