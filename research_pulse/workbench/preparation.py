"""Shared Paper registration and one-owner structured preparation queue."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from research_pulse.workbench.models import Paper, ParseStatus, PdfStatus
from research_pulse.workbench.paper_ingest import PdfIngestResult


class PaperRepository(Protocol):
    def upsert_paper(self, paper: Paper) -> Paper: ...
    def get_paper(self, paper_id: str) -> Paper | None: ...
    def save_paper(self, paper: Paper) -> None: ...
    def add_paper_source(self, paper_id: str, source_alias: str, source_url: str | None = None) -> None: ...


class PaperParser(Protocol):
    def parse_pdf(
        self,
        pdf_path: Path,
        *,
        source_id: str,
        source_url: str,
        output_dir: Path,
    ) -> Path: ...


@dataclass(frozen=True)
class PaperCapabilities:
    pdf_readable: bool
    structured_ready: bool
    can_ask_with_paper: bool
    can_generate_note: bool


@dataclass(frozen=True)
class _PreparationJob:
    paper_id: str
    pdf_path: Path
    source_url: str
    material_root: Path


class PreparationQueue:
    """Deterministic single-owner queue; the app lifespan calls run_next."""

    def __init__(self, parser: PaperParser, repository: PaperRepository) -> None:
        self.parser = parser
        self.repository = repository
        self._pending: OrderedDict[str, _PreparationJob] = OrderedDict()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def enqueue(
        self,
        paper_id: str,
        *,
        pdf_path: Path,
        source_url: str,
        material_root: Path,
    ) -> None:
        self._pending.setdefault(
            paper_id,
            _PreparationJob(paper_id, pdf_path, source_url, material_root),
        )

    def run_next(self) -> Paper | None:
        if not self._pending:
            return None
        _, job = self._pending.popitem(last=False)
        paper = self.repository.get_paper(job.paper_id)
        if paper is None:
            return None
        if paper.parse_status == ParseStatus.READY:
            return paper
        if paper.parse_status == ParseStatus.FAILED:
            paper = paper.transition_parse(ParseStatus.QUEUED)
        if paper.parse_status == ParseStatus.QUEUED:
            paper = paper.transition_parse(ParseStatus.PARSING)
        self.repository.save_paper(replace(paper, safe_error=None))
        try:
            self.parser.parse_pdf(
                job.pdf_path,
                source_id=job.paper_id,
                source_url=job.source_url,
                output_dir=job.material_root,
            )
            if not _complete_material(job.material_root):
                raise RuntimeError("parser did not commit complete material")
            paper = replace(
                paper.transition_parse(ParseStatus.READY),
                safe_error=None,
            )
        except Exception:
            paper = replace(
                paper.transition_parse(ParseStatus.FAILED),
                safe_error="论文解析失败，可重试",
            )
        self.repository.save_paper(paper)
        return paper


class PaperPreparationService:
    def __init__(
        self,
        repository: PaperRepository,
        queue: PreparationQueue,
        *,
        material_cache_root: str | Path,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.material_cache_root = Path(material_cache_root)

    def register_pdf(
        self,
        ingested: PdfIngestResult,
        *,
        source_alias: str | None = None,
        source_url: str | None = None,
        title: str | None = None,
    ) -> Paper:
        material_root = self.material_cache_root / ingested.paper_id / "material"
        proposed = Paper(
            paper_id=ingested.sha256,
            source_identity=f"sha256:{ingested.sha256}",
            title=title,
            source_url=source_url,
            pdf_path=str(ingested.pdf_path),
            pdf_status=PdfStatus.READY,
            parse_status=ParseStatus.QUEUED,
            material_root=str(material_root),
        )
        paper = self.repository.upsert_paper(proposed)
        alias = source_alias or proposed.source_identity
        self.repository.add_paper_source(paper.paper_id, alias, source_url)
        if paper.parse_status != ParseStatus.READY:
            self.queue.enqueue(
                paper.paper_id,
                pdf_path=Path(paper.pdf_path or ingested.pdf_path),
                source_url=source_url or paper.source_url or f"urn:sha256:{paper.paper_id}",
                material_root=Path(paper.material_root or material_root),
            )
        return paper

    def get(self, paper_id: str) -> Paper:
        paper = self.repository.get_paper(paper_id)
        if paper is None:
            raise KeyError(paper_id)
        return paper

    def capabilities(self, paper_id: str) -> PaperCapabilities:
        paper = self.get(paper_id)
        pdf_readable = paper.pdf_status == PdfStatus.READY
        structured_ready = paper.parse_status == ParseStatus.READY
        return PaperCapabilities(
            pdf_readable=pdf_readable,
            structured_ready=structured_ready,
            can_ask_with_paper=structured_ready,
            can_generate_note=structured_ready,
        )


def _complete_material(material_root: Path) -> bool:
    return (
        (material_root / "manifest.json").is_file()
        and (material_root / "blocks.jsonl").is_file()
    )
