"""Environment-wired development server factory for Research Pulse."""

from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any
from datetime import datetime
import os

from langgraph.checkpoint.memory import MemorySaver
from dotenv import load_dotenv

from research_pulse.api.app import create_app
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.production.adapters import (
    ArxivCandidateFinder,
    DeepSeekEntailmentJudge,
    DeepSeekStructuredExtractor,
    DEFAULT_DEEPSEEK_TEXT_MODEL,
    DoclingSourceParser,
    FilesystemKnowledgePublisher,
    PostgresProcessedPaperRegistry,
)
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


def create_runtime_app():
    """Uvicorn factory. DATABASE_URL is required; DeepSeek is optional until needed."""

    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
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

    return create_app(
        interactive_graph=interactive_graph,
        knowledge_reader=FilesystemKnowledgeReader(vault_root),
        topic_service=topic_service,
        scheduler_status=daily_scheduler.status,
        review_store=review_store,
        review_approver=ReviewProductionApprover(database_url, vault_root, model),
        lifespan=build_scheduler_lifespan(daily_scheduler, production_executor),
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
