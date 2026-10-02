from __future__ import annotations

from pathlib import Path
from unittest import TestCase

import httpx

from research_pulse.workbench.paper_download import (
    PdfDownloadConfig,
    PdfDownloadError,
    PublicPdfDownloader,
    normalize_paper_url,
)
from research_pulse.workbench.paper_ingest import PdfIngestStore


VALID_PDF = b"%PDF-1.7\nremote\n%%EOF\n"
PUBLIC_IPS = lambda host: ("93.184.216.34",)


class PublicPdfDownloaderTests(TestCase):
    def setUp(self) -> None:
        self.root = Path("tests/.workbench-pdf-download")
        self.created_paths: list[Path] = []

    def tearDown(self) -> None:
        for path in reversed(self.created_paths):
            path.unlink(missing_ok=True)
            if path.parent.exists():
                path.parent.rmdir()
        if self.root.exists():
            for temporary in self.root.glob(".upload-*.tmp"):
                temporary.unlink(missing_ok=True)
            self.root.rmdir()

    def _downloader(self, handler, *, max_bytes=1024) -> PublicPdfDownloader:
        return PublicPdfDownloader(
            PdfIngestStore(self.root, max_pdf_bytes=max_bytes),
            config=PdfDownloadConfig(timeout_seconds=2, max_redirects=2),
            transport=httpx.MockTransport(handler),
            resolver=PUBLIC_IPS,
        )

    def test_normalizes_arxiv_and_downloads_verified_pdf(self) -> None:
        normalized = normalize_paper_url("https://arxiv.org/abs/2608.12345v2")
        self.assertEqual(normalized.source_identity, "arxiv:2608.12345v2")
        self.assertEqual(normalized.download_url, "https://arxiv.org/pdf/2608.12345v2")

        downloader = self._downloader(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=VALID_PDF,
                request=request,
            )
        )
        result = downloader.download("https://arxiv.org/abs/2608.12345v2")
        self.created_paths.append(result.pdf.pdf_path)

        self.assertEqual(result.source_identity, "arxiv:2608.12345v2")
        self.assertEqual(result.pdf.pdf_path.read_bytes(), VALID_PDF)

    def test_rejects_non_http_private_and_private_redirect(self) -> None:
        with self.assertRaisesRegex(PdfDownloadError, "HTTP"):
            self._downloader(lambda request: None).download("file:///secret.pdf")

        private = PublicPdfDownloader(
            PdfIngestStore(self.root),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=VALID_PDF)),
            resolver=lambda host: ("127.0.0.1",),
        )
        with self.assertRaisesRegex(PdfDownloadError, "公网"):
            private.download("https://example.org/paper.pdf")

        requested_hosts: list[str] = []

        def redirect_handler(request):
            requested_hosts.append(request.url.host)
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private.pdf"})

        with self.assertRaisesRegex(PdfDownloadError, "公网"):
            self._downloader(redirect_handler).download("https://example.org/paper.pdf")
        self.assertEqual(requested_hosts, ["example.org"])

    def test_rejects_timeout_oversize_and_non_pdf_without_residue(self) -> None:
        timeout_downloader = self._downloader(
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=request))
        )
        with self.assertRaisesRegex(PdfDownloadError, "超时"):
            timeout_downloader.download("https://example.org/paper.pdf")

        with self.assertRaisesRegex(PdfDownloadError, "超过配置上限"):
            self._downloader(
                lambda request: httpx.Response(200, content=VALID_PDF, request=request),
                max_bytes=len(VALID_PDF) - 1,
            ).download("https://example.org/large.pdf")

        with self.assertRaisesRegex(PdfDownloadError, "PDF 文件头"):
            self._downloader(
                lambda request: httpx.Response(200, content=b"<html>no</html>", request=request)
            ).download("https://example.org/not-pdf")

        self.assertEqual(list(self.root.rglob("*")) if self.root.exists() else [], [])
