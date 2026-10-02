"""Deep public seam for workbench ResearchRun lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from research_pulse.workbench.models import ExplorationRun, ExplorationStatus
from research_pulse.workbench.cancellation import CancellationRegistry
from research_pulse.workbench.run_models import (
    Attempt,
    AttemptStatus,
    CONTINUABLE_ATTEMPT_STATUSES,
    RunSnapshot,
)
from research_pulse.workbench.continuation import ContinuationAssembler


class RunNotFoundError(LookupError):
    pass


class RunConflictError(ValueError):
    pass


@dataclass(frozen=True)
class CreateRunCommand:
    session_id: str
    question: str
    config: Mapping[str, object]
    budgets: Mapping[str, int]


class ResearchRunCoordinator:
    """Own Run/Attempt identity while persistence remains an internal adapter."""

    def __init__(
        self,
        repository,
        *,
        run_id_factory: Callable[[], str] = lambda: str(uuid4()),
        attempt_id_factory: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cancellation_registry: CancellationRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._run_id_factory = run_id_factory
        self._attempt_id_factory = attempt_id_factory
        self._clock = clock
        self._cancellation_registry = cancellation_registry

    def create(self, command: CreateRunCommand) -> RunSnapshot:
        if self._repository.get(command.session_id) is None:
            raise KeyError(command.session_id)
        question = " ".join(command.question.split())
        if not question:
            raise ValueError("research run question must not be blank")
        run_no = max(
            (item.attempt for item in self._repository.list_exploration_runs(command.session_id)),
            default=0,
        ) + 1
        run_id = self._run_id_factory()
        now = self._clock()
        run = ExplorationRun(
            run_id=run_id,
            session_id=command.session_id,
            question_snapshot=question,
            attempt=run_no,
            status=ExplorationStatus.QUEUED,
            config_snapshot=dict(command.config),
            budgets=dict(command.budgets),
            created_at=now,
        )
        attempt = Attempt(
            attempt_id=self._attempt_id_factory(),
            run_id=run_id,
            attempt_no=1,
            status=AttemptStatus.QUEUED,
            input_snapshot={"question": question, "config": dict(command.config)},
            budgets=dict(command.budgets),
            created_at=now,
        )
        self._repository.insert_coordinated_run(run, attempt)
        return self.inspect(run_id)

    def inspect(self, run_id: str) -> RunSnapshot:
        run = self._repository.get_exploration_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        attempts = self._repository.list_coordinated_attempts(run_id)
        if not attempts:
            raise RunConflictError(f"run has no attempt: {run_id}")
        return RunSnapshot(
            run_id=run.run_id,
            session_id=run.session_id,
            question=run.question_snapshot,
            current_attempt=attempts[-1],
            attempt_history=attempts,
            config=run.config_snapshot,
        )

    def continue_run(
        self,
        run_id: str,
        *,
        expected_attempt_id: str,
        idempotency_key: str,
    ) -> RunSnapshot:
        if not expected_attempt_id.strip() or not idempotency_key.strip():
            raise ValueError("expected attempt id and idempotency key are required")
        current = self.inspect(run_id).current_attempt
        if current.attempt_id != expected_attempt_id:
            existing = self._repository.get_idempotent_attempt(
                run_id, "continue", idempotency_key
            )
            if existing is None:
                raise RunConflictError("current attempt changed")
            return self.inspect(run_id)
        if current.status not in CONTINUABLE_ATTEMPT_STATUSES:
            raise RunConflictError("current attempt is not continuable")
        if current.status is AttemptStatus.BUDGET_EXHAUSTED:
            consecutive_budget_exhaustions = 0
            for attempt in reversed(self.inspect(run_id).attempt_history):
                if attempt.status is not AttemptStatus.BUDGET_EXHAUSTED:
                    break
                consecutive_budget_exhaustions += 1
            if consecutive_budget_exhaustions >= 2:
                raise RunConflictError(
                    "连续预算耗尽，已停止自动继续；请缩小研究问题或重新开始"
                )
        input_snapshot = dict(current.input_snapshot)
        input_snapshot["continuation"] = ContinuationAssembler(
            self._repository
        ).assemble(
            current.attempt_id,
            prior_status=current.status.value,
        ).to_recovery_facts()
        next_attempt = Attempt(
            attempt_id=self._attempt_id_factory(),
            run_id=run_id,
            attempt_no=current.attempt_no + 1,
            status=AttemptStatus.QUEUED,
            generation=current.generation + 1,
            input_snapshot=input_snapshot,
            budgets=current.budgets,
            created_at=self._clock(),
        )
        try:
            self._repository.continue_coordinated_run(
                run_id,
                expected_attempt_id=expected_attempt_id,
                idempotency_key=idempotency_key,
                attempt=next_attempt,
            )
        except ValueError as exc:
            raise RunConflictError(str(exc)) from exc
        return self.inspect(run_id)

    def cancel_current(
        self,
        run_id: str,
        *,
        expected_attempt_id: str,
        idempotency_key: str,
    ) -> RunSnapshot:
        if not expected_attempt_id.strip() or not idempotency_key.strip():
            raise ValueError("expected attempt id and idempotency key are required")
        current = self.inspect(run_id).current_attempt
        if current.legacy:
            raise RunConflictError("legacy attempt is read-only")
        try:
            self._repository.cancel_coordinated_attempt(
                run_id,
                expected_attempt_id=expected_attempt_id,
                idempotency_key=idempotency_key,
                finished_at=self._clock(),
            )
        except ValueError as exc:
            raise RunConflictError(str(exc)) from exc
        if self._cancellation_registry is not None:
            self._cancellation_registry.cancel(expected_attempt_id)
        return self.inspect(run_id)

    def record_attempt_outcome(
        self,
        attempt_id: str,
        status: AttemptStatus,
        *,
        expected_generation: int,
        safe_error: str | None = None,
        budget_used: Mapping[str, int] | None = None,
        final_draft: str | None = None,
    ) -> None:
        """Worker-side settlement seam; user-facing callers use the four methods above."""
        self._repository.complete_coordinated_attempt(
            attempt_id,
            status,
            expected_generation=expected_generation,
            safe_error=safe_error,
            budget_used={} if budget_used is None else budget_used,
            final_draft=final_draft,
            finished_at=self._clock(),
        )
