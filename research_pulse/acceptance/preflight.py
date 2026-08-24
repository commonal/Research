"""Ordered, no-model preflight for an explicitly requested real run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from research_pulse.production.pipeline import PaperCandidate, ProcessedPaperRegistry
from research_pulse.workflows.production import CandidateFinder


class EnvironmentBlocked(RuntimeError):
    def __init__(self, error_code: str, summary: str) -> None:
        super().__init__(summary)
        self.error_code = error_code


class NoUnprocessedCandidate(EnvironmentBlocked):
    def __init__(self) -> None:
        super().__init__("no_unprocessed_candidate", "实时结果中没有尚未处理的论文候选。")


@dataclass(frozen=True)
class AcceptanceConfig:
    database_url: str | None
    deepseek_api_key: str | None
    model: str
    topic: str
    domain: str
    vault_root: Path
    receipt_root: Path
    candidate_pool_size: int = 5

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.topic.strip() or not self.domain.strip():
            raise ValueError("Model, topic, and domain must be non-empty.")
        if not 1 <= self.candidate_pool_size <= 10:
            raise ValueError("candidate_pool_size must be between 1 and 10.")


@dataclass(frozen=True)
class PreflightDependencies:
    database_probe: Callable[[str], None]
    docling_probe: Callable[[], None]
    writable_probe: Callable[[Path], None]
    candidate_finder: CandidateFinder


@dataclass(frozen=True)
class PreflightResult:
    candidates: tuple[PaperCandidate, ...]
    checks: tuple[str, ...]


def run_environment_preflight(config: AcceptanceConfig, deps: PreflightDependencies) -> PreflightResult:
    """Run every zero-cost check before any DeepSeek or production invocation."""

    checks: list[str] = []
    if not config.database_url:
        raise EnvironmentBlocked("database_not_configured", "DATABASE_URL 未配置。")
    if not config.deepseek_api_key:
        raise EnvironmentBlocked("deepseek_not_configured", "DEEPSEEK_API_KEY 未配置。")
    checks.append("configuration")
    _probe("database_unavailable", "PostgreSQL 不可用。", lambda: deps.database_probe(config.database_url))
    checks.append("database")
    _probe("docling_unavailable", "Docling 不可用。", deps.docling_probe)
    checks.append("docling")
    _probe("vault_unwritable", "知识仓不可写。", lambda: deps.writable_probe(config.vault_root))
    _probe("receipt_root_unwritable", "验收回执目录不可写。", lambda: deps.writable_probe(config.receipt_root))
    checks.append("writable_roots")
    try:
        candidates = deps.candidate_finder.discover(
            topic=config.topic,
            domain=config.domain,
            limit=config.candidate_pool_size,
        )
    except Exception as error:
        raise EnvironmentBlocked("arxiv_unreachable", "arXiv 实时查询不可用。") from error
    checks.append("arxiv")
    return PreflightResult(tuple(candidates), tuple(checks))


def select_unprocessed_candidate(
    candidates: Sequence[PaperCandidate],
    registry: ProcessedPaperRegistry,
) -> PaperCandidate:
    for candidate in candidates:
        if not registry.was_processed(candidate.source_id):
            return candidate
    raise NoUnprocessedCandidate()


def default_database_probe(database_url: str) -> None:
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("psycopg is unavailable") from error
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("PostgreSQL health probe returned an unexpected result")


def default_docling_probe() -> None:
    try:
        from docling.document_converter import DocumentConverter  # noqa: F401
    except ImportError as error:
        raise RuntimeError("Docling is unavailable") from error


def default_writable_probe(root: Path) -> None:
    resolved = root.resolve(strict=False)
    resolved.mkdir(parents=True, exist_ok=True)
    probe = resolved / ".research-pulse-write-probe.tmp"
    try:
        probe.write_text("probe\n", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "probe\n":
            raise OSError("Write probe verification failed")
    finally:
        probe.unlink(missing_ok=True)


def _probe(error_code: str, summary: str, operation: Callable[[], None]) -> None:
    try:
        operation()
    except Exception as error:
        raise EnvironmentBlocked(error_code, summary) from error
