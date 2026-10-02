"""Session-scoped access to registered papers, PDFs, and block locators."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Protocol

from research_pulse.workbench.models import Paper, ParseStatus, PdfStatus
from research_pulse.workbench.preparation import PreparationQueue
from research_pulse.workbench.sessions import PaperLimitError, SessionService
from research_pulse.workbench.context_resolution import ResolvableBlock


class PaperAccessRepository(Protocol):
    def get_paper(self, paper_id: str) -> Paper | None: ...
    def save_paper(self, paper: Paper) -> None: ...
    def list_paper_ids(self, session_id: str) -> tuple[str, ...]: ...
    def unlink_paper(self, session_id: str, paper_id: str) -> None: ...
    def has_paper_link(self, session_id: str, paper_id: str) -> bool: ...


class PaperNotFoundError(LookupError):
    pass


class PaperNotAttachedError(LookupError):
    pass


class UnmanagedPaperPathError(LookupError):
    pass


class PaperNotReadyError(RuntimeError):
    pass


def _normalized_bbox(value: object) -> tuple[float, float, float, float] | None:
    """Return a stable top-left/xyxy box even when a parser reverses y."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(isinstance(item, (int, float)) for item in value):
        return None
    x0, y0, x1, y1 = (float(item) for item in value)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


@dataclass(frozen=True)
class BlockLocator:
    status: str
    block_id: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    section_path: tuple[str, ...] = ()
    bbox_space: str = "page_points"
    bbox_dimensions: tuple[float, float] | None = None


class PaperAccessService:
    def __init__(
        self,
        repository: PaperAccessRepository,
        session_service: SessionService,
        queue: PreparationQueue,
        *,
        layer_c_root: str | Path,
        material_cache_root: str | Path,
    ) -> None:
        self.repository = repository
        self.session_service = session_service
        self.queue = queue
        self.layer_c_root = Path(layer_c_root).resolve()
        self.material_cache_root = Path(material_cache_root).resolve()

    def attach(self, session_id: str, paper_id: str) -> Paper:
        paper = self.get(paper_id)
        self.session_service.attach_paper(session_id, paper_id)
        return paper

    def list(self, session_id: str) -> tuple[Paper, ...]:
        self.session_service.get(session_id)
        papers = tuple(self.get(paper_id) for paper_id in self.repository.list_paper_ids(session_id))
        return papers

    def remove(self, session_id: str, paper_id: str) -> None:
        self.session_service.get(session_id)
        self.get(paper_id)
        self.repository.unlink_paper(session_id, paper_id)

    def get(self, paper_id: str) -> Paper:
        paper = self.repository.get_paper(paper_id)
        if paper is None:
            raise PaperNotFoundError(paper_id)
        return paper

    def retry(self, session_id: str, paper_id: str) -> Paper:
        self._require_attached(session_id, paper_id)
        paper = self.get(paper_id)
        if paper.parse_status != ParseStatus.FAILED:
            return paper
        paper = replace(
            paper.transition_parse(ParseStatus.QUEUED),
            safe_error=None,
        )
        self.repository.save_paper(paper)
        pdf_path = self._managed_path(paper.pdf_path, self.layer_c_root)
        material_root = self._managed_path(paper.material_root, self.material_cache_root)
        self.queue.enqueue(
            paper.paper_id,
            pdf_path=pdf_path,
            source_url=paper.source_url or f"urn:sha256:{paper.paper_id}",
            material_root=material_root,
        )
        return paper

    def pdf_file(self, session_id: str, paper_id: str) -> Path:
        self._require_attached(session_id, paper_id)
        paper = self.get(paper_id)
        if paper.pdf_status != PdfStatus.READY:
            raise PaperNotFoundError(paper_id)
        path = self._managed_path(paper.pdf_path, self.layer_c_root)
        if not path.is_file():
            raise PaperNotFoundError(paper_id)
        return path

    def block_locator(self, session_id: str, paper_id: str, block_id: str) -> BlockLocator:
        self.session_service.get(session_id)
        paper = self.get(paper_id)
        if not self.repository.has_paper_link(session_id, paper_id):
            return BlockLocator("detached", block_id)
        if paper.parse_status != ParseStatus.READY:
            return BlockLocator("unresolved", block_id)
        try:
            material_root = self._managed_path(paper.material_root, self.material_cache_root)
        except UnmanagedPaperPathError:
            return BlockLocator("unresolved", block_id)
        blocks_path = material_root / "blocks.jsonl"
        if not blocks_path.is_file():
            return BlockLocator("unresolved", block_id)
        bbox_space, bbox_dimensions = self._bbox_metadata(material_root)
        for line in blocks_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("block_id") != block_id:
                continue
            return BlockLocator(
                "resolved",
                block_id,
                page=payload.get("page_start"),
                bbox=_normalized_bbox(payload.get("bbox")),
                section_path=tuple(str(item) for item in payload.get("section_path", [])),
                bbox_space=bbox_space,
                bbox_dimensions=bbox_dimensions,
            )
        return BlockLocator("unresolved", block_id)

    def load_resolvable_blocks(
        self, session_id: str, paper_id: str
    ) -> tuple[ResolvableBlock, ...]:
        self._require_attached(session_id, paper_id)
        paper = self.get(paper_id)
        if paper.parse_status != ParseStatus.READY:
            raise PaperNotReadyError("paper material is not ready")
        material_root = self._managed_path(paper.material_root, self.material_cache_root)
        blocks_path = material_root / "blocks.jsonl"
        if not blocks_path.is_file():
            raise PaperNotReadyError("paper material is not ready")
        bbox_space, bbox_dimensions = self._bbox_metadata(material_root)
        blocks: list[ResolvableBlock] = []
        for order, line in enumerate(blocks_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            block_id = payload.get("block_id")
            text = payload.get("text")
            if not isinstance(block_id, str) or not isinstance(text, str):
                continue
            blocks.append(
                ResolvableBlock(
                    block_id=block_id,
                    text=text,
                    section_path=tuple(str(x) for x in payload.get("section_path", [])),
                    page=payload.get("page_start"),
                    order=order,
                    bbox=_normalized_bbox(payload.get("bbox")),
                    bbox_space=bbox_space,
                    bbox_dimensions=bbox_dimensions,
                )
            )
        return tuple(blocks)

    @staticmethod
    def _bbox_metadata(material_root: Path) -> tuple[str, tuple[float, float] | None]:
        """Read the parser-owned coordinate contract from the material manifest.

        MinerU's v4 API content list uses a 1000x1000 page canvas.  Older or
        synthetic material without a parser marker keeps the historical
        ``page_points`` contract for backwards compatibility.
        """
        manifest_path = material_root / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "page_points", None
        if isinstance(payload, dict) and payload.get("parser") == "mineru_api":
            return "normalized_1000", (1000.0, 1000.0)
        return "page_points", None

    def _require_attached(self, session_id: str, paper_id: str) -> None:
        self.session_service.get(session_id)
        self.get(paper_id)
        if not self.repository.has_paper_link(session_id, paper_id):
            raise PaperNotAttachedError(paper_id)

    @staticmethod
    def _managed_path(value: str | None, root: Path) -> Path:
        if not value:
            raise UnmanagedPaperPathError("managed file is unavailable")
        candidate = Path(value).resolve()
        if not candidate.is_relative_to(root):
            raise UnmanagedPaperPathError("managed file is unavailable")
        return candidate
