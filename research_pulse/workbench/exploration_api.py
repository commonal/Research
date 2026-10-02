"""HTTP lifecycle projection for durable exploration runs."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from research_pulse.workbench.exploration import (
    ExplorationNotFoundError,
    ExplorationRetryError,
    ExplorationService,
)
from research_pulse.workbench.run_coordinator import RunConflictError
from research_pulse.workbench.tool_execution import (
    Retryability,
    SideEffectState,
    ToolErrorCode,
    ToolOutcome,
    ToolOutcomeStatus,
)

_TOOL_RECOMMENDED_ACTIONS = {
    (ToolOutcomeStatus.REJECTED, Retryability.AFTER_CORRECTION): "correct_parameters",
    (ToolOutcomeStatus.RETRYABLE_FAILURE, Retryability.AUTOMATIC): "wait_for_limited_retry",
    (ToolOutcomeStatus.EFFECT_UNKNOWN, Retryability.NEVER): "reconcile_operation",
    (ToolOutcomeStatus.CANCELLED, Retryability.NEVER): "create_new_attempt",
}

_LEGACY_EVENT_SEQUENCE_MESSAGE = "旧版探索记录的事件序列不完整，无法继续；请重新发起问题。"


def _tool_recommended_action(outcome: ToolOutcome) -> str:
    if outcome.status is ToolOutcomeStatus.SUCCEEDED:
        return "none"
    action = _TOOL_RECOMMENDED_ACTIONS.get((outcome.status, outcome.retryability))
    if action:
        return action
    if outcome.error_code is ToolErrorCode.PERMISSION_DENIED:
        return "check_configuration"
    # Terminal failures without an automatic path continue as a NEW attempt
    # from persisted results — never as a claimed lossless resume.
    return "continue_from_persisted_results"


def _tool_detail(outcome: ToolOutcome) -> dict[str, object]:
    # Bounded user-facing view: safe fields only. Raw exceptions, arguments,
    # stack traces, prompts and tool payloads never leave the outcome.
    return {
        "tool_call_id": outcome.tool_call_id,
        "tool_name": outcome.tool_name,
        "status": outcome.status.value,
        "retryability": outcome.retryability.value,
        "side_effect_state": outcome.side_effect_state.value,
        "error_code": outcome.error_code.value if outcome.error_code else None,
        "safe_message": outcome.safe_message,
        "diagnostic_id": outcome.diagnostic_id,
        "recommended_action": _tool_recommended_action(outcome),
    }


def _is_legacy_event_sequence_failure(safe_error: str | None) -> bool:
    """Recognise the pre-attempt event-store failure without exposing internals.

    Older rows can contain the implementation exception raised when a retry
    appended to the run-level event table.  Those rows are not evidence that a
    new attempt is safely continuable: replaying the same mutation merely
    repeats the old failure.  Keep the row readable, but project it as a
    terminal diagnostic instead of offering a misleading Continue action.
    """
    normalized = (safe_error or "").lower()
    return "exploration event sequence" in normalized


class CreateExplorationRequest(BaseModel):
    question: str = Field(min_length=1)
    config_snapshot: dict[str, object] = Field(default_factory=dict)
    budgets: dict[str, int]


class AttemptMutationRequest(BaseModel):
    expected_attempt_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


def build_exploration_router(
    service: ExplorationService,
) -> APIRouter:
    router = APIRouter(prefix="/api/workbench", tags=["workbench"])

    def attempt_payload(attempt):
        failure = None
        # AWAITING_USER is a durable checkpoint: it may carry a stale generic
        # stop message from runs created before the checkpoint status was
        # introduced, but it must never render as a failure or offer retry.
        if attempt.status.value != "awaiting_user" and (
            attempt.safe_error or attempt.status.value in {
            "retryable_failure", "terminal_failure", "abandoned",
            }
        ):
            if _is_legacy_event_sequence_failure(attempt.safe_error):
                failure = {
                    "category": "legacy_failure",
                    "safe_message": _LEGACY_EVENT_SEQUENCE_MESSAGE,
                    "recommended_action": "inspect_diagnostic",
                }
            else:
                recommended_action = (
                    "continue" if attempt.status.value in {
                        "retryable_failure", "abandoned", "budget_exhausted",
                    } else "inspect_diagnostic"
                )
                failure = {
                    "category": attempt.status.value,
                    "safe_message": attempt.safe_error or "探索执行未完成",
                    "recommended_action": recommended_action,
                }
        return {
            "attempt_id": attempt.attempt_id,
            "attempt_no": attempt.attempt_no,
            "status": attempt.status.value,
            "generation": attempt.generation,
            "legacy": attempt.legacy,
            "budgets": dict(attempt.budgets),
            "application_budget_mode": (
                "experimental_unbounded"
                if bool(attempt.budgets.get("experimental_unbounded"))
                else "bounded"
            ),
            "budget_used": dict(attempt.budget_used),
            "failure": failure,
            "created_at": attempt.created_at,
            "started_at": attempt.started_at,
            "finished_at": attempt.finished_at,
        }

    def payload(run):
        result = asdict(run)
        result["application_budget_mode"] = (
            "experimental_unbounded"
            if bool(run.budgets.get("experimental_unbounded"))
            else "bounded"
        )
        try:
            snapshot = service.coordinator.inspect(run.run_id)
            result["current_attempt_id"] = snapshot.current_attempt.attempt_id
            result["current_attempt"] = attempt_payload(snapshot.current_attempt)
            result["attempt_history"] = [
                attempt_payload(attempt) for attempt in snapshot.attempt_history
            ]
            result["budget"] = {
                "current": dict(snapshot.current_budget_used),
                "cumulative": dict(snapshot.cumulative_budget_used),
            }
            result["failure"] = result["current_attempt"]["failure"]
            # ``awaiting_user_decision`` is a durable product checkpoint, not
            # an execution failure.  Older rows may still carry the kernel's
            # generic stop marker in the run-level ``safe_error`` column;
            # suppress that stale marker in the public projection so every
            # client observes one consistent lifecycle state.
            if snapshot.current_attempt.status.value == "awaiting_user":
                result["safe_error"] = None
            if snapshot.current_attempt.status.value == "budget_exhausted":
                consecutive_budget_exhaustions = 0
                for item in reversed(snapshot.attempt_history):
                    if item.status.value != "budget_exhausted":
                        break
                    consecutive_budget_exhaustions += 1
                if consecutive_budget_exhaustions >= 2:
                    result["failure"] = {
                        "category": "budget_exhausted_stalled",
                        "safe_message": "已连续两次预算耗尽，请缩小研究问题或重新开始",
                        "recommended_action": "narrow_scope",
                    }
            result["tool_details"] = [
                _tool_detail(outcome)
                for outcome in service.repository.list_tool_outcomes(
                    snapshot.current_attempt.attempt_id
                )
            ]
        except RunConflictError:
            # Legacy rows predate Attempt identity. Keep them readable without
            # inventing a boundary that never existed.
            result["current_attempt_id"] = None
            result["current_attempt"] = None
            result["attempt_history"] = []
            result["budget"] = {"current": {}, "cumulative": {}}
            result["tool_details"] = []
            result["failure"] = (
                {
                    "category": "legacy_failure",
                    "safe_message": run.safe_error,
                    "recommended_action": "inspect_diagnostic",
                }
                if run.safe_error else None
            )
        # Workbench exploration currently starts a fresh Attempt from durable
        # references. It does not restore an in-memory agent thread.
        result["continuation_mode"] = "persisted_results"
        resolved_config = (
            snapshot.config.get("resolved")
            if result.get("current_attempt") and isinstance(snapshot.config, dict)
            else None
        )
        result["retrieval_plan"] = (
            resolved_config.get("retrieval_plan")
            if isinstance(resolved_config, dict) else None
        )
        recovery_strategy = None
        if result.get("current_attempt"):
            input_snapshot = snapshot.current_attempt.input_snapshot
            continuation = input_snapshot.get("continuation", {})
            if isinstance(continuation, dict):
                recovery_strategy = continuation.get("recovery_strategy")
        result["continuation_strategy"] = recovery_strategy
        result["continuation_label"] = {
            "synthesize_from_persisted_evidence": "基于已有证据生成结论",
            "narrow_scope_required": "需要缩小研究问题",
        }.get(str(recovery_strategy), "基于已有结果继续执行")
        events = service.repository.list_exploration_events(run.run_id)
        # The frontend renders the live event stream from this list, so the
        # payload must expose the persisted safe events verbatim (persist-first
        # projection keeps refresh recovery working through the same list).
        event_times = {
            (persisted.attempt_id, persisted.sequence_no): persisted.occurred_at.isoformat()
            for persisted in service.repository.list_run_attempt_events(run.run_id)
        }
        result["events"] = [
            {
                "attempt_id": event.attempt_id,
                "sequence_no": event.sequence_no,
                "event_type": event.event_type,
                "summary": event.summary,
                "stable_ids": dict(event.stable_ids),
                "counters": dict(event.counters),
                # Safe timing metadata for the public progress timeline.  Do
                # not expose the event payload itself: it may contain provider
                # arguments or large model/tool responses.
                "occurred_at": event_times.get((event.attempt_id, event.sequence_no)),
            }
            for event in events
        ]
        # The legacy ExplorationRun row predates durable HITL checkpoints and
        # therefore commonly has a null ``decision_id``.  The checkpoint event
        # is the authoritative persisted link; project it so a client can
        # resolve the decision without first guessing which workspace file to
        # inspect.  Keep the row value when an older integration already set it.
        if not result.get("decision_id"):
            result["decision_id"] = next(
                (
                    str(event.stable_ids["decision_id"])
                    for event in reversed(events)
                    if event.event_type == "awaiting_decision"
                    and event.stable_ids.get("decision_id")
                ),
                None,
            )
        # A managed paper can be read directly without a preceding search, so
        # it has no ``source_discovered`` event.  Project sources from both
        # discovery and context-read events, then enrich them from the paper
        # repository when a title/URL is available.  This keeps the source
        # list aligned with the evidence that actually reached the model.
        source_rows: dict[str, dict[str, object]] = {}
        for event in events:
            source_id = event.stable_ids.get("source_id")
            if not source_id:
                continue
            source_id = str(source_id)
            row = source_rows.setdefault(source_id, {
                "source_id": source_id,
                "title": "已读论文证据",
                "kind": "managed",
                "url": "",
                "relevance": "",
            })
            if event.event_type == "source_discovered":
                row["title"] = event.stable_ids.get("title") or row["title"]
                row["kind"] = event.stable_ids.get("source_kind", row["kind"])
                row["url"] = event.stable_ids.get("url") or row["url"]
                row["relevance"] = event.stable_ids.get("relevance") or row["relevance"]
        get_paper = getattr(service.repository, "get_paper", None)
        if callable(get_paper):
            for source_id, row in source_rows.items():
                try:
                    paper = get_paper(source_id)
                except Exception:  # repository enrichment is best effort
                    paper = None
                if paper is None:
                    continue
                title = getattr(paper, "title", None)
                source_url = getattr(paper, "source_url", None)
                if title:
                    row["title"] = title
                if source_url:
                    row["url"] = source_url
        result["sources"] = list(source_rows.values())
        web_search_usage = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }
        for event in events:
            extras = event.extras
            if not isinstance(extras, (list, tuple)):
                continue
            for item in extras:
                if not isinstance(item, dict) or item.get("kind") != "web_search_usage":
                    continue
                web_search_usage["calls"] += 1
                for key in web_search_usage:
                    if key == "calls":
                        continue
                    value = item.get(key)
                    if isinstance(value, int) and value >= 0:
                        web_search_usage[key] += value
        result["web_search_usage"] = web_search_usage
        result["phase"] = next((event.summary for event in reversed(events) if event.event_type in {"phase_changed", "final_draft", "run_failed", "run_cancelled"}), run.status.value)
        result["counters"] = events[-1].counters if events else {}
        tool_counts: dict[str, int] = {}
        for event in events:
            if event.event_type == "tool_started":
                tool_name = event.stable_ids.get("tool_name")
                if tool_name:
                    tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        result["tools_used"] = [
            {"tool": tool_name, "calls": calls}
            for tool_name, calls in sorted(tool_counts.items())
        ]
        # Event counters are cumulative snapshots, so the final token total is
        # the LAST event's counters — summing every event would multiply the
        # real usage by the number of events.
        final_counters = events[-1].counters if events else {}
        result["token_usage"] = {
            "input_tokens": final_counters.get("input_tokens", 0),
            "output_tokens": final_counters.get("output_tokens", 0),
        }
        # Compatibility projection for legacy rows written before the kernel
        # enforced the non-empty draft invariant. They must not keep looking
        # successful while rendering an empty conversation card.
        if run.status.value == "completed" and not (run.final_draft or "").strip():
            result["status"] = "failed"
            result["safe_error"] = "completed exploration has no final draft"
            result["phase"] = "未生成可显示正文"
            result["failure"] = {
                "category": "terminal_failure",
                "safe_message": "该探索已结束，但没有生成可显示正文；请重新发起问题。",
                "recommended_action": "inspect_diagnostic",
            }
        elif _is_legacy_event_sequence_failure(run.safe_error):
            # Do not leak the old implementation exception through the raw
            # run projection either.  The user-facing failure object above is
            # the canonical diagnostic and the raw ``safe_error`` remains
            # available only in server-side storage.
            result["safe_error"] = _LEGACY_EVENT_SEQUENCE_MESSAGE
        return result

    @router.get("/sessions/{session_id}/explorations")
    def list_runs(session_id: str):
        try:
            return {"items": [payload(run) for run in service.list_for_session(session_id)]}
        except KeyError as error:
            raise HTTPException(404, "session not found") from error

    @router.post("/sessions/{session_id}/explorations", status_code=202)
    def create_run(session_id: str, request: CreateExplorationRequest):
        try:
            run = service.create(
                session_id, request.question,
                config_snapshot=request.config_snapshot,
                budgets=request.budgets,
            )
            result = payload(run)
            return result
        except KeyError as error:
            raise HTTPException(404, "session not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/explorations/{run_id}")
    def get_run(run_id: str):
        try:
            return payload(service.get(run_id))
        except ExplorationNotFoundError as error:
            raise HTTPException(404, "exploration not found") from error

    @router.post("/explorations/{run_id}/cancel")
    def cancel_run(run_id: str, request: AttemptMutationRequest):
        try:
            return payload(service.cancel_current(
                run_id,
                expected_attempt_id=request.expected_attempt_id,
                idempotency_key=request.idempotency_key,
            ))
        except ExplorationNotFoundError as error:
            raise HTTPException(404, "exploration not found") from error
        except ExplorationRetryError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/explorations/{run_id}/continue", status_code=202)
    def continue_run(run_id: str, request: AttemptMutationRequest):
        try:
            continued = service.continue_run(
                run_id,
                expected_attempt_id=request.expected_attempt_id,
                idempotency_key=request.idempotency_key,
            )
            result = payload(continued)
            return result
        except ExplorationNotFoundError as error:
            raise HTTPException(404, "exploration not found") from error
        except ExplorationRetryError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/explorations/{run_id}/retry", status_code=202)
    def retry_run(run_id: str):
        try:
            retried = service.retry(run_id)
        except ExplorationNotFoundError as error:
            raise HTTPException(404, "exploration not found") from error
        except ExplorationRetryError as error:
            raise HTTPException(409, str(error)) from error
        return payload(retried)

    return router
