"""Supporting-paper import: turn a search_arxiv candidate into a managed source.

Thin adapter. It does NOT build a new parser or read PDF text itself. It only
hands the candidate's location (its arxiv id / url) to the existing managed
acquisition pipeline (``PublicPdfDownloader`` -> ``PaperPreparationService``),
then returns the new ``source_id`` plus a sample managed block id so a
read-only research tool can later read the blocks.

Unlike ``PaperSubmissionService``, a supporting paper is NEVER attached to the
user's session as an active paper. It is registered as a reusable managed
source, which is exactly what the "evidence unique entry" (``read_managed_blocks``)
requires.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import time
from typing import Protocol

from research_pulse.workbench.preparation import PaperPreparationService


class SupportingPaperImportError(ValueError):
    """Safe public error for a rejected or failed supporting-paper import."""


class PaperDownloader(Protocol):
    def download(self, url: str): ...


@dataclass(frozen=True)
class SupportingPaperImportResult:
    source_id: str
    sample_block_id: str | None
    source_identity: str | None
    source_url: str | None
    pdf_status: str
    parse_status: str
    safe_error: str | None


def _arxiv_pdf_url(arxiv_id: str) -> str:
    base = arxiv_id.split("v")[0] if arxiv_id.lower().endswith("v1") else arxiv_id
    return f"https://arxiv.org/pdf/{arxiv_id}"


class SupportingPaperImporter:
    """Reuse download/ingest/prep to turn a candidate metadata into a managed source."""

    def __init__(
        self,
        *,
        preparation: PaperPreparationService,
        downloader: PaperDownloader,
        after_enqueue: Callable[[], None] | None = None,
    ) -> None:
        self.preparation = preparation
        self.downloader = downloader
        # Called once after a successful register (the PDF is queued for
        # parsing). The runtime wires this to an async drain so the parse runs
        # in the background instead of blocking the agent call.
        self._after_enqueue = after_enqueue

    def import_paper(self, candidate: dict[str, object]) -> SupportingPaperImportResult:
        arxiv_id = str(candidate.get("arxiv_id") or "").strip()
        source_id = str(candidate.get("source_id") or "").strip()
        url = str(candidate.get("url") or "").strip()
        title = str(candidate.get("title") or "").strip()

        # Need some way to locate the paper: prefer the explicit arxiv id (most
        # reliable), else the source_url. Without a location we cannot download.
        location = arxiv_id or source_id or url
        if not location:
            raise SupportingPaperImportError("candidate has no arxiv id or url to locate the paper")

        alias = arxiv_id or source_id or url
        existing_lookup = getattr(self.preparation.repository, "get_paper_by_source_alias", None)
        if existing_lookup is not None:
            existing = existing_lookup(alias)
            if existing is not None and not existing.safe_error:
                # A previous request may have registered the paper while its
                # parse is still queued.  Re-kick the runtime drain so opening
                # the same candidate in a second session cannot leave it
                # permanently stuck in ``queued``.
                if existing.parse_status.value != "ready" and self._after_enqueue is not None:
                    self._after_enqueue()
                return SupportingPaperImportResult(
                    source_id=existing.paper_id,
                    sample_block_id=None,
                    source_identity=existing.source_identity,
                    source_url=existing.source_url,
                    pdf_status=existing.pdf_status.value,
                    parse_status=existing.parse_status.value,
                    safe_error=None,
                )

        # The downloader hands back a downloaded PDF (a PdfIngestResult). We do
        # not construct the URL here; the downloader knows how to turn a raw
        # arxiv id or url into a PDF. If a plain arxiv id was given, hand the
        # downloader a canonical abs url so it normalizes consistently.
        download_url = url
        if not download_url and arxiv_id:
            download_url = f"https://arxiv.org/abs/{arxiv_id}"

        last_error: Exception | None = None
        for attempt in range(1, 4):  # arXiv PDF endpoints are flaky on some
            # networks (connection reset mid-transfer); retry before giving up.
            try:
                downloaded = self.downloader.download(download_url if download_url else location)
                last_error = None
                break
            except Exception as error:  # noqa: BLE001
                last_error = error
                if attempt < 3:
                    time.sleep(1.0)
        if last_error is not None:
            reason = f"{type(last_error).__name__}: {str(last_error)[:120]}"
            raise SupportingPaperImportError(
                f"supporting paper download failed after 3 attempts ({reason}). "
                "arXiv PDF 连接可能被网络重置——可稍后重试，或让用户本地上传该 PDF 后重新导入。"
            ) from last_error

        ingested = downloaded.pdf
        paper = self.preparation.register_pdf(
            ingested,
            source_alias=str(downloaded.source_identity or source_id or arxiv_id),
            source_url=str(downloaded.source_url or url or ""),
            title=title or None,
        )
        paper = self.preparation.get(paper.paper_id)
        if self._after_enqueue is not None:
            self._after_enqueue()

        sample_block_id = None
        if paper.parse_status.value == "ready" and paper.material_root:
            from pathlib import Path

            blocks_path = Path(paper.material_root) / "blocks.jsonl"
            if blocks_path.exists():
                for line in blocks_path.read_text(encoding="utf-8").splitlines():
                    if '"block_id"' in line:
                        sample_block_id = _extract_block_id(line)
                        break
        elif paper.safe_error:
            # Surface a safe, sanitized failure description only.
            return SupportingPaperImportResult(
                source_id=paper.paper_id,
                sample_block_id=None,
                source_identity=str(downloaded.source_identity or ""),
                source_url=str(downloaded.source_url or ""),
                pdf_status=paper.pdf_status.value,
                parse_status=paper.parse_status.value,
                safe_error=paper.safe_error,
            )

        return SupportingPaperImportResult(
            source_id=paper.paper_id,
            sample_block_id=sample_block_id,
            source_identity=str(downloaded.source_identity or ""),
            source_url=str(downloaded.source_url or ""),
            pdf_status=paper.pdf_status.value,
            parse_status=paper.parse_status.value,
            safe_error=None,
        )


def _extract_block_id(line: str) -> str | None:
    import json

    try:
        item = json.loads(line)
        value = item.get("block_id") if isinstance(item, dict) else None
        return str(value) if value else None
    except json.JSONDecodeError:
        return None
