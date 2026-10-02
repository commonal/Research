"""Thin session-paper HTTP routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable
import re
from urllib.request import urlopen
from xml.etree import ElementTree

from io import BytesIO
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse

from research_pulse.workbench.paper_access import (
    PaperAccessService,
    PaperNotAttachedError,
    PaperNotFoundError,
    UnmanagedPaperPathError,
)
from research_pulse.workbench.sessions import PaperLimitError, SessionNotFoundError
from research_pulse.workbench.paper_download import PdfDownloadError
from research_pulse.workbench.paper_ingest import PdfIngestError
from research_pulse.workbench.paper_submission import PaperSubmissionService


class AddPaperUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    title: str | None = Field(default=None, max_length=500)


def build_workbench_paper_router(
    service: PaperAccessService,
    *,
    submission: PaperSubmissionService | None = None,
    note_lookup: Callable[[Any], tuple[str, str] | None] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/workbench/sessions/{session_id}/papers", tags=["workbench"])

    def paper_payload(paper, session_id: str) -> dict[str, Any]:
        if (not paper.title or paper.title.startswith("arXiv Query:")) and paper.source_url:
            match = re.search(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", paper.source_url, re.I)
            if match:
                try:
                    with urlopen(f"https://export.arxiv.org/api/query?id_list={match.group(1)}", timeout=4) as response:
                        root = ElementTree.fromstring(response.read())
                    entry = next((node for node in root.iter() if node.tag.endswith("}entry")), None)
                    title_node = next((node for node in entry.iter() if node.tag.endswith("}title")), None) if entry is not None else None
                    title = " ".join((title_node.text or "").split()) if title_node is not None else ""
                    if title:
                        paper = service.repository.update_paper_title(paper.paper_id, title) or paper
                except Exception:
                    pass
        sample_block = None
        try:
            blocks = service.load_resolvable_blocks(session_id, paper.paper_id)
            sample_block = blocks[0] if blocks else None
        except (PaperNotAttachedError, UnmanagedPaperPathError, RuntimeError):
            # Preparation is asynchronous. A paper without a resolvable block is
            # still a valid paper projection; the UI simply cannot attach a
            # selection context yet.
            pass
        if (not paper.title or paper.title.startswith("arXiv Query:")) and sample_block and sample_block.section_path:
            recovered_title = sample_block.section_path[-1]
            if recovered_title:
                paper = service.repository.update_paper_title(paper.paper_id, recovered_title) or paper
        note = note_lookup(paper) if note_lookup is not None else None
        return {
            "paper_id": paper.paper_id,
            "title": paper.title,
            "source_url": paper.source_url,
            "pdf_status": paper.pdf_status.value,
            "parse_status": paper.parse_status.value,
            "safe_error": paper.safe_error,
            "pdf_url": f"/api/workbench/sessions/{session_id}/papers/{paper.paper_id}/pdf",
            "sample_block_id": sample_block.block_id if sample_block else None,
            "sample_section_path": list(sample_block.section_path) if sample_block else [],
            "sample_page": sample_block.page if sample_block else None,
            # Published notes live in the knowledge vault, but belong to this
            # paper. Project the read-only relation so a paper-first session
            # keeps showing the note after a refresh.
            "note_status": note[1] if note else "idle",
            "note_url": note[0] if note else None,
        }

    @router.get("")
    def list_papers(session_id: str) -> dict[str, list[dict[str, Any]]]:
        try:
            return {"items": [paper_payload(item, session_id) for item in service.list(session_id)]}
        except SessionNotFoundError as error:
            raise HTTPException(status_code=404, detail="workbench session not found") from error

    @router.post("/upload", status_code=202)
    async def upload_paper(session_id: str, request: Request, background_tasks: BackgroundTasks):
        if submission is None:
            raise HTTPException(503, "paper upload is unavailable")
        try:
            paper = submission.upload(session_id, BytesIO(await request.body()))
            background_tasks.add_task(service.queue.run_next)
            return paper_payload(paper, session_id)
        except PdfIngestError as error:
            raise HTTPException(422, str(error)) from error
        except (SessionNotFoundError, PaperLimitError) as error:
            raise HTTPException(409 if isinstance(error, PaperLimitError) else 404, str(error)) from error

    @router.post("/url", status_code=202)
    def add_paper_url(session_id: str, request: AddPaperUrlRequest, background_tasks: BackgroundTasks):
        if submission is None:
            raise HTTPException(503, "paper URL import is unavailable")
        try:
            paper = submission.add_url(session_id, request.url, title=request.title)
            background_tasks.add_task(service.queue.run_next)
            return paper_payload(paper, session_id)
        except PdfDownloadError as error:
            raise HTTPException(422, str(error)) from error
        except (SessionNotFoundError, PaperLimitError) as error:
            raise HTTPException(409 if isinstance(error, PaperLimitError) else 404, str(error)) from error

    @router.post("/{paper_id}")
    def attach_paper(session_id: str, paper_id: str) -> dict[str, Any]:
        try:
            return paper_payload(service.attach(session_id, paper_id), session_id)
        except (SessionNotFoundError, PaperNotFoundError) as error:
            raise HTTPException(status_code=404, detail="session or paper not found") from error
        except PaperLimitError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.delete("/{paper_id}", status_code=204)
    def remove_paper(session_id: str, paper_id: str) -> Response:
        try:
            service.remove(session_id, paper_id)
        except (SessionNotFoundError, PaperNotFoundError) as error:
            raise HTTPException(status_code=404, detail="session or paper not found") from error
        return Response(status_code=204)

    @router.post("/{paper_id}/retry", status_code=202)
    def retry_paper(session_id: str, paper_id: str) -> dict[str, Any]:
        try:
            return paper_payload(service.retry(session_id, paper_id), session_id)
        except (SessionNotFoundError, PaperNotFoundError, PaperNotAttachedError) as error:
            raise HTTPException(status_code=404, detail="session paper not found") from error
        except UnmanagedPaperPathError as error:
            raise HTTPException(status_code=409, detail="paper material is unavailable") from error

    @router.get("/{paper_id}/pdf")
    def read_pdf(session_id: str, paper_id: str) -> FileResponse:
        try:
            path = service.pdf_file(session_id, paper_id)
        except (SessionNotFoundError, PaperNotFoundError, PaperNotAttachedError, UnmanagedPaperPathError) as error:
            raise HTTPException(status_code=404, detail="paper PDF not found") from error
        return FileResponse(path, media_type="application/pdf", filename="paper.pdf")

    @router.get("/{paper_id}/blocks")
    def list_blocks(session_id: str, paper_id: str) -> dict[str, list[dict[str, Any]]]:
        """Expose only safe block metadata for deterministic PDF selection matching."""
        try:
            blocks = service.load_resolvable_blocks(session_id, paper_id)
        except (SessionNotFoundError, PaperNotFoundError, PaperNotAttachedError) as error:
            raise HTTPException(status_code=404, detail="session paper not found") from error
        items: list[dict[str, Any]] = []
        for block in blocks:
            item: dict[str, Any] = {
                "block_id": block.block_id,
                "text": block.text,
                "section_path": list(block.section_path),
                "page": block.page,
                "order": block.order,
                "bbox": list(block.bbox) if block.bbox is not None else None,
                "bbox_format": "xyxy",
                "bbox_space": block.bbox_space,
            }
            if block.bbox_dimensions is not None:
                item["bbox_dimensions"] = list(block.bbox_dimensions)
            items.append(item)
        return {"items": items}

    @router.get("/{paper_id}/blocks/{block_id}")
    def block_locator(session_id: str, paper_id: str, block_id: str) -> dict[str, Any]:
        try:
            return asdict(service.block_locator(session_id, paper_id, block_id))
        except (SessionNotFoundError, PaperNotFoundError) as error:
            raise HTTPException(status_code=404, detail="session or paper not found") from error

    return router
