"""Environment-wired development server factory for Research Pulse."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any
from datetime import UTC, datetime
import atexit
import os
import sys
import uuid

from langgraph.checkpoint.memory import MemorySaver
from dotenv import load_dotenv

from research_pulse.api.app import create_app
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.workbench.workspace import WorkspaceService
from research_pulse.workbench.hitl import HITLService
from research_pulse.workbench.workspace_json import WorkspaceJsonStore
from research_pulse.workbench.workspace_pipeline import build_workspace_pipeline
from research_pulse.workbench.workspace_tools import WorkspaceResearchTools
from research_pulse.production.adapters import (
    ArxivCandidateFinder,
    DeepSeekEntailmentJudge,
    DEFAULT_DEEPSEEK_TEXT_MODEL,
    DoclingSourceParser,
    FilesystemKnowledgePublisher,
    PostgresProcessedPaperRegistry,
)
try:
    from research_pulse.production.adapters import DeepSeekStructuredExtractor
except ImportError:
    DeepSeekStructuredExtractor = None  # legacy bundle pipeline is disabled
from research_pulse.production.pipeline import ProductionService
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.rag.answer import DeepSeekGroundedAnswerGenerator
from research_pulse.rag.postgres import PostgresResearchRAG
from research_pulse.review_drafts import PostgresReviewDraftStore, ReviewDraft, ReviewDraftApprover, ReviewDraftError
from research_pulse.production.reread import RereadProcessedPaperRegistry
from research_pulse.scheduling import DailyScheduler, ScheduleCoordinator, SchedulerConfig, build_scheduler_lifespan
from research_pulse.topics.postgres import PostgresTopicRepository
from research_pulse.topics.service import LangGraphProductionRunner, TopicRunService
from research_pulse.workflows.bridge import ProductionGraphSupplementer
from research_pulse.workflows.interactive import CitationOnlyAnswerGenerator, SupplementationResult, build_interactive_graph, InteractiveGraphDependencies
from research_pulse.workflows.production import ProductionGraphDependencies, build_production_graph
from research_pulse.production.mineru_api import MinerUApiConfig, MinerUApiParser
from research_pulse.workbench.models import ParseStatus
from research_pulse.workbench.paper_access import PaperAccessService
from research_pulse.workbench.paper_download import PublicPdfDownloader
from research_pulse.workbench.paper_ingest import PdfIngestStore
from research_pulse.workbench.paper_submission import PaperSubmissionService
from research_pulse.workbench.preparation import PaperPreparationService, PreparationQueue
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository, open_workbench_database
from research_pulse.workbench.supporting_paper import SupportingPaperImporter
from langchain_openai import ChatOpenAI
from research_pulse.traceable_reading.service import TraceableReadingService
from research_pulse.production.reading import DeepSeekPaperReadingModel
from research_pulse.workbench.note_events import NoteRunEventProjector
from research_pulse.workbench.note_pipeline import ObservedReadingProvider
from research_pulse.workbench.arxiv_mcp import ArxivMCPClient, free_port
from research_pulse.workbench.chat import ChatService
from research_pulse.workbench.chat_model import OpenAICompatibleChatModel
from research_pulse.workbench.context_resolution import ContextResolver
from research_pulse.workbench.deepagents_v0 import DeepAgentsV0Runtime
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.attempt_worker import AttemptWorker
from research_pulse.workbench.cancellation import CancellationRegistry
from research_pulse.workbench.deepagents_kernel import DeepAgentsKernelAdapter
from research_pulse.workbench.run_coordinator import ResearchRunCoordinator
from research_pulse.workbench.note_runs import NoteRunService
from research_pulse.workbench.prompt_budget import PromptBudgeter
from research_pulse.workbench.research_tools import ManagedSource, ReadOnlyResearchTools
from research_pulse.workbench.session_workspace import SessionWorkspaceManager
from research_pulse.workbench.web_retrieval import build_env_web_search_provider
from research_pulse.workbench.capability_router import CapabilityRouter
from research_pulse.workbench.turn_adapters import BackgroundTurnAdapter, DurableTurnAdapter, SynchronousTurnAdapter
from research_pulse.workbench.turn_runtime import (
    default_capability_registry,
    experimental_research_mode_enabled,
    research_provider_output_limit,
)
from research_pulse.workbench.turn_service import TurnRuntime


@dataclass
class LazyProductionSupplementer:
    """Avoid requiring a DeepSeek key until the user actually approves supplementation."""

    database_url: str
    vault_root: Path
    model: str
    _delegate: ProductionGraphSupplementer | None = None
    _lock: Lock = field(default_factory=Lock)

    def supplement(self, *, query: str, domain: str | None, reason: str) -> SupplementationResult:
        with self._lock:
            if self._delegate is None:
                self._delegate = _build_production_supplementer(
                    database_url=self.database_url,
                    vault_root=self.vault_root,
                    model=self.model,
                )
        return self._delegate.supplement(query=query, domain=domain, reason=reason)


@dataclass
class LazyTopicProductionRunner:
    """Build heavyweight PDF/model adapters only after an actual topic run starts."""

    database_url: str
    vault_root: Path
    model: str
    _delegate: LangGraphProductionRunner | None = None
    _lock: Lock = field(default_factory=Lock)

    def run(
        self,
        *,
        run_id: str,
        topic: str,
        domain: str,
        limit: int,
        window_start: datetime | None,
        window_end: datetime,
    ) -> dict[str, Any]:
        with self._lock:
            if self._delegate is None:
                self._delegate = LangGraphProductionRunner(
                    _build_configured_production_graph(
                        database_url=self.database_url,
                        vault_root=self.vault_root,
                        model=self.model,
                    )
                )
        return self._delegate.run(
            run_id=run_id,
            topic=topic,
            domain=domain,
            limit=limit,
            window_start=window_start,
            window_end=window_end,
        )


@dataclass
class ReviewProductionApprover(ReviewDraftApprover):
    database_url: str
    vault_root: Path
    model: str

    def approve(self, draft: ReviewDraft):
        if draft.status != "needs_review":
            raise ReviewDraftError("Only a needs_review draft can be approved.")
        try:
            registry = PostgresProcessedPaperRegistry(self.database_url)
            registry.initialize()
            delegate = RereadProcessedPaperRegistry(registry, target_source_id=draft.source_id)
            rag = PostgresResearchRAG(self.database_url)
            rag.initialize()
            review_store = PostgresReviewDraftStore(database_url=self.database_url, vault_root=self.vault_root)
            review_store.initialize()
            service = ProductionService(
                parser=DoclingSourceParser(),
                extractor=DeepSeekStructuredExtractor.from_environment(model=self.model),
                publisher=FilesystemKnowledgePublisher(
                    vault_root=self.vault_root,
                    rag=rag,
                    processed_registry=registry,
                ),
                processed_registry=delegate,
                entailment_judge=DeepSeekEntailmentJudge.from_environment(model=self.model),
                run_deadline_seconds=float(os.getenv("DEEPSEEK_RUN_DEADLINE_SECONDS", "420")),
                review_sink=review_store,
            )
            return service.process(
                PaperCandidate(
                    source_id=draft.source_id,
                    title=draft.title,
                    source_url=draft.source_urls[0],
                    domain=draft.domain,
                )
            )
        except ReviewDraftError:
            raise
        except Exception as error:
            raise ReviewDraftError("review approval could not start") from error


class _ApproximateTokenCodec:
    def count(self, text: str) -> int: return max(1, len(text.encode("utf-8")) // 3)
    def truncate(self, text: str, max_tokens: int) -> str: return text[: max_tokens * 3]


def create_runtime_app():
    """Uvicorn factory. DATABASE_URL is required; DeepSeek is optional until needed."""

    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    # The test switch is explicit and process-scoped.  It removes the
    # application budget ceiling for research runs, while provider output,
    # wall-clock timeout and safety guards remain active.
    experimental_mode = experimental_research_mode_enabled()
    research_output_limit = research_provider_output_limit(
        experimental_mode=experimental_mode
    )
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. See .env.example for the local development value.")
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_TEXT_MODEL)
    vault_root = project_root / "knowledge"
    rag = PostgresResearchRAG(database_url)
    rag.initialize()
    registry = PostgresProcessedPaperRegistry(database_url)
    registry.initialize()
    topic_repository = PostgresTopicRepository(database_url)
    topic_repository.initialize()
    topic_repository.reconcile_active_runs()
    api_answerer = _answer_generator(model)
    interactive_graph = build_interactive_graph(
        InteractiveGraphDependencies(
            rag=rag,
            supplementer=LazyProductionSupplementer(database_url, vault_root, model),
            answer_generator=api_answerer,
        ),
        checkpointer=MemorySaver(),
    )
    topic_runner = (
        LazyTopicProductionRunner(database_url, vault_root, model)
        if os.getenv("DEEPSEEK_API_KEY")
        else None
    )
    topic_service = TopicRunService(topic_repository, runner=topic_runner)
    scheduler_config = SchedulerConfig.from_environment(os.environ)
    production_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-pulse-production")
    coordinator = ScheduleCoordinator(
        topic_service,
        submitter=lambda run_id: production_executor.submit(topic_service.execute, run_id),
    )
    daily_scheduler = DailyScheduler(scheduler_config, coordinator)
    review_store = PostgresReviewDraftStore(database_url=database_url, vault_root=vault_root)
    review_store.initialize()
    workbench_root = Path(os.getenv("WORKBENCH_DATA_ROOT", str(project_root / "data" / "workbench")))
    workbench_repository = SQLiteWorkbenchRepository(open_workbench_database(
        Path(os.getenv("WORKBENCH_DATABASE_PATH", str(workbench_root / "workbench.sqlite3")))
    ))
    workbench_repository.recover_interrupted_background_turns()
    # A process restart invalidates every in-memory worker, even when its
    # five-minute lease has not expired yet.  Reconcile those rows immediately
    # so the UI exposes a continuable ``abandoned`` Attempt instead of a false
    # ``running`` state until the lease timeout elapses.
    workbench_repository.abandon_running_attempts(now=datetime.now(UTC))
    workbench_sessions = SessionService(workbench_repository)
    ingest_store = PdfIngestStore(workbench_root / "layer-c")
    mineru_token = os.getenv("MINERU_API_TOKEN", "").strip()
    parser = MinerUApiParser(MinerUApiConfig(
        token=mineru_token or "not-configured",
        base_url=os.getenv("MINERU_API_BASE_URL", "https://mineru.net/api"),
        model_version=os.getenv("MINERU_MODEL_VERSION", "vlm"),
    ))
    preparation_queue = PreparationQueue(parser, workbench_repository)
    preparation = PaperPreparationService(
        workbench_repository, preparation_queue,
        material_cache_root=workbench_root / "mineru",
    )
    paper_access = PaperAccessService(
        workbench_repository, workbench_sessions, preparation_queue,
        layer_c_root=workbench_root / "layer-c",
        material_cache_root=workbench_root / "mineru",
    )
    paper_submission = PaperSubmissionService(
        ingest_store, preparation, paper_access, PublicPdfDownloader(ingest_store)
    )

    # --- parse drain --------------------------------------------------------
    # The PreparationQueue is drained by run_next() calls; the import path
    # enqueues but nothing ever drains it, so imported papers sat 'queued'
    # forever. Drain on a background daemon thread: one at a time (remote
    # parser), kicked on every new enqueue and once at boot for rows left
    # queued by earlier processes.
    _drain_state = {"running": False, "wake": False}
    _drain_lock = Lock()

    def kick_parse_drain() -> None:
        # Imports can arrive from several request threads.  Mark a wake-up
        # while holding the lock so an enqueue that races with an empty queue
        # check cannot leave the new paper stuck in ``queued`` forever.
        with _drain_lock:
            _drain_state["wake"] = True
            if _drain_state["running"]:
                return
            _drain_state["running"] = True

        def drain() -> None:
            try:
                while True:
                    with _drain_lock:
                        _drain_state["wake"] = False
                    while preparation_queue.run_next() is not None:
                        pass  # parser is remote/slow; drain the backlog sequentially
                    with _drain_lock:
                        if _drain_state["wake"]:
                            continue
                        _drain_state["running"] = False
                        return
            finally:
                with _drain_lock:
                    _drain_state["running"] = False

        import threading

        threading.Thread(target=drain, name="prep-parse-drain", daemon=True).start()

    # Published notes created by the note-only reader used to stop at the
    # knowledge vault.  Reuse the same downloader/preparation seam for an
    # explicit note -> workbench import, so a note never creates a second
    # paper pipeline or a phantom database row.
    note_paper_importer = SupportingPaperImporter(
        preparation=preparation,
        downloader=paper_submission.downloader,
        after_enqueue=kick_parse_drain,
    )

    def requeue_interrupted_parses() -> int:
        """Re-enqueue papers whose parse never finished in an earlier process
        (the in-memory queue lost them on restart)."""
        papers = workbench_repository.list_papers_by_parse_status(["queued", "parsing"])
        requeued = 0
        for paper in papers:
            if not paper.pdf_path or not Path(paper.pdf_path).is_file():
                continue
            preparation_queue.enqueue(
                paper.paper_id,
                pdf_path=Path(paper.pdf_path),
                source_url=paper.source_url or "",
                material_root=Path(paper.material_root) if paper.material_root else Path(workbench_root) / "mineru" / paper.paper_id / "material",
            )
            requeued += 1
        return requeued

    # Requeue + drain any parses left behind by earlier process generations,
    # then let later import_paper calls kick the drain again.
    requeue_interrupted_parses()
    kick_parse_drain()
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip() or "not-configured"
    # arXiv MCP is external, best-effort, and only used by search_arxiv (and not
    # at all by paper-local runs). Start it LAZILY on first search so the uvx
    # first-run download / health-wait never blocks app startup.
    # Dynamic port: killed backends may leave a stray arxiv-mcp-server holding
    # the fixed port, which would make the next lazy start fail its health wait.
    arxiv_client = ArxivMCPClient(port=free_port())
    atexit.register(arxiv_client.stop)
    # Web retrieval is optional and uses DeepSeek's server-side web_search.
    # Without a configured DeepSeek key, web_lookup runs fail closed instead
    # of falling back to arXiv or silently returning an empty result.
    web_search_provider = build_env_web_search_provider()
    chat_service = ChatService(
        workbench_repository, workbench_sessions, paper_access,
        ContextResolver(max_blocks=40),
        PromptBudgeter(_ApproximateTokenCodec(), context_window_tokens=32000, reserved_output_tokens=2000),
        OpenAICompatibleChatModel(model=model, api_key=deepseek_key, base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")),
        message_id_factory=lambda: __import__("uuid").uuid4().hex,
        clock=datetime.now,
    )
    # Paper selection answers acknowledge immediately and finish in a small
    # bounded pool.  Research/web runs keep their existing durable worker
    # lifecycle; this pool is intentionally separate so a slow provider call
    # cannot occupy the API request thread or starve exploration attempts.
    paper_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="research-pulse-paper")
    atexit.register(paper_executor.shutdown, wait=False, cancel_futures=True)
    attempt_cancellation = CancellationRegistry()
    exploration_service = ExplorationService(
        workbench_repository, runtime=None, cancellation_registry=attempt_cancellation
    )  # type: ignore[arg-type]

    def logical_exploration_run(execution_id: str):
        run = workbench_repository.get_exploration_run(execution_id)
        if run is not None:
            return run
        attempt = workbench_repository.get_coordinated_attempt(execution_id)
        return (
            workbench_repository.get_exploration_run(attempt.run_id)
            if attempt is not None else None
        )

    def exploration_tools(run_id, status_reader):
        run = logical_exploration_run(run_id)
        sources = []
        if run is not None:
            for paper_id in workbench_repository.list_paper_ids(run.session_id):
                paper = workbench_repository.get_paper(paper_id)
                if not paper or not paper.material_root:
                    continue
                if paper.parse_status != ParseStatus.READY:
                    continue
                blocks_path = Path(paper.material_root) / "blocks.jsonl"
                # A paper that was enqueued but not yet parsed has material_root
                # set but no blocks on disk; exposing it would crash the run with
                # FileNotFoundError on the first read. Gate on blocks actually
                # being present so the assistant reads only ready material.
                if not blocks_path.is_file():
                    continue
                sources.append(ManagedSource(
                    paper.paper_id, paper.source_url or paper.paper_id,
                    paper.source_url or f"urn:sha256:{paper.paper_id}",
                    blocks_path,
                ))
        return ReadOnlyResearchTools(
            sources,
            run_id=run_id,
            status_reader=status_reader,
            arxiv_client=arxiv_client,
            web_search_provider=web_search_provider,
        )

    exploration_runtime = DeepAgentsV0Runtime(
        model_factory=lambda guard: ChatOpenAI(model=model, api_key=deepseek_key, base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"), temperature=0, max_completion_tokens=research_output_limit, max_retries=1, timeout=60),
        tools_factory=exploration_tools,
        web_search_provider=web_search_provider,
    )
    exploration_service.runtime = exploration_runtime

    # Research-assistant profile: workspace-backed DeepAgents with file tools +
    # planning. Runs through the same ExplorationRun lifecycle (profile decides
    # the tool surface); workspace files live under the session directory.
    workbench_workspaces = SessionWorkspaceManager(workbench_root)

    def resource_workspace_id(session_id: str) -> str | None:
        """Return the session's real RESEARCH-WORKSPACE id (never derived from
        session_id). Legacy sessions without a binding get one allocated and
        persisted lazily (a real uuid, not ``ws-{session_id}``)."""
        session = workbench_repository.get(session_id)
        if session is None:
            return None
        if session.workspace_id:
            workbench_repository.ensure_workspace(session.workspace_id)
            return session.workspace_id
        workspace_id = uuid.uuid4().hex
        workbench_repository.save(replace(session, workspace_id=workspace_id))
        workbench_repository.ensure_workspace(workspace_id)
        return workspace_id

    def assistant_workspace_resolver(run_id: str) -> str | None:
        run = logical_exploration_run(run_id)
        if run is None:
            return None
        workspace_id = resource_workspace_id(run.session_id)
        if workspace_id is None:
            return None
        # Freeform notes live in this session's isolated state subfolder, so
        # several threads can share one workspace without clobbering each other.
        return workbench_workspaces.session_state_dir(workspace_id, run.session_id)

    def assistant_workspace_tools_factory(run_id: str) -> WorkspaceResearchTools | None:
        """Bind the run's session to a controlled workspace state store.

        RESOURCE scope is the workspace (shared papers/notes: ``resolve``), but
        the M3 research state (research map / subquestions / evidence / gate /
        commit) stays at the per-session level (Session ≈ ResearchRun) so a
        workspace can hold several independent threads. The state JSON lives in
        the session's subfolder, not the shared workspace root.
        """
        run = logical_exploration_run(run_id)
        if run is None:
            return None
        workspace_id = resource_workspace_id(run.session_id)
        if workspace_id is None:
            return None
        try:
            workspace_dir = workbench_workspaces.session_state_dir(workspace_id, run.session_id)
        except Exception:
            return None
        store = WorkspaceJsonStore(workspace_dir)
        workspace_pipeline = build_workspace_pipeline(store, workspace_root=workspace_dir)
        service = WorkspaceService(store, workspace_id_factory=lambda: workspace_id)
        try:
            service.get(workspace_id)
        except Exception:
            # Build a real workspace from the run's question and the session's
            # anchor paper (V1 single-paper). If no anchor is attached yet, the
            # binding is deferred rather than faking a workspace.
            first_paper = workbench_repository.list_paper_ids(run.session_id)[:1]
            anchor_paper_id = first_paper[0] if first_paper else None
            if anchor_paper_id is None:
                return None
            try:
                service.create(
                    research_question=run.question_snapshot or "未命名研究问题",
                    research_intent=run.question_snapshot or "未命名研究问题",
                    anchor_paper_id=anchor_paper_id,
                )
            except Exception:
                return None
        return WorkspaceResearchTools(
            service, workspace_id,
            gate=workspace_pipeline.gate,
            risk_classifier=workspace_pipeline.risk_classifier,
            commit_service=workspace_pipeline.commit_service,
            hitl_service=workspace_pipeline.hitl_service,
            # ``run_id`` at this seam is the physical Attempt id (the kernel
            # deliberately gives the legacy runtime an attempt-scoped id for
            # event isolation).  HITL decision points belong to the durable
            # logical ExplorationRun, however, so persist that stable id in
            # provenance instead of leaking an attempt id into the workspace
            # decision record.
            run_id=run.run_id,
            # Evidence governance: the agent never downloads/parses papers on
            # its own. Search candidates are surfaced to the user, who decides
            # whether to add them (＋ → async download+parse). No importer is
            # bound, so import_supporting_paper stays absent from the surface.
            importer=None,
            paper_linker=lambda paper_id: workbench_repository.link_paper(run.session_id, paper_id),
        )

    assistant_exploration_service = ExplorationService(
        workbench_repository, runtime=None, cancellation_registry=attempt_cancellation
    )  # type: ignore[arg-type]
    assistant_runtime = DeepAgentsV0Runtime(
        model_factory=lambda guard: ChatOpenAI(model=model, api_key=deepseek_key, base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"), temperature=0, max_completion_tokens=research_output_limit, max_retries=1, timeout=120),
        tools_factory=exploration_tools,
        assistant=True,
        workspace_resolver=assistant_workspace_resolver,
        workspace_tools_factory=assistant_workspace_tools_factory,
        web_search_provider=web_search_provider,
    )
    capability_registry = default_capability_registry(
        experimental_mode=experimental_mode
    )
    turn_runtime = TurnRuntime(
        router=CapabilityRouter(capability_registry),
        synchronous=SynchronousTurnAdapter(chat_service),
        background=BackgroundTurnAdapter(chat_service, executor=paper_executor),
        durable=DurableTurnAdapter(
            assistant_exploration_service,
            capability_registry,
        ),
        audit_repository=workbench_repository,
        available_capabilities={
            "basic", "paper", "research",
            *( {"web"} if web_search_provider is not None else set() ),
        },
    )
    attempt_worker = AttemptWorker(
        workbench_repository,
        ResearchRunCoordinator(
            workbench_repository, cancellation_registry=attempt_cancellation
        ),
        DeepAgentsKernelAdapter({
            "literature": exploration_runtime,
            "assistant": assistant_runtime,
        }),
        cancellation_registry=attempt_cancellation,
    )
    note_events = NoteRunEventProjector(workbench_repository)
    note_service = NoteRunService(
        workbench_repository, TraceableReadingService(),
        vault_root=vault_root, cache_root=workbench_root / "mineru",
        events=note_events,
        reader=FilesystemKnowledgeReader(vault_root),
    )
    # Observe the traceable reading provider so each (operation, usage) is
    # projected as a sanitised note-run event; the parallel production/reading
    # refactor is never touched.
    note_service.pipeline = TraceableReadingService(
        provider_factory=lambda: ObservedReadingProvider(
            DeepSeekPaperReadingModel.from_environment(), note_service._emit_operation
        )
    )

    def workspace_service_for(session_id: str) -> WorkspaceService | None:
        workspace_id = resource_workspace_id(session_id)
        if workspace_id is None:
            return None
        try:
            workspace_dir = workbench_workspaces.session_state_dir(workspace_id, session_id)
        except Exception:
            return None
        store = WorkspaceJsonStore(workspace_dir)
        service = WorkspaceService(store, workspace_id_factory=lambda: workspace_id)
        try:
            service.get(workspace_id)
        except Exception:
            return None
        return service

    def workspace_decision_for(session_id: str) -> HITLService | None:
        workspace_id = resource_workspace_id(session_id)
        if workspace_id is None:
            return None
        try:
            workspace_dir = workbench_workspaces.session_state_dir(workspace_id, session_id)
        except Exception:
            return None
        store = WorkspaceJsonStore(workspace_dir)
        try:
            return build_workspace_pipeline(store, workspace_root=workspace_dir).hitl_service
        except Exception:
            return None

    def cleanup_session_workspace(session_id: str, workspace_id: str | None = None) -> None:
        # Called AFTER the session row is deleted, so the API route passes the
        # workspace_id captured pre-delete; the DB lookup is only a fallback
        # for legacy single-argument callers (row gone -> no-op).
        if not workspace_id:
            session = workbench_repository.get(session_id)
            workspace_id = session.workspace_id if session else None
        if workspace_id:
            workbench_workspaces.remove_session(workspace_id, session_id)

    def cleanup_workspace(workspace_id: str) -> None:
        workbench_workspaces.remove_workspace(workspace_id)

    return create_app(
        interactive_graph=interactive_graph,
        knowledge_reader=FilesystemKnowledgeReader(vault_root),
        topic_service=topic_service,
        scheduler_status=daily_scheduler.status,
        review_store=review_store,
        review_approver=ReviewProductionApprover(database_url, vault_root, model),
        lifespan=build_scheduler_lifespan(daily_scheduler, production_executor),
        workbench_session_service=workbench_sessions,
        workbench_paper_service=paper_access,
        workbench_paper_submission_service=paper_submission,
        workbench_paper_importer=note_paper_importer,
        workbench_chat_service=chat_service,
        workbench_exploration_service=exploration_service,
        workbench_assistant_exploration_service=assistant_exploration_service,
        workbench_turn_runtime=turn_runtime,
        workbench_attempt_worker=attempt_worker,
        workbench_note_run_service=note_service,
        workbench_session_workspace_cleanup=cleanup_session_workspace,
        workbench_workspace_cleanup=cleanup_workspace,
        workbench_workspace_service_provider=workspace_service_for,
        workbench_workspace_decision_provider=workspace_decision_for,
        workbench_session_workspace_resolver=resource_workspace_id,
    )


def _answer_generator(model: str):
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if api_key:
        return DeepSeekGroundedAnswerGenerator(
            api_key=api_key,
            model=model,
            max_tokens=int(os.getenv("DEEPSEEK_ANSWER_MAX_TOKENS", "1200")),
        )
    return CitationOnlyAnswerGenerator()


def _build_production_supplementer(*, database_url: str, vault_root: Path, model: str) -> ProductionGraphSupplementer:
    graph = _build_configured_production_graph(database_url=database_url, vault_root=vault_root, model=model)
    return ProductionGraphSupplementer(production_graph=graph, limit=2)


def _build_configured_production_graph(*, database_url: str, vault_root: Path, model: str):
    extractor = DeepSeekStructuredExtractor.from_environment(model=model)
    judge = DeepSeekEntailmentJudge.from_environment(model=model)
    rag = PostgresResearchRAG(database_url)
    registry = PostgresProcessedPaperRegistry(database_url)
    review_sink = PostgresReviewDraftStore(database_url=database_url, vault_root=vault_root)
    review_sink.initialize()
    service = ProductionService(
        parser=DoclingSourceParser(),
        extractor=extractor,
        publisher=FilesystemKnowledgePublisher(vault_root=vault_root, rag=rag, processed_registry=registry),
        processed_registry=registry,
        entailment_judge=judge,
        run_deadline_seconds=float(os.getenv("DEEPSEEK_RUN_DEADLINE_SECONDS", "420")),
        review_sink=review_sink,
    )
    graph = build_production_graph(
        ProductionGraphDependencies(candidate_finder=ArxivCandidateFinder(), production_service=service)
    )
    return graph
