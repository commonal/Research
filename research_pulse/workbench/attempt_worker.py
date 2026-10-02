"""Persistent application-owned worker for queued research Attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

from research_pulse.workbench.agent_kernel import AgentKernel, AttemptContext, EventSink
from research_pulse.workbench.agent_runtime import RunBudgets
from research_pulse.workbench.cancellation import AttemptCancelled, CancellationRegistry
from research_pulse.workbench.run_events import AttemptEventStore, UnsequencedEvent
from research_pulse.workbench.run_models import AttemptStatus


class _PersistedEventSink(EventSink):
    def __init__(self, store: AttemptEventStore, attempt_id: str, generation: int) -> None:
        self._store = store
        self._attempt_id = attempt_id
        self._generation = generation

    def emit(self, event: UnsequencedEvent) -> None:
        self._store.append(self._attempt_id, self._generation, event)


class AttemptWorker:
    """One bounded loop whose lifetime is owned by the application lifespan."""

    def __init__(
        self,
        repository,
        coordinator,
        kernel: AgentKernel,
        *,
        cancellation_registry: CancellationRegistry | None = None,
        worker_id: str | None = None,
        poll_interval: float = 0.1,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if poll_interval <= 0 or lease_duration.total_seconds() <= 0:
            raise ValueError("positive poll interval and lease duration are required")
        self._repository = repository
        self._coordinator = coordinator
        self._kernel = kernel
        self._cancellation = cancellation_registry or CancellationRegistry()
        self._worker_id = worker_id or f"attempt-worker:{uuid4()}"
        self._poll_interval = poll_interval
        self._lease_duration = lease_duration
        self._events = AttemptEventStore(repository)
        self._stop = Event()
        self._idle = Event()
        self._idle.set()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name=self._worker_id, daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    def wait_until_idle(self, *, timeout: float) -> bool:
        return self._idle.wait(timeout)

    def run_once(self) -> bool:
        lease = self._repository.claim_next_attempt(
            self._worker_id,
            now=datetime.now(UTC),
            lease_duration=self._lease_duration,
        )
        if lease is None:
            return False
        self._idle.clear()
        lease_stop = Event()
        lease_thread: Thread | None = None
        try:
            attempt = self._repository.get_coordinated_attempt(lease.attempt_id)
            if attempt is None:
                raise KeyError(lease.attempt_id)
            run = self._repository.get_exploration_run(attempt.run_id)
            if run is None:
                raise KeyError(attempt.run_id)
            profile = str(run.config_snapshot.get("profile") or "literature")
            capabilities = self._kernel.capabilities(profile)
            configured_tools = run.config_snapshot.get("allowed_tools")
            allowed_tools = (
                tuple(str(item) for item in configured_tools)
                if isinstance(configured_tools, (list, tuple))
                else tuple(item.name for item in capabilities)
            )
            continuation = attempt.input_snapshot.get("continuation", {})
            context = AttemptContext(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                generation=lease.generation,
                profile=profile,
                question=run.question_snapshot,
                allowed_tools=allowed_tools,
                budgets=RunBudgets(**dict(attempt.budgets)),
                input_snapshot=attempt.input_snapshot,
                recovery_facts=(continuation if isinstance(continuation, dict) else {}),
            )
            cancellation = self._cancellation.for_attempt(attempt.attempt_id)
            lease_thread = Thread(
                target=self._renew_lease,
                args=(attempt.attempt_id, lease.generation, cancellation, lease_stop),
                name=f"{self._worker_id}:lease",
                daemon=True,
            )
            lease_thread.start()
            outcome = self._kernel.execute(
                context,
                _PersistedEventSink(self._events, attempt.attempt_id, lease.generation),
                cancellation,
            )
            self._coordinator.record_attempt_outcome(
                attempt.attempt_id,
                outcome.status,
                expected_generation=lease.generation,
                safe_error=outcome.safe_error,
                budget_used=outcome.budget_used,
                final_draft=outcome.final_draft,
            )
        except AttemptCancelled:
            self._settle_failure(lease.attempt_id, lease.generation, AttemptStatus.CANCELLED, "cancelled")
        except (KeyError, TypeError, ValueError) as exc:
            self._settle_failure(
                lease.attempt_id, lease.generation,
                AttemptStatus.TERMINAL_FAILURE, f"invalid attempt configuration: {type(exc).__name__}",
            )
        except Exception as exc:
            self._settle_failure(
                lease.attempt_id, lease.generation,
                AttemptStatus.RETRYABLE_FAILURE, f"agent execution failed: {type(exc).__name__}",
            )
        finally:
            lease_stop.set()
            if lease_thread is not None:
                lease_thread.join(timeout=1)
            self._idle.set()
        return True

    def _settle_failure(
        self, attempt_id: str, generation: int, status: AttemptStatus, safe_error: str
    ) -> None:
        try:
            self._coordinator.record_attempt_outcome(
                attempt_id, status, expected_generation=generation, safe_error=safe_error
            )
        except (KeyError, ValueError):
            # A newer lease generation owns settlement; the stale worker must
            # not overwrite it.
            return

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self._poll_interval)

    def _renew_lease(
        self, attempt_id: str, generation: int, cancellation, stopped: Event
    ) -> None:
        interval = min(30.0, self._lease_duration.total_seconds() / 3)
        while not stopped.wait(interval):
            try:
                self._repository.renew_attempt_lease(
                    attempt_id,
                    worker_id=self._worker_id,
                    expected_generation=generation,
                    now=datetime.now(UTC),
                    lease_duration=self._lease_duration,
                )
            except (KeyError, ValueError):
                cancellation.cancel()
                return
