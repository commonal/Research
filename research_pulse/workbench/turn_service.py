"""Application service for the unified conversational turn boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable
from uuid import uuid4

from research_pulse.workbench.capability_router import CapabilityRouter
from research_pulse.workbench.turn_adapters import TurnAdapter
from research_pulse.workbench.turn_runtime import (
    CapabilityDecision,
    TurnAuditRecord,
    TurnAuditRepository,
    TurnRequest,
    TurnResult,
)
from research_pulse.workbench.turn_events import UnsequencedTurnEvent


class TurnRuntime:
    """Select one adapter and preserve one turn identity across retries."""

    def __init__(
        self,
        *,
        router: CapabilityRouter | None = None,
        synchronous: TurnAdapter | None = None,
        background: TurnAdapter | None = None,
        durable: TurnAdapter | None = None,
        audit_repository: TurnAuditRepository | None = None,
        available_capabilities: Iterable[str] | None = None,
        turn_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.router = router or CapabilityRouter()
        self.synchronous = synchronous
        self.background = background
        self.durable = durable
        self.audit_repository = audit_repository
        self.available_capabilities = (
            frozenset(available_capabilities)
            if available_capabilities is not None
            else None
        )
        self.turn_id_factory = turn_id_factory
        self._results: dict[tuple[str, str], TurnResult] = {}

    def start(self, request: TurnRequest) -> TurnResult:
        key = (
            request.session_id,
            request.client_request_id,
        ) if request.client_request_id else None
        if key is not None and key in self._results:
            return self._results[key]

        decision = self.router.resolve(
            request,
            available_capabilities=self.available_capabilities,
        )
        turn_id = self.turn_id_factory()
        audit = TurnAuditRecord(
            turn_id=turn_id,
            session_id=request.session_id,
            decision=decision,
            context_snapshot=request.interaction_context.to_dict(),
            client_request_id=request.client_request_id,
            status="queued" if decision.execution_mode == "durable" else "running",
        )
        if self.audit_repository is not None:
            audit = self.audit_repository.create_or_get_turn_audit(audit)
            if key is not None and audit.turn_id != turn_id:
                # A persisted idempotency hit means the original turn owns this
                # request. A final result may be loaded by a future projector;
                # do not create a second message or Attempt now.
                existing = TurnResult(
                    turn_id=audit.turn_id,
                    assistant_message=None,
                    capability_decision=audit.decision,
                    status=audit.status,
                    durable_handle=audit.run_id,
                )
                self._results[key] = existing
                return existing

        self._append_event(
            turn_id,
            UnsequencedTurnEvent(
                "route_decided",
                "已确定回答能力和执行模式",
                stable_ids={"capability": decision.capability},
            ),
        )

        missing_paper_context = (
            decision.capability == "paper"
            and not (
                request.interaction_context.selection
                or request.interaction_context.canonical_paper_id
            )
        )
        if decision.status in {"unavailable", "ambiguous"} or missing_paper_context:
            if decision.status == "ambiguous" or missing_paper_context:
                message = "请先打开论文或选择有效文本后再执行该论文动作。"
                code = "paper_context_required"
            else:
                message = "当前能力未配置，请检查服务配置后重试。"
                code = "capability_unavailable"
            result = TurnResult(
                turn_id=turn_id,
                assistant_message=message,
                capability_decision=decision,
                status="failed",
                error={
                    "code": code,
                    "message": message,
                },
            )
        else:
            if decision.execution_mode == "durable":
                adapter = self.durable
            elif decision.execution_mode == "background":
                # Keep old embedders usable while production wiring adopts the
                # explicit background adapter.  The fallback is synchronous,
                # never a silent durable research escalation.
                adapter = self.background or self.synchronous
            else:
                adapter = self.synchronous
            if adapter is None:
                result = TurnResult(
                    turn_id=turn_id,
                    assistant_message="当前执行能力暂不可用，请稍后重试。",
                    capability_decision=replace(decision, status="unavailable"),
                    status="failed",
                    error={
                        "code": "adapter_unavailable",
                        "message": "当前执行能力暂不可用，请稍后重试。",
                    },
                )
            else:
                try:
                    result = adapter.execute(request, decision)
                except Exception:
                    # Adapter internals may contain provider/network details;
                    # never expose those at the conversation boundary.
                    result = TurnResult(
                        turn_id=turn_id,
                        assistant_message="当前回答未完成，请稍后重试。",
                        capability_decision=decision,
                        status="failed",
                        error={
                            "code": "turn_execution_failed",
                            "message": "当前回答未完成，请稍后重试。",
                        },
                    )

        # The runtime owns the turn identity. Adapters may use their own
        # message/run identifiers internally, but must not create a second
        # conversational turn identity at the boundary.
        if result.turn_id != turn_id:
            result = replace(result, turn_id=turn_id)

        if self.audit_repository is not None:
            updated = replace(
                audit,
                status=result.status,
                run_id=result.durable_handle or audit.run_id,
            )
            self.audit_repository.save_turn_audit(updated)
        self._append_event(
            result.turn_id,
            UnsequencedTurnEvent(
                {
                    "queued": "turn_started",
                    "running": "turn_started",
                    "completed": "turn_completed",
                    "failed": "turn_failed",
                    "cancelled": "turn_cancelled",
                }.get(result.status, "turn_failed"),
                {
                    "queued": "任务已排队",
                    "running": "任务已开始",
                    "completed": "回答已完成",
                    "failed": "回答未完成",
                    "cancelled": "任务已取消",
                }.get(result.status, "回答未完成"),
            ),
        )
        if key is not None:
            self._results[key] = result
        return result

    def _append_event(self, turn_id: str, event: UnsequencedTurnEvent) -> None:
        if self.audit_repository is None:
            return
        append = getattr(self.audit_repository, "append_turn_event", None)
        if append is not None:
            append(turn_id, event)
