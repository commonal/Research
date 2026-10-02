"""Thin HTTP API for the timeline and interruptible knowledge-base dialogue."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Callable, Sequence
from uuid import uuid4
import re

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
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.paper_access import PaperAccessService
from research_pulse.workbench.paper_submission import PaperSubmissionService
from research_pulse.workbench.supporting_paper import SupportingPaperImporter, SupportingPaperImportError
from research_pulse.workbench.chat import ChatService
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.note_runs import NoteRunService


class ChatRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2_000)
    domain: str | None = Field(default=None, max_length=120)
    knowledge_ids: list[str] = Field(default_factory=list, max_length=20)
    thread_id: str | None = Field(default=None, max_length=200)


class ResumeRequest(BaseModel):
    approved: bool


class CreateKnowledgeWorkbenchSessionRequest(BaseModel):
    """Optional destination when a published paper note enters the workbench."""

    workspace_id: str | None = Field(default=None, max_length=240)


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
    interactive_graph: Any = None,
    knowledge_reader: KnowledgeReader,
    topic_service: TopicRunService | None = None,
    scheduler_status: Callable[[], SchedulerStatus] | None = None,
    review_store: ReviewDraftStore | None = None,
    review_approver: ReviewDraftApprover | None = None,
    lifespan: Any = None,
    on_topic_created: Callable[[str, str], None] | None = None,
    workbench_session_service: SessionService | None = None,
    workbench_paper_service: PaperAccessService | None = None,
    workbench_paper_submission_service: PaperSubmissionService | None = None,
    workbench_paper_importer: SupportingPaperImporter | None = None,
    workbench_chat_service: ChatService | None = None,
    workbench_exploration_service: ExplorationService | None = None,
    workbench_note_run_service: NoteRunService | None = None,
    workbench_assistant_exploration_service: ExplorationService | None = None,
    workbench_turn_runtime: Any = None,
    workbench_attempt_worker: Any = None,
    workbench_session_workspace_cleanup: Callable[[str, str | None], None] | None = None,
    workbench_workspace_cleanup: Callable[[str], None] | None = None,
    workbench_workspace_service_provider: Callable[[str], "WorkspaceService | None"] | None = None,
    workbench_workspace_decision_provider: Callable[[str], "HITLService | None"] | None = None,
    workbench_session_workspace_resolver: Callable[[str], str | None] | None = None,
) -> FastAPI:
    """Create an app around a graph already configured with a checkpointer.

    ``interactive_graph`` is optional: a note-only deployment (see
    ``note_only.create_note_only_app``) serves the knowledge timeline and detail
    endpoints straight from the file vault and returns 503 for features it does
    not back (chat, research topics, scheduler, review).  The frontend already
    degrades those 503s gracefully.

    ``on_topic_created`` is an optional side-effect hook invoked with
    ``(name, query)`` after a research topic is created; the note-only
    deployment wires it to keep the scout agent interest profile in sync.
    """

    if workbench_attempt_worker is not None:
        upstream_lifespan = lifespan

        @asynccontextmanager
        async def worker_lifespan(app):
            workbench_attempt_worker.start()
            try:
                if upstream_lifespan is None:
                    yield
                else:
                    async with upstream_lifespan(app):
                        yield
            finally:
                workbench_attempt_worker.stop()

        lifespan = worker_lifespan
    app = FastAPI(title="Research Pulse API", version="0.1.0", lifespan=lifespan)

    def _source_key(value: object) -> str:
        """Normalize paper and knowledge source identities for a read join."""
        if not isinstance(value, str):
            return ""
        normalized = value.strip().lower().split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if normalized.startswith("urn:"):
            normalized = normalized.removeprefix("urn:")
        if normalized.startswith("arxiv:"):
            return normalized
        match = re.search(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", normalized, re.IGNORECASE)
        if match:
            return f"arxiv:{match.group(1)}"
        return normalized

    def _published_note_for_paper(paper: Any) -> tuple[str, str] | None:
        """Join a prepared paper to its current published knowledge note."""
        paper_keys = {
            key
            for key in (
                _source_key(getattr(paper, "source_identity", None)),
                _source_key(getattr(paper, "source_url", None)),
            )
            if key
        }
        repository = getattr(workbench_paper_service, "repository", None)
        aliases = getattr(repository, "list_paper_sources", None)
        if callable(aliases):
            try:
                paper_keys.update(
                    key for key in (_source_key(value) for value in aliases(paper.paper_id)) if key
                )
            except Exception:
                pass
        if not paper_keys:
            return None
        try:
            summaries = knowledge_reader.recent(limit=100)
        except Exception:
            return None
        for summary in summaries:
            if _source_key(getattr(summary, "source_url", None)) in paper_keys:
                return (summary.knowledge_id, "published")
            # A published note may list several sources while the summary
            # read model intentionally exposes only its first URL.  Resolve
            # the complete detail lazily so the paper projection remains
            # correct for multi-source notes without widening the summary DTO.
            try:
                detail = knowledge_reader.get_current(summary.knowledge_id)
            except Exception:
                continue
            if detail is not None and any(
                _source_key(value) in paper_keys for value in getattr(detail, "source_urls", ())
            ):
                return (summary.knowledge_id, "published")
        return None
    if workbench_session_service is not None:
        from research_pulse.workbench.api import build_workbench_router, build_resource_workspace_router

        app.include_router(build_workbench_router(
            workbench_session_service,
            session_workspace_cleanup=workbench_session_workspace_cleanup,
        ))
        app.include_router(build_resource_workspace_router(
            workbench_session_service.repository,
            workspace_cleanup=workbench_workspace_cleanup,
        ))
    if workbench_paper_service is not None:
        from research_pulse.workbench.paper_api import build_workbench_paper_router

        app.include_router(build_workbench_paper_router(
            workbench_paper_service,
            submission=workbench_paper_submission_service,
            note_lookup=_published_note_for_paper,
        ))
    if workbench_chat_service is not None:
        from research_pulse.workbench.chat_api import build_workbench_chat_router

        app.include_router(build_workbench_chat_router(
            workbench_chat_service,
            exploration_service=workbench_exploration_service,
            assistant_exploration_service=workbench_assistant_exploration_service,
            turn_runtime=workbench_turn_runtime,
        ))
    if workbench_exploration_service is not None:
        from research_pulse.workbench.exploration_api import build_exploration_router

        app.include_router(build_exploration_router(
            workbench_exploration_service,
        ))
    if workbench_workspace_service_provider is not None:
        from research_pulse.workbench.workspace_api import build_workspace_router

        app.include_router(build_workspace_router(
            workbench_workspace_service_provider,
            decision_provider=workbench_workspace_decision_provider,
            session_workspace_resolver=workbench_session_workspace_resolver,
        ))
    if workbench_note_run_service is not None:
        from research_pulse.workbench.note_api import build_note_run_router

        app.include_router(build_note_run_router(workbench_note_run_service))

    if workbench_session_service is not None:
        from research_pulse.workbench.api import session_payload

        def _knowledge_source_alias(knowledge_id: str, source_urls: Sequence[str]) -> str | None:
            """Resolve the source identity used by the paper workbench.

            Published arXiv notes use ``kp:arxiv:<source_id>``.  The URL
            fallback keeps older bundles readable without teaching the UI to
            parse knowledge-id conventions.  Content-addressed uploads use a
            ``sha256:...`` source identity; generic public PDF/DOI URLs are
            resolved by the repository's URL lookup below.
            """
            def content_address(value: str) -> str | None:
                """Canonicalize hash forms emitted by older note producers.

                Some historical notes used ``kp:arxiv:<digest>`` even when
                the underlying paper was a local upload.  The workbench
                stores that paper under ``sha256:<digest>``.  A real arXiv
                identifier is returned unchanged, so this normalization does
                not affect normal arXiv imports.
                """
                normalized = value.strip().lower()
                if normalized.startswith("urn:"):
                    normalized = normalized.removeprefix("urn:")
                if normalized.startswith("sha256:"):
                    digest = normalized.removeprefix("sha256:")
                elif re.fullmatch(r"[0-9a-f]{64}", normalized):
                    digest = normalized
                else:
                    return None
                return f"sha256:{digest}" if re.fullmatch(r"[0-9a-f]{64}", digest) else None

            for prefix in ("kp:arxiv:", "kp:sha256:"):
                if knowledge_id.startswith(prefix):
                    value = knowledge_id.removeprefix(prefix).strip()
                    return content_address(value) or value or None
            for source_url in source_urls:
                match = re.search(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", source_url, re.IGNORECASE)
                if match:
                    return re.sub(r"\.pdf$", "", match.group(1), flags=re.IGNORECASE)
                normalized = content_address(source_url)
                if normalized:
                    return normalized
            return None

        repository = workbench_session_service.repository

        def _resolve_registered_paper(knowledge_id: str, source_urls: Sequence[str]) -> Any | None:
            """Join a published note to the workbench paper registry.

            This lookup is deliberately shared by the status, import, and
            hand-off routes.  Keeping one join prevents the UI from seeing a
            paper as importable in one endpoint and missing in another.
            """
            alias = _knowledge_source_alias(knowledge_id, source_urls)
            alias_candidates: list[str] = []
            if alias:
                alias_candidates.append(alias)
                if not alias.startswith(("arxiv:", "sha256:")):
                    alias_candidates.append(f"arxiv:{alias}")
                elif alias.startswith("sha256:"):
                    # Older content-addressed rows use the bare digest as
                    # paper_id; newer rows also keep the sha256 alias.
                    alias_candidates.append(alias.removeprefix("sha256:"))
            lookup = getattr(repository, "get_paper", None)
            if callable(lookup):
                for candidate in alias_candidates:
                    paper = lookup(candidate)
                    if paper is not None:
                        return paper
            alias_lookup = getattr(repository, "get_paper_by_source_alias", None)
            if callable(alias_lookup):
                for candidate in alias_candidates:
                    paper = alias_lookup(candidate)
                    if paper is not None:
                        return paper
            # Generic public PDF/DOI imports are content-addressed, so the
            # URL join is the final fallback for legacy notes.
            url_lookup = getattr(repository, "get_paper_by_source_url", None)
            if callable(url_lookup):
                for source_url in source_urls:
                    if not isinstance(source_url, str):
                        continue
                    candidate = source_url.strip()
                    if not candidate.lower().startswith(("http://", "https://")):
                        continue
                    paper = url_lookup(candidate)
                    if paper is not None:
                        return paper
            return None

        def _knowledge_import_candidate(knowledge_id: str, detail: Any) -> dict[str, object] | None:
            """Build the small candidate shape expected by the shared importer."""
            source_url = next(
                (
                    value.strip()
                    for value in getattr(detail, "source_urls", ())
                    if isinstance(value, str)
                    and value.strip().lower().startswith(("http://", "https://"))
                ),
                None,
            )
            if source_url is None:
                return None
            arxiv_match = re.search(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", source_url, re.IGNORECASE)
            arxiv_id = re.sub(r"\.pdf$", "", arxiv_match.group(1), flags=re.IGNORECASE) if arxiv_match else ""
            suffix = knowledge_id.removeprefix("kp:arxiv:").strip()
            return {
                "arxiv_id": arxiv_id,
                "source_id": arxiv_id or suffix or source_url,
                "url": source_url,
                "title": str(getattr(detail, "title", "") or "").strip(),
                "domain": str(getattr(detail, "domain", "research") or "research"),
            }

        def _open_registered_paper(paper: Any, workspace_id: str | None) -> dict[str, Any]:
            """Open/reuse a paper session without creating duplicate projects."""
            try:
                session = workbench_session_service.find_active_for_paper(paper.paper_id, workspace_id)
                if session is not None:
                    session = workbench_session_service.update_layout(
                        session.session_id,
                        paper_panel_open=True,
                        active_paper_id=paper.paper_id,
                        update_active_paper=True,
                    )
                else:
                    session = workbench_session_service.create_from_paper(paper.paper_id, workspace_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="paper not found") from error
            return session_payload(workbench_session_service, session)

        def _paper_state(paper: Any | None) -> str:
            if paper is None:
                return "not_registered"
            parse_status = getattr(getattr(paper, "parse_status", None), "value", getattr(paper, "parse_status", None))
            pdf_status = getattr(getattr(paper, "pdf_status", None), "value", getattr(paper, "pdf_status", None))
            if parse_status == "ready" and pdf_status == "ready":
                return "available"
            if parse_status == "failed" or pdf_status == "failed":
                return "failed"
            return "preparing"

        @app.get("/api/knowledge/{knowledge_id}/workbench-status")
        def knowledge_workbench_status(knowledge_id: str) -> dict[str, Any]:
            """Return whether a note can enter the paper workbench."""
            detail = knowledge_reader.get_current(knowledge_id)
            if detail is None:
                raise HTTPException(status_code=404, detail="knowledge not found")
            paper = _resolve_registered_paper(knowledge_id, detail.source_urls)
            candidate = _knowledge_import_candidate(knowledge_id, detail)
            state = _paper_state(paper)
            if paper is None and candidate is None:
                state = "unavailable"
            can_import = (
                candidate is not None
                and workbench_paper_importer is not None
                and (paper is None or state == "failed")
            )
            return {
                "knowledge_id": knowledge_id,
                "status": state,
                "can_import": can_import,
                "paper_id": getattr(paper, "paper_id", None),
                "title": getattr(paper, "title", None) or getattr(detail, "title", None),
                "source_url": getattr(paper, "source_url", None) or (candidate or {}).get("url"),
                "pdf_status": getattr(getattr(paper, "pdf_status", None), "value", getattr(paper, "pdf_status", None)) if paper else None,
                "parse_status": getattr(getattr(paper, "parse_status", None), "value", getattr(paper, "parse_status", None)) if paper else None,
            }

        @app.post("/api/knowledge/{knowledge_id}/workbench-import", status_code=202)
        def import_knowledge_workbench_session(
            knowledge_id: str,
            request: CreateKnowledgeWorkbenchSessionRequest | None = None,
        ) -> dict[str, Any]:
            """Import a note's source through the shared paper pipeline, then open it.

            Existing papers are never downloaded again.  A missing paper is
            imported only when the note has an HTTP(S) source and the runtime
            has the shared importer wired; otherwise the endpoint reports a
            safe, actionable error.
            """
            detail = knowledge_reader.get_current(knowledge_id)
            if detail is None:
                raise HTTPException(status_code=404, detail="knowledge not found")
            paper = _resolve_registered_paper(knowledge_id, detail.source_urls)
            if paper is None or _paper_state(paper) == "failed":
                candidate = _knowledge_import_candidate(knowledge_id, detail)
                if candidate is None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "knowledge_not_bound_to_paper",
                            "message": "这份笔记没有绑定可下载的论文来源。",
                        },
                    )
                if workbench_paper_importer is None:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "paper_import_unavailable",
                            "message": "当前运行模式未启用论文导入，请使用完整工作台服务。",
                        },
                    )
                try:
                    imported = workbench_paper_importer.import_paper(candidate)
                except SupportingPaperImportError as error:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "paper_import_failed", "message": str(error)},
                    ) from error
                paper = repository.get_paper(imported.source_id)
                if paper is None:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "code": "paper_import_incomplete",
                            "message": "论文已下载但未能登记到工作台，请稍后重试。",
                        },
                    )
            destination = request.workspace_id if request is not None else None
            return _open_registered_paper(paper, destination)

        @app.post("/api/knowledge/{knowledge_id}/workbench-session", status_code=201)
        def create_knowledge_workbench_session(
            knowledge_id: str,
            request: CreateKnowledgeWorkbenchSessionRequest | None = None,
        ) -> dict[str, Any]:
            """Open an existing, parsed paper from its published note.

            Import is intentionally explicit and lives in the separate
            ``workbench-import`` endpoint.  This route remains a fast,
            read-only hand-off for already prepared papers.
            """
            detail = knowledge_reader.get_current(knowledge_id)
            if detail is None:
                raise HTTPException(status_code=404, detail="knowledge not found")
            alias = _knowledge_source_alias(knowledge_id, detail.source_urls)
            if alias is None and not any(
                isinstance(source_url, str)
                and source_url.strip().lower().startswith(("http://", "https://"))
                for source_url in detail.source_urls
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "knowledge_not_bound_to_paper",
                        "message": "这份笔记没有绑定可定位的工作台论文。",
                    },
                )
            paper = _resolve_registered_paper(knowledge_id, detail.source_urls)
            if paper is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "paper_not_registered",
                        "message": "这份笔记对应的论文尚未进入工作台，请先完成论文导入。",
                    },
                )
            if _paper_state(paper) != "available":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "paper_not_ready",
                        "message": "论文仍在下载或解析，完成后才能从笔记进入工作台。",
                    },
                )
            destination = request.workspace_id if request is not None else None
            return _open_registered_paper(paper, destination)

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
        if on_topic_created is not None:
            try:
                on_topic_created(topic.name, topic.query)
            except Exception:
                # Profile sync is a best-effort side effect; a failure here must
                # not block the topic creation flow.
                pass
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
        if interactive_graph is None:
            raise HTTPException(status_code=503, detail="chat is unavailable in note-only mode")
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
        if interactive_graph is None:
            raise HTTPException(status_code=503, detail="chat is unavailable in note-only mode")
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
