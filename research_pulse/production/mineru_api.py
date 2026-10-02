"""MinerU Precision v4 adapter producing parser-neutral normalized material.

The remote protocol is hidden behind :class:`MinerUApiParser`.  Callers receive
the same committed ``blocks.jsonl`` + ``manifest.json`` cache used by the rest
of Research Pulse; they never orchestrate upload, polling, ZIP handling, or
MinerU-specific JSON themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence
import io
import json
import time
import zipfile

import httpx

from research_pulse.production.normalized import (
    NormalizedBlock,
    NormalizedDocument,
    SourceRef,
)


class MinerUApiError(RuntimeError):
    """Safe, token-free failure reported by the MinerU adapter."""


@dataclass(frozen=True)
class MinerUApiConfig:
    token: str
    base_url: str = "https://mineru.net/api"
    model_version: str = "vlm"
    enable_formula: bool = True
    enable_table: bool = True
    language: str = "ch"
    poll_interval_seconds: float = 2.0
    timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("MINERU_API_TOKEN is required")


class MinerUApiParser:
    """Upload one PDF and commit its MinerU material as ``NormalizedBlock``s."""

    def __init__(
        self,
        config: MinerUApiConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._transport = transport
        self._sleep = sleeper

    def parse_pdf(
        self,
        pdf_path: Path,
        *,
        source_id: str,
        source_url: str,
        output_dir: Path,
    ) -> Path:
        pdf_path = Path(pdf_path)
        output_dir = Path(output_dir)
        if not pdf_path.is_file():
            raise MinerUApiError(f"PDF file does not exist: {pdf_path}")
        headers = {"Authorization": f"Bearer {self.config.token.strip()}"}
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(max(30.0, self.config.timeout_seconds)),
                follow_redirects=True,
            ) as client:
                batch_id, upload_url = self._request_upload(client, headers, pdf_path.name)
                with pdf_path.open("rb") as stream:
                    response = client.put(upload_url, content=stream)
                response.raise_for_status()
                archive_url = self._wait_for_result(client, headers, batch_id, pdf_path.name)
                archive_bytes = self._download_archive(client, archive_url)
            document, markdown_bytes, content_bytes, assets = _normalize_archive(
                archive_bytes,
                source_id=source_id,
                source_url=source_url,
                pdf_hash=_file_sha256(pdf_path),
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "full.md").write_bytes(markdown_bytes)
            (output_dir / "content_list.json").write_bytes(content_bytes)
            for relative, payload in assets.items():
                target = output_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            document.write(output_dir)
            return output_dir
        except MinerUApiError:
            raise
        except Exception as exc:
            detail = str(exc).replace(self.config.token, "[redacted]").strip()
            raise MinerUApiError(
                f"MinerU API parsing failed: {detail or type(exc).__name__}"
            ) from exc

    def _download_archive(self, client: httpx.Client, archive_url: str) -> bytes:
        """Download the result, bypassing a broken local proxy when necessary."""
        try:
            response = client.get(archive_url)
            response.raise_for_status()
            return response.content
        except httpx.TransportError:
            if self._transport is not None:
                raise
            with httpx.Client(
                timeout=httpx.Timeout(max(30.0, self.config.timeout_seconds)),
                follow_redirects=True,
                trust_env=False,
            ) as direct_client:
                response = direct_client.get(archive_url)
                response.raise_for_status()
                return response.content

    def _request_upload(
        self,
        client: httpx.Client,
        headers: Mapping[str, str],
        filename: str,
    ) -> tuple[str, str]:
        response = client.post(
            f"{self.config.base_url.rstrip('/')}/v4/file-urls/batch",
            headers=headers,
            json={
                "files": [{
                    "name": filename,
                    "data_id": sha256(filename.encode()).hexdigest()[:16],
                }],
                "model_version": self.config.model_version,
                "enable_formula": self.config.enable_formula,
                "enable_table": self.config.enable_table,
                "language": self.config.language,
            },
        )
        response.raise_for_status()
        data = _api_data(response)
        batch_id = str(data.get("batch_id") or "")
        urls = data.get("file_urls") or []
        if not batch_id or not isinstance(urls, list) or not urls:
            raise MinerUApiError("MinerU did not return a batch id and upload URL")
        return batch_id, str(urls[0])

    def _wait_for_result(
        self,
        client: httpx.Client,
        headers: Mapping[str, str],
        batch_id: str,
        filename: str,
    ) -> str:
        deadline = time.monotonic() + max(30.0, self.config.timeout_seconds)
        while time.monotonic() < deadline:
            response = client.get(
                f"{self.config.base_url.rstrip('/')}/v4/extract-results/batch/{batch_id}",
                headers=headers,
            )
            response.raise_for_status()
            jobs = _api_data(response).get("extract_result") or []
            job = next(
                (item for item in jobs if item.get("file_name") == filename),
                jobs[0] if jobs else {},
            )
            state = str(job.get("state") or "").casefold()
            if state == "done" and job.get("full_zip_url"):
                return str(job["full_zip_url"])
            if state == "failed":
                raise MinerUApiError(
                    f"MinerU job failed: {job.get('err_msg') or 'unknown error'}"
                )
            self._sleep(max(0.0, self.config.poll_interval_seconds))
        raise MinerUApiError(
            f"MinerU job exceeded {self.config.timeout_seconds:.0f}s timeout"
        )


def _api_data(response: httpx.Response) -> dict[str, Any]:
    payload = response.json()
    if payload.get("code") not in (0, "0", None):
        raise MinerUApiError(
            f"MinerU API rejected request: "
            f"{payload.get('msg') or payload.get('message') or payload.get('code')}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MinerUApiError("MinerU API returned an invalid response")
    return data


def _normalize_archive(
    archive_bytes: bytes,
    *,
    source_id: str,
    source_url: str,
    pdf_hash: str,
) -> tuple[NormalizedDocument, bytes, bytes, dict[Path, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            content_name = next(
                (name for name in archive.namelist() if name.endswith("_content_list.json")),
                None,
            )
            if content_name is None:
                raise MinerUApiError("MinerU result has no content_list.json")
            markdown_name = next(
                (name for name in archive.namelist() if PurePosixPath(name).name == "full.md"),
                next((name for name in archive.namelist() if name.lower().endswith(".md")), None),
            )
            if markdown_name is None:
                raise MinerUApiError("MinerU result has no full.md")
            markdown_bytes = archive.read(markdown_name)
            if not markdown_bytes.strip():
                raise MinerUApiError("MinerU result has an empty full.md")
            content_bytes = archive.read(content_name)
            content = json.loads(content_bytes)
            assets = _archive_assets(archive)
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MinerUApiError(f"MinerU result archive is invalid: {exc}") from exc
    blocks = _content_blocks(content, source_id=source_id, available_assets=assets)
    if not blocks:
        raise MinerUApiError("MinerU returned no usable material blocks")
    document = NormalizedDocument(
        source_id=source_id,
        source_url=source_url,
        blocks=tuple(blocks),
        input_hashes={
            "source_pdf": pdf_hash,
            "mineru_full_markdown": sha256(markdown_bytes).hexdigest(),
            "mineru_content_list": sha256(content_bytes).hexdigest(),
        },
        warnings=("single_parser_observation",),
        parser="mineru_api",
        parser_version="precision-v4",
    )
    return document, markdown_bytes, content_bytes, assets


def _archive_assets(archive: zipfile.ZipFile) -> dict[Path, bytes]:
    assets: dict[Path, bytes] = {}
    for name in archive.namelist():
        path = PurePosixPath(name)
        if "images" not in path.parts or name.endswith("/"):
            continue
        image_index = path.parts.index("images")
        relative_parts = path.parts[image_index:]
        if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
            continue
        relative = Path(*relative_parts)
        assets[relative] = archive.read(name)
    return assets


def _content_blocks(
    payload: Any,
    *,
    source_id: str,
    available_assets: Mapping[Path, bytes],
) -> list[NormalizedBlock]:
    items: list[tuple[int, int, Mapping[str, Any]]] = []
    if isinstance(payload, list):
        for outer_index, item in enumerate(payload):
            if isinstance(item, dict):
                page = int(item.get("page_idx", 0)) + 1
                items.append((page, outer_index, item))
            elif isinstance(item, list):
                items.extend(
                    (outer_index + 1, inner_index, child)
                    for inner_index, child in enumerate(item)
                    if isinstance(child, dict)
                )
    section_path: tuple[str, ...] = ()
    blocks: list[NormalizedBlock] = []
    for page, index, item in items:
        raw_kind = str(item.get("type") or "text").casefold()
        text, latex, table_html, caption = _item_content(item, raw_kind)
        if not text:
            continue
        is_heading = raw_kind in {"title", "heading"} or (
            raw_kind == "text"
            and isinstance(item.get("text_level"), int)
            and int(item["text_level"]) > 0
        )
        if is_heading:
            section_path = (text,)
        kind = _normalized_kind(raw_kind)
        bbox = _bbox(item.get("bbox"))
        image_path = _safe_image_path(item.get("img_path"), available_assets)
        identity = json.dumps(
            [source_id, page, index, raw_kind, bbox, text],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        block_id = f"normalized:{source_id}:{kind}:{sha256(identity.encode()).hexdigest()[:12]}"
        blocks.append(NormalizedBlock(
            block_id=block_id,
            kind=kind,
            text=text,
            section_path=section_path,
            page_start=page,
            page_end=page,
            bbox=bbox,
            caption=caption,
            latex=latex,
            table_html=table_html,
            image_path=image_path,
            sources=(SourceRef("mineru_api", f"content_list.json#/{index}"),),
            alignment="mineru_only",
            parse_status="available",
            confidence=0.55,
        ))
    return blocks


def _item_content(
    item: Mapping[str, Any], raw_kind: str
) -> tuple[str, str | None, str | None, str | None]:
    caption = _join_text(
        item.get("table_caption")
        or item.get("image_caption")
        or item.get("chart_caption")
        or item.get("caption")
    )
    table_html = _string(item.get("table_body")) if raw_kind == "table" else None
    latex = _string(item.get("text")) if raw_kind in {"equation", "formula"} else None
    if raw_kind == "table":
        text = table_html or caption
    elif raw_kind in {"image", "figure", "chart"}:
        text = _string(item.get("content")) or caption
    else:
        text = _string(item.get("text")) or _string(item.get("content")) or caption
    return text, latex, table_html, caption


def _normalized_kind(raw_kind: str) -> str:
    if raw_kind in {"equation", "formula"}:
        return "formula"
    if raw_kind == "table":
        return "table"
    if raw_kind in {"image", "figure", "chart"}:
        return "figure"
    if "caption" in raw_kind:
        return "caption"
    return "text"


def _safe_image_path(value: Any, assets: Mapping[Path, bytes]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    candidate = Path(*path.parts)
    if candidate in assets:
        return candidate.as_posix()
    by_name = next((asset for asset in assets if asset.name == candidate.name), None)
    return by_name.as_posix() if by_name else None


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    if not all(isinstance(item, (int, float)) for item in value):
        return None
    return tuple(float(item) for item in value)  # type: ignore[return-value]


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _join_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        text = " ".join(str(item).strip() for item in value if str(item).strip())
        return text or None
    return None


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
