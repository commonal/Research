"""Production adapter from one discovered paper to the traceable note pipeline."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from research_pulse.production.mineru_api import MinerUApiConfig, MinerUApiParser
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import DeepSeekPaperReadingModel
from research_pulse.topics.remote_parse import _download_pdf
from research_pulse.traceable_reading.material import load_material_document
from research_pulse.traceable_reading.pipeline import DeepSeekOperationAdapter, TraceableReadingPipeline


class TraceableCandidateReader:
    """Build/reuse MinerU material and publish one traceable reading note.

    Dependencies are lazy in production so the note-only API can still start
    for browsing when model credentials are absent.  Credentials are required
    only when a candidate is actually processed.
    """

    def __init__(
        self,
        *,
        cache_root: Path,
        vault_root: Path | None = None,
        parser: Any | None = None,
        pipeline: Any | None = None,
        material_loader: Callable[..., Any] = load_material_document,
        downloader: Callable[[str, Path, int], None] = _download_pdf,
        download_timeout_seconds: int = 90,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.vault_root = Path(vault_root) if vault_root is not None else None
        self._parser = parser
        self._pipeline = pipeline
        self.material_loader = material_loader
        self.downloader = downloader
        self.download_timeout_seconds = download_timeout_seconds

    def process(self, candidate: PaperCandidate) -> dict[str, Any]:
        material_root = self.cache_root / candidate.source_id / "material"
        if not _complete_material(material_root):
            self.cache_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".traceable-pdf-", dir=self.cache_root) as temporary:
                pdf_path = Path(temporary) / "source.pdf"
                self.downloader(candidate.source_id, pdf_path, self.download_timeout_seconds)
                self._get_parser().parse_pdf(
                    pdf_path,
                    source_id=candidate.source_id,
                    source_url=candidate.source_url,
                    output_dir=material_root,
                )
        document = self.material_loader(
            material_root,
            source_id=candidate.source_id,
            source_url=candidate.source_url,
        )
        receipt = self._get_pipeline().run(document, domain=candidate.domain)
        payload = dict(receipt.to_payload())
        status = str(payload.get("publication_status") or "failed")
        payload["status"] = status
        payload["receipt_status"] = status
        return payload

    def _get_parser(self) -> Any:
        if self._parser is None:
            token = os.environ.get("MINERU_API_TOKEN", "").strip()
            self._parser = MinerUApiParser(MinerUApiConfig(
                token=token,
                base_url=os.environ.get("MINERU_API_BASE_URL", "https://mineru.net/api"),
                model_version=os.environ.get("MINERU_MODEL_VERSION", "vlm"),
                enable_formula=_env_bool("MINERU_ENABLE_FORMULA", True),
                enable_table=_env_bool("MINERU_ENABLE_TABLE", True),
                language=os.environ.get("MINERU_LANGUAGE", "ch"),
            ))
        return self._parser

    def _get_pipeline(self) -> Any:
        if self._pipeline is None:
            if self.vault_root is None:
                raise RuntimeError("vault_root is required for production traceable reading")
            provider = DeepSeekPaperReadingModel.from_environment()
            self._pipeline = TraceableReadingPipeline(
                DeepSeekOperationAdapter(provider),
                self.vault_root,
            )
        return self._pipeline


def _complete_material(root: Path) -> bool:
    required = (root / "full.md", root / "content_list.json")
    return all(path.is_file() and path.stat().st_size > 0 for path in required)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")
