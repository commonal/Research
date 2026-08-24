"""Explicit, cost-bounded acceptance of one live arXiv paper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4
import argparse
import os

from dotenv import load_dotenv
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from research_pulse.acceptance.models import (
    BrowserStatus,
    FINAL_STAGE_NAMES,
    FinalStatus,
    RealPaperAcceptanceReceipt,
    RunKind,
    StageReceipt,
    StageStatus,
)
from research_pulse.acceptance.orchestration import (
    CostBoundary,
    RejectingSupplementer,
    SingleCandidateFinder,
    invoke_production_once,
)
from research_pulse.acceptance.preflight import (
    AcceptanceConfig,
    EnvironmentBlocked,
    PreflightDependencies,
    default_database_probe,
    default_docling_probe,
    default_writable_probe,
    run_environment_preflight,
    select_unprocessed_candidate,
)
from research_pulse.acceptance.receipt import AcceptanceReceiptWriter, ReceiptPaths, safe_error
from research_pulse.acceptance.validation import (
    AcceptanceValidationError,
    BundleVerification,
    ChatVerification,
    RetrievalVerification,
    build_acceptance_app,
    snapshot_knowledge,
    snapshot_raw_material,
    verify_new_bundle,
    verify_no_new_raw_material,
    verify_postgres_retrieval,
    verify_reading_api,
    verify_scoped_chat,
)
from research_pulse.production.adapters import (
    ArxivCandidateFinder,
    DeepSeekEntailmentJudge,
    DeepSeekStructuredExtractor,
    DEFAULT_DEEPSEEK_TEXT_MODEL,
    DoclingSourceParser,
    FilesystemKnowledgePublisher,
    PostgresProcessedPaperRegistry,
)
from research_pulse.production.pipeline import PaperCandidate, ProductionService
from research_pulse.rag.answer import DeepSeekGroundedAnswerGenerator
from research_pulse.rag.postgres import PostgresResearchRAG
from research_pulse.workflows.interactive import InteractiveGraphDependencies, build_interactive_graph
from research_pulse.workflows.production import ProductionGraphDependencies, build_production_graph


DEFAULT_TOPIC = "LLM agent memory"
DEFAULT_DOMAIN = "llm_agent_memory"
DEFAULT_QUESTION = "这篇论文解决了什么问题，主要方法和实验结果是什么？"


@dataclass(frozen=True)
class RealAcceptanceDependencies:
    run_kind: RunKind
    preflight: PreflightDependencies
    registry: Any
    rag: Any
    production_graph_factory: Callable[[PaperCandidate], Any]
    interactive_graph_factory: Callable[[], Any]


@dataclass(frozen=True)
class AcceptanceRunResult:
    receipt: RealPaperAcceptanceReceipt
    paths: ReceiptPaths

    @property
    def exit_code(self) -> int:
        return 0 if self.receipt.is_real_core_pass else 1


class _StageLog:
    def __init__(self, started_at: str) -> None:
        self.started_at = started_at
        self.values: dict[str, StageReceipt] = {}

    def timed(self, name: str, operation: Callable[[], Any]) -> Any:
        started = _utc_now()
        clock = perf_counter()
        try:
            result = operation()
        except Exception:
            raise
        else:
            self.values[name] = StageReceipt(name, started, _elapsed_ms(clock), StageStatus.PASSED)
            return result

    def pass_stage(self, name: str, *, started: str, clock: float, metrics: dict[str, Any] | None = None) -> None:
        self.values[name] = StageReceipt(
            name,
            started,
            _elapsed_ms(clock),
            StageStatus.PASSED,
            metrics=metrics or {},
        )

    def fail_stage(
        self,
        name: str,
        *,
        status: StageStatus,
        error_code: str,
        summary: str,
        started: str | None = None,
        clock: float | None = None,
    ) -> None:
        self.values[name] = StageReceipt(
            name,
            started or _utc_now(),
            _elapsed_ms(clock) if clock is not None else 0,
            status,
            error_code=error_code,
            error_summary=summary[:240],
        )

    def finalize(self, *, receipt_passed: bool = True) -> tuple[StageReceipt, ...]:
        terminal_seen = False
        for name in FINAL_STAGE_NAMES[:-1]:
            existing = self.values.get(name)
            if existing is not None:
                if existing.status in {StageStatus.BLOCKED, StageStatus.FAILED}:
                    terminal_seen = True
                continue
            self.values[name] = StageReceipt(
                name,
                _utc_now(),
                0,
                StageStatus.SKIPPED if terminal_seen else StageStatus.PENDING,
            )
        self.values["receipt"] = StageReceipt(
            "receipt",
            _utc_now(),
            0,
            StageStatus.PASSED if receipt_passed else StageStatus.FAILED,
            error_code=None if receipt_passed else "receipt_write_failed",
            error_summary=None if receipt_passed else "验收回执写入失败。",
        )
        return tuple(self.values[name] for name in FINAL_STAGE_NAMES)


def run_real_acceptance(
    config: AcceptanceConfig,
    deps: RealAcceptanceDependencies,
    *,
    question: str = DEFAULT_QUESTION,
    run_id: str | None = None,
) -> AcceptanceRunResult:
    """Run one live paper through the existing product path and persist a safe receipt."""

    started_at = _utc_now()
    active_run_id = run_id or f"real-paper-{uuid4()}"
    stages = _StageLog(started_at)
    writer = AcceptanceReceiptWriter(config.receipt_root)
    budget = CostBoundary()
    source_id: str | None = None
    bundle_verification: BundleVerification | None = None
    retrieval_verification: RetrievalVerification | None = None
    chat_verification: ChatVerification | None = None
    before_knowledge = snapshot_knowledge(config.vault_root)
    managed_roots = (config.vault_root, _project_root() / "data", config.receipt_root)
    before_raw = snapshot_raw_material(managed_roots)
    final_status: FinalStatus

    preflight_started, preflight_clock = _utc_now(), perf_counter()
    try:
        preflight = run_environment_preflight(config, deps.preflight)
        stages.pass_stage(
            "preflight",
            started=preflight_started,
            clock=preflight_clock,
            metrics={"check_count": len(preflight.checks)},
        )
    except EnvironmentBlocked as error:
        stages.fail_stage(
            "preflight",
            status=StageStatus.BLOCKED,
            error_code=error.error_code,
            summary=str(error),
            started=preflight_started,
            clock=preflight_clock,
        )
        final_status = FinalStatus.ENVIRONMENT_BLOCKED
        return _write_result(
            writer=writer,
            stages=stages,
            config=config,
            run_id=active_run_id,
            started_at=started_at,
            final_status=final_status,
            run_kind=deps.run_kind,
            source_id=None,
        )

    discovery_started, discovery_clock = _utc_now(), perf_counter()
    try:
        candidate = select_unprocessed_candidate(preflight.candidates, deps.registry)
        source_id = candidate.source_id
        stages.pass_stage(
            "discovery",
            started=discovery_started,
            clock=discovery_clock,
            metrics={"candidate_pool_count": len(preflight.candidates), "selected_count": 1},
        )
    except EnvironmentBlocked as error:
        stages.fail_stage(
            "discovery",
            status=StageStatus.BLOCKED,
            error_code=error.error_code,
            summary=str(error),
            started=discovery_started,
            clock=discovery_clock,
        )
        return _write_result(
            writer=writer,
            stages=stages,
            config=config,
            run_id=active_run_id,
            started_at=started_at,
            final_status=FinalStatus.ENVIRONMENT_BLOCKED,
            run_kind=deps.run_kind,
            source_id=None,
        )

    production_started, production_clock = _utc_now(), perf_counter()
    try:
        graph = deps.production_graph_factory(candidate)
        result = invoke_production_once(
            graph,
            run_id=active_run_id,
            topic=config.topic,
            domain=config.domain,
            candidate=candidate,
            budget=budget,
        )
        verify_no_new_raw_material(before=before_raw, roots=managed_roots)
        production_receipt = result["receipts"][0]
        if production_receipt.get("status") != "published":
            reason = production_receipt.get("reason") or production_receipt.get("status") or "unknown"
            raise AcceptanceValidationError(f"Production did not publish the selected paper: {reason}")
        stages.pass_stage(
            "production",
            started=production_started,
            clock=production_clock,
            metrics={"published_count": 1},
        )
    except Exception as error:
        code, summary = safe_error(error, stage="production")
        stages.fail_stage(
            "production",
            status=StageStatus.FAILED,
            error_code=code,
            summary=summary,
            started=production_started,
            clock=production_clock,
        )
        return _write_result(
            writer=writer,
            stages=stages,
            config=config,
            run_id=active_run_id,
            started_at=started_at,
            final_status=FinalStatus.ACCEPTANCE_FAILED,
            run_kind=deps.run_kind,
            source_id=source_id,
        )

    bundle_started, bundle_clock = _utc_now(), perf_counter()
    try:
        bundle_verification = verify_new_bundle(
            vault_root=config.vault_root,
            before=before_knowledge,
            source_id=source_id,
        )
        verify_no_new_raw_material(before=before_raw, roots=managed_roots)
        stages.pass_stage(
            "bundle_verify",
            started=bundle_started,
            clock=bundle_clock,
            metrics={
                "source_fact_count": len(bundle_verification.source_fact_ids),
                "source_anchor_count": len(bundle_verification.source_anchor_ids),
            },
        )
    except Exception as error:
        return _failed_result(
            writer, stages, config, active_run_id, started_at, source_id,
            "bundle_verify", error, bundle_started, bundle_clock,
            run_kind=deps.run_kind,
        )

    retrieval_started, retrieval_clock = _utc_now(), perf_counter()
    try:
        retrieval_verification = verify_postgres_retrieval(deps.rag, bundle_verification)
        stages.pass_stage(
            "retrieval_verify",
            started=retrieval_started,
            clock=retrieval_clock,
            metrics={"chunk_count": len(retrieval_verification.chunk_ids)},
        )
    except Exception as error:
        return _failed_result(
            writer, stages, config, active_run_id, started_at, source_id,
            "retrieval_verify", error, retrieval_started, retrieval_clock,
            run_kind=deps.run_kind,
            bundle_verification=bundle_verification,
        )

    api_started, api_clock = _utc_now(), perf_counter()
    try:
        app = build_acceptance_app(
            interactive_graph=deps.interactive_graph_factory(),
            vault_root=config.vault_root,
        )
        with TestClient(app) as client:
            verify_reading_api(client, bundle_verification.bundle)
            chat_verification = verify_scoped_chat(
                client,
                query=question,
                bundle=bundle_verification.bundle,
                budget=budget,
            )
        verify_no_new_raw_material(before=before_raw, roots=managed_roots)
        stages.pass_stage(
            "api_chat_verify",
            started=api_started,
            clock=api_clock,
            metrics={"citation_count": len(chat_verification.citation_ids), "question_count": 1},
        )
    except Exception as error:
        return _failed_result(
            writer, stages, config, active_run_id, started_at, source_id,
            "api_chat_verify", error, api_started, api_clock,
            run_kind=deps.run_kind,
            bundle_verification=bundle_verification,
            retrieval_verification=retrieval_verification,
        )

    return _write_result(
        writer=writer,
        stages=stages,
        config=config,
        run_id=active_run_id,
        started_at=started_at,
        final_status=FinalStatus.PASSED,
        run_kind=deps.run_kind,
        source_id=source_id,
        bundle_verification=bundle_verification,
        retrieval_verification=retrieval_verification,
        chat_verification=chat_verification,
    )


def build_real_dependencies(config: AcceptanceConfig) -> RealAcceptanceDependencies:
    finder = ArxivCandidateFinder()
    database_url = config.database_url or ""
    registry = PostgresProcessedPaperRegistry(database_url)
    rag = PostgresResearchRAG(database_url)

    def production_factory(candidate: PaperCandidate):
        if not config.deepseek_api_key:
            raise RuntimeError("DeepSeek is not configured after preflight.")
        rag.initialize()
        registry.initialize()
        service = ProductionService(
            parser=DoclingSourceParser(),
            extractor=DeepSeekStructuredExtractor.from_credentials(model=config.model, api_key=config.deepseek_api_key),
            publisher=FilesystemKnowledgePublisher(config.vault_root, rag, registry),
            processed_registry=registry,
            entailment_judge=DeepSeekEntailmentJudge(
                config.model,
                config.deepseek_api_key,
                request_timeout_seconds=float(os.getenv("DEEPSEEK_JUDGE_TIMEOUT_SECONDS", "90")),
            ),
            run_deadline_seconds=float(os.getenv("DEEPSEEK_RUN_DEADLINE_SECONDS", "420")),
        )
        return build_production_graph(
            ProductionGraphDependencies(SingleCandidateFinder(candidate), service),
            checkpointer=MemorySaver(),
        )

    def interactive_factory():
        if not config.deepseek_api_key:
            raise RuntimeError("DeepSeek is not configured after preflight.")
        return build_interactive_graph(
            InteractiveGraphDependencies(
                rag=rag,
                supplementer=RejectingSupplementer(),
                answer_generator=DeepSeekGroundedAnswerGenerator(config.deepseek_api_key, config.model),
                minimum_hits=2,
            ),
            checkpointer=MemorySaver(),
        )

    return RealAcceptanceDependencies(
        run_kind=RunKind.REAL,
        preflight=PreflightDependencies(
            database_probe=default_database_probe,
            docling_probe=default_docling_probe,
            writable_probe=default_writable_probe,
            candidate_finder=finder,
        ),
        registry=registry,
        rag=rag,
        production_graph_factory=production_factory,
        interactive_graph_factory=interactive_factory,
    )


def main(argv: list[str] | None = None) -> int:
    root = _project_root()
    load_dotenv(root / ".env", override=False)
    parser = argparse.ArgumentParser(
        description="对一篇真实 arXiv 论文运行 Research Pulse 端到端验收（可能产生 DeepSeek 费用）。"
    )
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_TEXT_MODEL))
    parser.add_argument("--receipt-dir", type=Path, default=root / "evals" / "real-e2e")
    args = parser.parse_args(argv)
    config = AcceptanceConfig(
        database_url=os.getenv("DATABASE_URL"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
        model=args.model,
        topic=args.topic,
        domain=args.domain,
        vault_root=Path(os.getenv("RESEARCH_PULSE_VAULT_ROOT", str(root / "knowledge"))),
        receipt_root=args.receipt_dir,
    )
    try:
        result = run_real_acceptance(config, build_real_dependencies(config), question=args.question)
    except Exception as error:
        code, _ = safe_error(error, stage="receipt")
        print(f"real-paper acceptance could not write a safe receipt: {code}")
        return 2
    print(f"core_status={result.receipt.final_status.value}")
    print(f"receipt_json={_display_path(result.paths.json, root)}")
    print(f"receipt_markdown={_display_path(result.paths.markdown, root)}")
    return result.exit_code


def _failed_result(
    writer: AcceptanceReceiptWriter,
    stages: _StageLog,
    config: AcceptanceConfig,
    run_id: str,
    started_at: str,
    source_id: str,
    stage: str,
    error: Exception,
    stage_started: str,
    stage_clock: float,
    *,
    run_kind: RunKind,
    bundle_verification: BundleVerification | None = None,
    retrieval_verification: RetrievalVerification | None = None,
) -> AcceptanceRunResult:
    code, summary = safe_error(error, stage=stage)
    stages.fail_stage(
        stage,
        status=StageStatus.FAILED,
        error_code=code,
        summary=summary,
        started=stage_started,
        clock=stage_clock,
    )
    return _write_result(
        writer=writer,
        stages=stages,
        config=config,
        run_id=run_id,
        started_at=started_at,
        final_status=FinalStatus.ACCEPTANCE_FAILED,
        run_kind=run_kind,
        source_id=source_id,
        bundle_verification=bundle_verification,
        retrieval_verification=retrieval_verification,
    )


def _write_result(
    *,
    writer: AcceptanceReceiptWriter,
    stages: _StageLog,
    config: AcceptanceConfig,
    run_id: str,
    started_at: str,
    final_status: FinalStatus,
    run_kind: RunKind = RunKind.REAL,
    source_id: str | None,
    bundle_verification: BundleVerification | None = None,
    retrieval_verification: RetrievalVerification | None = None,
    chat_verification: ChatVerification | None = None,
) -> AcceptanceRunResult:
    bundle = bundle_verification.bundle if bundle_verification else None
    receipt = RealPaperAcceptanceReceipt(
        receipt_schema_version=1,
        run_id=run_id,
        run_kind=run_kind,
        final_status=final_status,
        started_at=started_at,
        topic=config.topic,
        domain=config.domain,
        model=config.model,
        stages=stages.finalize(),
        source_id=source_id,
        knowledge_id=bundle.asset.knowledge_id if bundle else None,
        knowledge_version=bundle.asset.knowledge_version if bundle else None,
        evidence_level=bundle.asset.evidence_level if bundle else None,
        bundle_markdown_path=bundle_verification.markdown_relative_path if bundle_verification else None,
        content_sha256=bundle.asset.content_sha256 if bundle else None,
        provenance_sha256=bundle.asset.provenance_sha256 if bundle else None,
        manifest_status=bundle_verification.manifest.index_status if bundle_verification else None,
        claim_ids=bundle_verification.source_fact_ids if bundle_verification else (),
        source_anchor_ids=bundle_verification.source_anchor_ids if bundle_verification else (),
        chunk_ids=retrieval_verification.chunk_ids if retrieval_verification else (),
        citation_ids=chat_verification.citation_ids if chat_verification else (),
        answer_sha256=chat_verification.answer_sha256 if chat_verification else None,
        answer_length=chat_verification.answer_length if chat_verification else None,
        browser_status=BrowserStatus.PENDING if final_status == FinalStatus.PASSED else BrowserStatus.SKIPPED,
    )
    paths = writer.write(receipt)
    return AcceptanceRunResult(receipt, paths)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1_000))


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


if __name__ == "__main__":
    raise SystemExit(main())
