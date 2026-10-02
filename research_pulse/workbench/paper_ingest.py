"""Safe local PDF ingestion into the managed Layer C source cache."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4
import os


DEFAULT_MAX_PDF_BYTES = 100 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_PDF_MAGIC = b"%PDF-"
_PDF_EOF = b"%%EOF"


class PdfIngestError(ValueError):
    """Safe validation error for a rejected local PDF."""


@dataclass(frozen=True)
class PdfIngestResult:
    paper_id: str
    source_identity: str
    sha256: str
    size_bytes: int
    pdf_path: Path
    created: bool


class PdfIngestStore:
    def __init__(
        self,
        layer_c_root: str | Path,
        *,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
    ) -> None:
        if max_pdf_bytes < len(_PDF_MAGIC):
            raise ValueError("max_pdf_bytes is too small for a PDF")
        self.layer_c_root = Path(layer_c_root)
        self.max_pdf_bytes = max_pdf_bytes

    def ingest(self, stream: BinaryIO) -> PdfIngestResult:
        self.layer_c_root.mkdir(parents=True, exist_ok=True)
        temporary = self.layer_c_root / f".upload-{uuid4().hex}.tmp"
        digest = sha256()
        size_bytes = 0
        prefix = bytearray()
        tail = bytearray()

        try:
            with temporary.open("xb") as target:
                while True:
                    chunk = stream.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise PdfIngestError("PDF 上传流必须为二进制内容")
                    size_bytes += len(chunk)
                    if size_bytes > self.max_pdf_bytes:
                        raise PdfIngestError("PDF 文件超过配置上限")
                    if len(prefix) < len(_PDF_MAGIC):
                        prefix.extend(chunk[: len(_PDF_MAGIC) - len(prefix)])
                    tail.extend(chunk)
                    if len(tail) > 2048:
                        del tail[:-2048]
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())

            if bytes(prefix) != _PDF_MAGIC:
                raise PdfIngestError("无效的 PDF 文件头")
            if _PDF_EOF not in tail:
                raise PdfIngestError("PDF 文件不完整，缺少结束标记")

            content_hash = digest.hexdigest()
            paper_id = content_hash
            paper_root = self.layer_c_root / paper_id
            destination = paper_root / "source.pdf"
            if destination.is_file():
                temporary.unlink(missing_ok=True)
                created = False
            else:
                paper_root.mkdir(parents=False, exist_ok=True)
                os.replace(temporary, destination)
                created = True

            return PdfIngestResult(
                paper_id=paper_id,
                source_identity=f"sha256:{content_hash}",
                sha256=content_hash,
                size_bytes=size_bytes,
                pdf_path=destination,
                created=created,
            )
        except PdfIngestError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as error:
            temporary.unlink(missing_ok=True)
            raise PdfIngestError("PDF 接收失败") from error
