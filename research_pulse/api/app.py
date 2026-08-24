"""Thin HTTP API for the timeline and interruptible knowledge-base dialogue."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Sequence
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from langgraph.types import Command
from pydantic import BaseModel, Field, field_validator, model_validator

from research_pulse.knowledge.reader import KnowledgeReader
from research_pulse.review_drafts import ReviewDraftApprover, ReviewDraftError, ReviewDraftStore
from research_pulse.topics.contracts import ActiveRunConflict, RunNotFoundError, TopicNotFoundError
from research_pulse.topics.models import TopicValidationError
from research_pulse.topics.service import TopicRunService
from research_pulse.scheduling import SchedulerStatus


class ChatRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2_000)
    domain: str | None = Field(default=None, max_length=120)
    knowledge_ids: list[str] = Field(default_factory=list, max_length=20)
    thread_id: str | None = Field(default=None, max_length=200)


class ResumeRequest(BaseModel):
    approved: bool


class CreateTopicRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=500)

    @field_validator("name", "query")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class UpdateTopicRequest(BaseModel):
    enabled: bool | None = None
    daily_limit: int | None = Field(default=None, ge=1, le=3)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateTopicRequest":
        if self.enabled is None and self.daily_limit is None:
            raise ValueError("at least one setting is required")
        return self


def create_app(
    *,
    interactive_graph: Any,
    knowledge_reader: KnowledgeReader,
    topic_service: TopicRunService | None = None,
    scheduler_status: Callable[[], SchedulerStatus] | None = None,
    review_store: ReviewDraftStore | None = None,
    review_approver: ReviewDraftApprover | None = None,
    lifespan: Any = None,
) -> FastAPI:
    """Create an app around a graph already configured with a checkpointer."""

    app = FastAPI(title="Research Pulse API", version="0.1.0", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/knowledge")
    def list_knowledge(limit: int = 30) -> dict[str, Sequence[dict[str, str]]]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        return {"items": [asdict(item) for item in knowledge_reader.recent(limit=limit)]}

    @app.get("/api/knowledge/{knowledge_id}/review-drafts")
    def list_review_drafts(knowledge_id: str) -> dict[str, Sequence[dict[str, Any]]]:
        store = _require_review_store(review_store)
        drafts = store.list_for_knowledge(knowledge_id)
        return {
            "items": [_review_summary(draft) for draft in drafts],
        }

    @app.get("/api/knowledge/{knowledge_id}/review-drafts/{draft_id}")
    def get_review_draft(knowledge_id: str, draft_id: str) -> dict[str, Any]:
        store = _require_review_store(review_store)
        draft = store.get(draft_id)
        if draft is None or draft.knowledge_id != knowledge_id:
            raise HTTPException(status_code=404, detail="review draft not found")
        return draft.to_payload()

    @app.post("/api/knowledge/{knowledge_id}/review-drafts/{draft_id}/reject")
    def reject_review_draft(knowledge_id: str, draft_id: str) -> dict[str, Any]:
        store = _require_review_store(review_store)
        draft = store.get(draft_id)
        if draft is None or draft.knowledge_id != knowledge_id:
            raise HTTPException(status_code=404, detail="review draft not found")
        try:
            return store.transition(draft_id, "rejected").to_payload()
        except ReviewDraftError as error:
            raise HTTPException(status_code=409, detail={"code": "review_draft_state_conflict", "message": str(error)}) from error

    @app.post("/api/knowledge/{knowledge_id}/review-drafts/{draft_id}/approve")
    def approve_review_draft(knowledge_id: str, draft_id: str) -> dict[str, Any]:
        store = _require_review_store(review_store)
        if review_approver is None:
            raise HTTPException(status_code=503, detail="review approval is unavailable")
        draft = store.get(draft_id)
        if draft is None or draft.knowledge_id != knowledge_id:
            raise HTTPException(status_code=404, detail="review draft not found")
        try:
            receipt = review_approver.approve(draft)
        except ReviewDraftError as error:
            raise HTTPException(status_code=409, detail={"code": "review_draft_state_conflict", "message": str(error)}) from error
        if receipt.status != "published":
            raise HTTPException(
                status_code=409,
                detail={"code": "quality_gate_blocked", "status": receipt.status, "reason": receipt.reason},
            )
        try:
            updated = store.transition(draft_id, "published")
        except ReviewDraftError as error:
            raise HTTPException(status_code=409, detail={"code": "review_draft_state_conflict", "message": str(error)}) from error
        return {"draft": updated.to_payload(), "receipt": asdict(receipt)}

    @app.get("/api/knowledge/{knowledge_id}")
    def get_knowledge(knowledge_id: str) -> dict[str, Any]:
        detail = knowledge_reader.get_current(knowledge_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        payload = asdict(detail)
        # Keep the pre-visuals response shape stable for old clients.
        for section in payload.get("reading_sections", []):
            if not section.get("visuals"):
                section.pop("visuals", None)
        return payload

    @app.get("/api/knowledge/{knowledge_id}/assets/{asset_path:path}")
    def get_knowledge_asset(knowledge_id: str, asset_path: str) -> FileResponse:
        reader = getattr(knowledge_reader, "read_asset", None)
        path = reader(knowledge_id, f"assets/{asset_path}") if callable(reader) else None
        if path is None:
            raise HTTPException(status_code=404, detail="knowledge asset not found")
        return FileResponse(path)

    @app.get("/api/research-topics")
    def list_research_topics() -> dict[str, Sequence[dict[str, Any]]]:
        service = _require_topic_service(topic_service)
        return {
            "items": [
                {
                    "topic": asdict(topic),
                    "latest_run": asdict(latest_run) if latest_run else None,
                }
                for topic, latest_run in service.list_topics()
            ]
        }

    @app.patch("/api/research-topics/{topic_id}")
    def update_research_topic(topic_id: str, request: UpdateTopicRequest) -> dict[str, Any]:
        service = _require_topic_service(topic_service)
        try:
            topic = service.update_topic_settings(
                topic_id,
                enabled=request.enabled,
                daily_limit=request.daily_limit,
            )
        except TopicNotFoundError as error:
            raise HTTPException(status_code=404, detail="research topic not found") from error
        except TopicValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(topic)

    @app.get("/api/scheduler")
    def get_scheduler_status() -> dict[str, Any]:
        status = scheduler_status() if scheduler_status else SchedulerStatus(False, "Asia/Shanghai", "08:00", None)
        return status.public_dict()

    @app.post("/api/research-topics", status_code=202)
    def create_research_topic(
        request: CreateTopicRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        service = _require_topic_service(topic_service)
        try:
            topic, run = service.create_topic(name=request.name, query=request.query)
        except TopicValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        background_tasks.add_task(service.execute, run.run_id)
        return {"topic": asdict(topic), "run": asdict(run)}

    @app.post("/api/research-topics/{topic_id}/runs", status_code=202)
    def retry_research_topic(
        topic_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        service = _require_topic_service(topic_service)
        try:
            run = service.retry(topic_id)
        except TopicNotFoundError as error:
            raise HTTPException(status_code=404, detail="research topic not found") from error
        except ActiveRunConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "active_run_exists", "run_id": error.active_run.run_id},
            ) from error
        background_tasks.add_task(service.execute, run.run_id)
        return asdict(run)

    @app.get("/api/production-runs/{run_id}")
    def get_production_run(run_id: str) -> dict[str, Any]:
        service = _require_topic_service(topic_service)
        try:
            return asdict(service.get_run(run_id))
        except RunNotFoundError as error:
            raise HTTPException(status_code=404, detail="production run not found") from error

    @app.post("/api/chat")
    def start_chat(request: ChatRequest) -> dict[str, Any]:
        thread_id = request.thread_id or f"chat:{uuid4()}"
        result = interactive_graph.invoke(
            {
                "query": request.query,
                "domain": request.domain,
                "knowledge_ids": request.knowledge_ids,
            },
            {"configurable": {"thread_id": thread_id}},
        )
        return _chat_result(thread_id, result)

    @app.post("/api/chat/{thread_id}/resume")
    def resume_chat(thread_id: str, request: ResumeRequest) -> dict[str, Any]:
        try:
            result = interactive_graph.invoke(
                Command(resume={"approved": request.approved}),
                {"configurable": {"thread_id": thread_id}},
            )
        except Exception as error:
            raise HTTPException(status_code=409, detail="conversation cannot be resumed") from error
        return _chat_result(thread_id, result)

    return app


def _require_topic_service(service: TopicRunService | None) -> TopicRunService:
    if service is None:
        raise HTTPException(status_code=503, detail="research topic service is unavailable")
    return service


def _require_review_store(store: ReviewDraftStore | None) -> ReviewDraftStore:
    if store is None:
        raise HTTPException(status_code=503, detail="review drafts are unavailable")
    return store


def _review_summary(draft: Any) -> dict[str, Any]:
    return {
        "draft_id": draft.draft_id,
        "knowledge_id": draft.knowledge_id,
        "knowledge_version": draft.knowledge_version,
        "title": draft.title,
        "domain": draft.domain,
        "status": draft.status,
        "source_urls": list(draft.source_urls),
        "quality_issues": [asdict(issue) for issue in draft.quality_issues],
        "evidence_boundary": draft.evidence_boundary,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


def _chat_result(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    interrupts = result.get("__interrupt__", ())
    if interrupts:
        interrupt = interrupts[0]
        payload = getattr(interrupt, "value", interrupt)
        return {"status": "needs_confirmation", "thread_id": thread_id, "interrupt": payload}
    hits = result.get("retrieved_chunks", [])
    citations = [
        {
            "knowledge_id": hit["knowledge_id"],
            "knowledge_version": hit["knowledge_version"],
            "anchor_id": hit.get("anchor_id"),
            "source_url": hit["source_url"],
            "claim_id": hit.get("claim_id"),
            "claim_type": hit.get("claim_type"),
            "source_anchors": hit.get("source_anchors", []),
        }
        for hit in hits
    ]
    return {
        "status": "completed",
        "thread_id": thread_id,
        "answer": result.get("answer", ""),
        "citations": citations,
    }
