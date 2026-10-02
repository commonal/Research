"""Application seam for uploading or downloading a paper into one session."""

from __future__ import annotations

from typing import BinaryIO

from research_pulse.workbench.paper_access import PaperAccessService
from research_pulse.workbench.paper_download import PublicPdfDownloader
from research_pulse.workbench.paper_ingest import PdfIngestStore
from research_pulse.workbench.preparation import PaperPreparationService


class PaperSubmissionService:
    def __init__(self, ingest: PdfIngestStore, preparation: PaperPreparationService, access: PaperAccessService, downloader: PublicPdfDownloader) -> None:
        self.ingest = ingest
        self.preparation = preparation
        self.access = access
        self.downloader = downloader

    def upload(self, session_id: str, stream: BinaryIO):
        ingested = self.ingest.ingest(stream)
        paper = self.preparation.register_pdf(ingested)
        self.access.attach(session_id, paper.paper_id)
        return paper

    def add_url(self, session_id: str, url: str, title: str | None = None):
        downloaded = self.downloader.download(url)
        paper = self.preparation.register_pdf(
            downloaded.pdf,
            source_alias=downloaded.source_identity,
            source_url=downloaded.source_url,
            title=title,
        )
        self.access.attach(session_id, paper.paper_id)
        return paper
