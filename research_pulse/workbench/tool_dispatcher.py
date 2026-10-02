"""Policy-owned tool registration and deterministic serial dispatch contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from threading import RLock
from typing import Any
from uuid import uuid4

from research_pulse.workbench.budget_enforcer import BudgetLedger, RunTerminated
from research_pulse.workbench.errors import RevisionConflictError
from research_pulse.workbench.tool_execution import (
    OperationEffectUnknownError,
    Retryability,
    SideEffectState,
    ToolErrorCode,
    ToolExecutionResult,
    ToolOutcome,
    ToolOutcomeStatus,
)


class ToolPreflightError(ValueError):
    pass


class RateLimitError(RuntimeError):
    def __init__(self, *, retry_after_seconds: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


class ServiceUnavailableError(RuntimeError):
    pass


class ToolNotConfiguredError(RuntimeError):
    """A declared capability exists but its runtime dependency is unavailable."""


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"


class Idempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    OPERATION_KEYED = "operation_keyed"
    NON_IDEMPOTENT = "non_idempotent"


class ParallelPolicy(StrEnum):
    SERIAL = "serial"
    THREAD_SAFE_READ = "thread_safe_read"


@dataclass(frozen=True)
class ToolPolicy:
    effect: ToolEffect
    idempotency: Idempotency
    timeout_seconds: float
    max_retries: int
    resource_keys: Callable[[Mapping[str, object]], Sequence[str]]
    parallel_policy: ParallelPolicy

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        if self.max_retries < 0:
            raise ValueError("tool retries must not be negative")
        if not callable(self.resource_keys):
            raise ValueError("tool resource key resolver is required")


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    handler: Callable[..., Any]
    policy: ToolPolicy | None


class ToolPolicyRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        policy: ToolPolicy | None = None,
    ) -> None:
        if not name.strip() or not callable(handler):
            raise ValueError("tool name and handler are required")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(name, handler, policy)

    def preflight(self, names: Sequence[str]) -> Mapping[str, RegisteredTool]:
        exposed: dict[str, RegisteredTool] = {}
        for name in names:
            registered = self._tools.get(name)
            if registered is None or registered.policy is None:
                raise ToolPreflightError(f"tool policy is missing or incomplete: {name}")
            exposed[name] = registered
        return MappingProxyType(exposed)


@dataclass(frozen=True)
class ToolErrorViews:
    user: str
    model: str
    diagnostic_id: str | None


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolPolicyRegistry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        diagnostic_id_factory: Callable[[], str] = lambda: str(uuid4()),
        operation_journal: Any | None = None,
        attempt_generation: int | None = None,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._diagnostic_id_factory = diagnostic_id_factory
        self._diagnostics: dict[str, str] = {}
        self._execution_lock = RLock()
        self._operation_journal = operation_journal
        self._attempt_generation = attempt_generation

    def dispatch(
        self,
        *,
        attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> ToolOutcome:
        return self.execute(
            attempt_id=attempt_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
        ).outcome

    def execute(
        self,
        *,
        attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> ToolExecutionResult:
        registered = self._registry.preflight((tool_name,))[tool_name]
        if any(not isinstance(key, str) or not key.strip() for key in arguments):
            raise ValueError("tool arguments require non-blank string keys")
        normalized = dict(arguments)
        started = self._clock()
        operation_id: str | None = None
        resource_key: str | None = None
        if (
            registered.policy.effect is ToolEffect.WRITE
            and registered.policy.idempotency is Idempotency.OPERATION_KEYED
            and self._operation_journal is not None
        ):
            if self._attempt_generation is None:
                raise ValueError("attempt generation is required for journaled writes")
            resource_keys = tuple(registered.policy.resource_keys(normalized))
            if len(resource_keys) != 1 or not resource_keys[0].strip():
                raise ValueError("journaled write requires exactly one resource key")
            resource_key = resource_keys[0]
            operation_id = f"{attempt_id}:{tool_call_id}"
            try:
                replay = self._operation_journal.begin_coordinated_operation(
                    operation_id,
                    attempt_id,
                    expected_generation=self._attempt_generation,
                    tool_call_id=tool_call_id,
                    resource_key=resource_key,
                    operation_kind=tool_name,
                )
            except OperationEffectUnknownError:
                return ToolExecutionResult(ToolOutcome(
                    tool_call_id=tool_call_id,
                    attempt_id=attempt_id,
                    tool_name=tool_name,
                    status=ToolOutcomeStatus.EFFECT_UNKNOWN,
                    error_code=ToolErrorCode.EFFECT_UNKNOWN,
                    retryability=Retryability.NEVER,
                    side_effect_state=SideEffectState.UNKNOWN,
                    started_at=started,
                    finished_at=self._clock(),
                    safe_message="写入结果无法确认，需要先对账后再继续。",
                    diagnostic_id=self._diagnostic_id_factory(),
                    operation_id=operation_id,
                ), None)
            if replay is not None:
                return ToolExecutionResult(self._success_outcome(
                    registered, started, attempt_id, tool_call_id, tool_name,
                    operation_id=operation_id,
                ), replay)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workbench-tool")
        try:
            # The lock must be owned by the worker for the handler's complete
            # lifetime. If the caller times out, the Python thread cannot be
            # killed; releasing the lock in the caller would let a later tool
            # overlap the still-running timed-out handler.
            result = executor.submit(
                self._invoke_serialized, registered.handler, normalized
            ).result(timeout=registered.policy.timeout_seconds)
        except RateLimitError as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.RETRYABLE_FAILURE, ToolErrorCode.RATE_LIMITED,
                Retryability.AUTOMATIC, retry_after_seconds=exc.retry_after_seconds,
            )
            return ToolExecutionResult(outcome, None)
        except ServiceUnavailableError as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.RETRYABLE_FAILURE,
                ToolErrorCode.SERVICE_UNAVAILABLE, Retryability.AUTOMATIC,
            )
            return ToolExecutionResult(outcome, None)
        except FutureTimeoutError as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.RETRYABLE_FAILURE, ToolErrorCode.TIMED_OUT,
                Retryability.AUTOMATIC,
            )
            return ToolExecutionResult(outcome, None)
        except ToolNotConfiguredError as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.TERMINAL_FAILURE, ToolErrorCode.NOT_CONFIGURED,
                Retryability.NEVER,
            )
            return ToolExecutionResult(outcome, None)
        except PermissionError as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.TERMINAL_FAILURE, ToolErrorCode.PERMISSION_DENIED,
                Retryability.NEVER,
            )
            return ToolExecutionResult(outcome, None)
        except ConnectionError as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.RETRYABLE_FAILURE, ToolErrorCode.CONNECTION_FAILED,
                Retryability.AUTOMATIC,
            )
            return ToolExecutionResult(outcome, None)
        except RevisionConflictError as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.REJECTED, ToolErrorCode.REVISION_CONFLICT,
                Retryability.AFTER_REPLAN,
                side_effect_state=SideEffectState.NOT_APPLIED,
            )
            return ToolExecutionResult(outcome, None)
        except ValueError as exc:
            error_code = (
                ToolErrorCode.NO_RESULTS
                if "returned no results" in str(exc).lower()
                else ToolErrorCode.INVALID_ARGUMENT
            )
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.REJECTED, error_code,
                Retryability.AFTER_CORRECTION,
            )
            return ToolExecutionResult(outcome, None)
        except RunTerminated:
            raise
        except Exception as exc:
            outcome = self._failed_outcome(
                exc, started, attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.TERMINAL_FAILURE, ToolErrorCode.INTERNAL_INVARIANT,
                Retryability.NEVER,
            )
            return ToolExecutionResult(outcome, None)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if isinstance(result, Mapping) and {
            str(key).lower() for key in result
        } & {"content", "prompt", "reasoning", "api_key", "token", "path"}:
            outcome = self._failed_outcome(
                ValueError("tool returned forbidden fields"), started,
                attempt_id, tool_call_id, tool_name,
                ToolOutcomeStatus.TERMINAL_FAILURE, ToolErrorCode.INVALID_RESULT,
                Retryability.NEVER,
            )
            return ToolExecutionResult(outcome, None)
        if operation_id is not None:
            if not isinstance(result, Mapping):
                outcome = self._failed_outcome(
                    ValueError("journaled write did not return a commit receipt"),
                    started, attempt_id, tool_call_id, tool_name,
                    ToolOutcomeStatus.TERMINAL_FAILURE,
                    ToolErrorCode.INVALID_RESULT, Retryability.NEVER,
                )
                return ToolExecutionResult(outcome, None)
            self._operation_journal.settle_coordinated_operation(
                operation_id,
                attempt_id,
                expected_generation=self._attempt_generation,
                status="committed",
                receipt=dict(result),
            )
        outcome = self._success_outcome(
            registered, started, attempt_id, tool_call_id, tool_name,
            operation_id=operation_id,
        )
        return ToolExecutionResult(outcome, result)

    def _invoke_serialized(
        self,
        handler: Callable[..., Any],
        arguments: Mapping[str, object],
    ) -> Any:
        with self._execution_lock:
            return handler(**arguments)

    def _success_outcome(
        self,
        registered: RegisteredTool,
        started: datetime,
        attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        *,
        operation_id: str | None = None,
    ) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            tool_name=tool_name,
            status=ToolOutcomeStatus.SUCCEEDED,
            error_code=None,
            retryability=Retryability.NEVER,
            side_effect_state=(
                SideEffectState.NONE
                if registered.policy.effect is ToolEffect.READ
                else SideEffectState.COMMITTED
            ),
            started_at=started,
            finished_at=self._clock(),
            operation_id=operation_id,
        )

    def _failed_outcome(
        self,
        error: Exception,
        started: datetime,
        attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        status: ToolOutcomeStatus,
        error_code: ToolErrorCode,
        retryability: Retryability,
        retry_after_seconds: float | None = None,
        side_effect_state: SideEffectState = SideEffectState.NONE,
    ) -> ToolOutcome:
        diagnostic_id = self._diagnostic_id_factory()
        self._diagnostics[diagnostic_id] = repr(error)
        messages = {
            ToolErrorCode.INVALID_ARGUMENT: "工具参数或前置条件无效，请修正后重试",
            ToolErrorCode.TIMED_OUT: "工具响应超时，可稍后重试",
            ToolErrorCode.PERMISSION_DENIED: "工具权限或配置不可用，请检查配置",
            ToolErrorCode.NOT_CONFIGURED: "任务所需工具尚未配置，请检查运行环境",
            ToolErrorCode.CONNECTION_FAILED: "外部服务连接失败，可稍后重试",
            ToolErrorCode.RATE_LIMITED: "外部服务请求受限，可稍后重试",
            ToolErrorCode.SERVICE_UNAVAILABLE: "外部服务暂时不可用，可稍后重试",
            ToolErrorCode.INVALID_RESULT: "工具返回结果不符合安全合同",
            ToolErrorCode.NO_RESULTS: "网页检索未返回结果，可改写查询或说明当前无法核验",
            ToolErrorCode.REVISION_CONFLICT: "Workspace 已发生变化，请重新读取后规划新的修改",
            ToolErrorCode.INTERNAL_INVARIANT: "工具执行发生内部错误",
        }
        return ToolOutcome(
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            tool_name=tool_name,
            status=status,
            error_code=error_code,
            retryability=retryability,
            side_effect_state=side_effect_state,
            started_at=started,
            finished_at=self._clock(),
            safe_message=messages[error_code],
            diagnostic_id=diagnostic_id,
            retry_after_seconds=retry_after_seconds,
        )

    def error_views(self, outcome: ToolOutcome) -> ToolErrorViews:
        user = outcome.safe_message or "工具调用完成"
        model = (
            user
            if outcome.error_code is ToolErrorCode.NO_RESULTS
            else (
                "参数无效，可使用不同参数重新调用"
                if outcome.retryability is Retryability.AFTER_CORRECTION
                else user
            )
        )
        return ToolErrorViews(user, model, outcome.diagnostic_id)

    def dispatch_with_recovery(
        self,
        *,
        attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
        budget_ledger: BudgetLedger,
        sleeper: Callable[[float], None],
    ) -> ToolOutcome:
        policy = self._registry.preflight((tool_name,))[tool_name].policy
        retries = 0
        last: ToolOutcome | None = None
        while True:
            try:
                budget_ledger.authorize_tool(tool_name)
            except RunTerminated:
                if last is not None:
                    return last
                raise
            last = self.dispatch(
                attempt_id=attempt_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
            )
            if (
                last.retryability is not Retryability.AUTOMATIC
                or retries >= policy.max_retries
            ):
                return last
            try:
                budget_ledger.authorize_recovery(last.error_code.value)
            except RunTerminated:
                return last
            retries += 1
            delay = last.retry_after_seconds or min(2 ** (retries - 1), 8)
            sleeper(delay)

    def diagnostic(self, diagnostic_id: str) -> str:
        return self._diagnostics[diagnostic_id]
