"""HTTP boundary for independent fixed-pipeline NoteRuns.

Every read projects the run's persisted event stream onto the payload, plus
aggregated ``tool_uses``, ``token_usage`` and ``elapsed_ms`` so the UI can show
the real generation chain and its cost.
"""

from dataclasses import asdict

from fastapi import APIRouter, BackgroundTasks, HTTPException

from research_pulse.workbench.note_runs import NoteRunService


def build_note_run_router(service: NoteRunService) -> APIRouter:
    router = APIRouter(prefix="/api/workbench", tags=["workbench"])

    def payload(run):
        result = asdict(run)
        # Paths are process-local implementation details.  The browser only
        # needs the availability/approval flags and the knowledge id after
        # publication; returning absolute vault paths leaks the server layout
        # and cannot be used as a stable URL anyway.
        result["receipt_path"] = None
        result["draft_path"] = None
        result["draft_available"] = bool(run.draft_path)
        result["can_publish"] = run.status.value == "awaiting_approval"
        result["draft_preview"] = service.draft_preview(run.note_run_id)
        events = service.repository.list_note_run_events(run.note_run_id)
        result["events"] = [asdict(event) for event in events]
        tool_counts: dict[str, int] = {}
        input_tokens = output_tokens = 0
        for event in events:
            if event.event_type == "operation_completed":
                operation = event.stable_ids.get("operation")
                if operation:
                    tool_counts[operation] = tool_counts.get(operation, 0) + 1
            input_tokens += event.counters.get("input_tokens", 0)
            output_tokens += event.counters.get("output_tokens", 0)
        result["tool_uses"] = [
            {"operation": operation, "calls": calls}
            for operation, calls in sorted(tool_counts.items())
        ]
        result["token_usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        result["elapsed_ms"] = next(
            (event.counters.get("elapsed_ms", 0) for event in reversed(events) if event.event_type in {"note_completed", "note_ready", "note_failed"}),
            0,
        )
        return result

    @router.post("/sessions/{session_id}/papers/{paper_id}/note-runs", status_code=202)
    def create_note_run(session_id: str, paper_id: str, background_tasks: BackgroundTasks):
        try:
            run = service.create(session_id, paper_id)
        except KeyError as error:
            raise HTTPException(404, "session paper not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        background_tasks.add_task(service.execute, run.note_run_id)
        return payload(run)

    @router.get("/sessions/{session_id}/note-runs")
    def list_note_runs(session_id: str):
        try:
            return {"items": [payload(run) for run in service.list_for_session(session_id)]}
        except KeyError as error:
            raise HTTPException(404, "session not found") from error

    @router.get("/note-runs/{note_run_id}")
    def get_note_run(note_run_id: str):
        try:
            return payload(service.get(note_run_id))
        except KeyError as error:
            raise HTTPException(404, "note run not found") from error

    @router.post("/note-runs/{note_run_id}/retry", status_code=202)
    def retry_note_run(note_run_id: str, background_tasks: BackgroundTasks):
        try:
            run = service.retry(note_run_id)
        except KeyError as error:
            raise HTTPException(404, "note run not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        background_tasks.add_task(service.execute, run.note_run_id)
        return payload(run)

    @router.post("/note-runs/{note_run_id}/publish")
    def publish_note_run(note_run_id: str):
        """Commit a generated draft after the user explicitly approves it."""
        try:
            return payload(service.publish(note_run_id))
        except KeyError as error:
            raise HTTPException(404, "note run not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return router
