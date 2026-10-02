"""SSRF-aware public PDF download into the managed Layer C cache."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Callable, Iterable, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit
import re
import socket

import httpx

from research_pulse.workbench.paper_ingest import (
    PdfIngestError,
    PdfIngestResult,
    PdfIngestStore,
)


_ARXIV_ID = re.compile(r"^(?:abs|pdf)/([^/?#]+?)(?:\.pdf)?$")


class PdfDownloadError(ValueError):
    """Safe public error for a rejected or failed remote PDF."""


@dataclass(frozen=True)
class NormalizedPaperUrl:
    source_identity: str | None
    canonical_url: str
    download_url: str


@dataclass(frozen=True)
class PdfDownloadConfig:
    timeout_seconds: float = 30.0
    max_redirects: int = 5

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must not be negative")


@dataclass(frozen=True)
class DownloadedPdf:
    source_identity: str
    source_url: str
    pdf: PdfIngestResult


def normalize_paper_url(url: str) -> NormalizedPaperUrl:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise PdfDownloadError("只支持 HTTP(S) 公开 PDF URL")
    if not parsed.hostname or parsed.username or parsed.password:
        raise PdfDownloadError("PDF URL 主机无效")

    host = parsed.hostname.casefold().rstrip(".")
    path = parsed.path.lstrip("/")
    if host in {"arxiv.org", "www.arxiv.org"}:
        matched = _ARXIV_ID.match(path)
        if matched:
            arxiv_id = matched.group(1)
            canonical = f"https://arxiv.org/abs/{arxiv_id}"
            return NormalizedPaperUrl(
                source_identity=f"arxiv:{arxiv_id}",
                canonical_url=canonical,
                download_url=f"https://arxiv.org/pdf/{arxiv_id}",
            )

    normalized = urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )
    return NormalizedPaperUrl(None, normalized, normalized)


def _resolve_public(host: str) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as error:
        raise PdfDownloadError("PDF URL 域名无法解析") from error
    return tuple(dict.fromkeys(record[4][0] for record in records))


class _IteratorReader:
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks: Iterator[bytes] = iter(chunks)

    def read(self, _: int = -1) -> bytes:
        return next(self._chunks, b"")


class PublicPdfDownloader:
    def __init__(
        self,
        store: PdfIngestStore,
        *,
        config: PdfDownloadConfig = PdfDownloadConfig(),
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], Iterable[str]] = _resolve_public,
    ) -> None:
        self.store = store
        self.config = config
        self.transport = transport
        self.resolver = resolver

    def download(self, url: str) -> DownloadedPdf:
        normalized = normalize_paper_url(url)
        current_url = normalized.download_url
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=httpx.Timeout(self.config.timeout_seconds),
                follow_redirects=False,
                trust_env=self.transport is None,
            ) as client:
                for redirect_count in range(self.config.max_redirects + 1):
                    self._validate_public_target(current_url)
                    with client.stream("GET", current_url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or redirect_count >= self.config.max_redirects:
                                raise PdfDownloadError("PDF URL 重定向次数超限")
                            current_url = urljoin(current_url, location)
                            continue
                        if response.status_code < 200 or response.status_code >= 300:
                            raise PdfDownloadError("PDF 下载服务返回错误")
                        pdf = self.store.ingest(_IteratorReader(response.iter_bytes()))
                        identity = normalized.source_identity or pdf.source_identity
                        return DownloadedPdf(identity, normalized.canonical_url, pdf)
        except PdfDownloadError:
            raise
        except PdfIngestError as error:
            raise PdfDownloadError(str(error)) from error
        except httpx.TimeoutException as error:
            raise PdfDownloadError("PDF 下载超时") from error
        except httpx.HTTPError as error:
            raise PdfDownloadError("PDF 下载失败") from error
        raise PdfDownloadError("PDF 下载失败")

    def _validate_public_target(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PdfDownloadError("重定向目标必须是 HTTP(S) URL")
        if parsed.username or parsed.password:
            raise PdfDownloadError("重定向目标不得包含凭据")
        try:
            literal_address = ip_address(parsed.hostname)
        except ValueError:
            literal_address = None
        addresses = (
            (str(literal_address),)
            if literal_address is not None
            else tuple(self.resolver(parsed.hostname))
        )
        if not addresses:
            raise PdfDownloadError("PDF URL 域名无法解析")
        try:
            parsed_addresses = tuple(ip_address(address) for address in addresses)
        except ValueError as error:
            raise PdfDownloadError("PDF URL 域名解析结果无效") from error
        if any(not address.is_global for address in parsed_addresses):
            raise PdfDownloadError("PDF URL 必须解析到公网地址")
