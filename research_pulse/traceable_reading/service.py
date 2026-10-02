"""Narrow application seam shared by the traceable CLI and workbench NoteRun."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Callable

from research_pulse.production.mineru_api import MinerUApiConfig, MinerUApiParser
from research_pulse.production.reading import DeepSeekPaperReadingModel

from .material import load_material_document
from .pipeline import DeepSeekOperationAdapter, TraceableReadingPipeline


@dataclass(frozen=True)
class TraceableReadingRequest:
    source_id: str
    source_url: str
    domain: str
    vault_root: Path
    cache_root: Path
    material_root: Path | None = None
    pdf: Path | None = None
    # Consumers such as the workbench can generate a durable review draft
    # without making it visible to the published knowledge read model.  The
    # standalone CLI keeps the historical ``published`` default.
    publication_status: str = "published"


@dataclass(frozen=True)
class TraceableReadingOutcome:
    exit_code: int
    payload: dict[str, object]


class TraceableReadingService:
    def __init__(
        self,
        *,
        provider_factory: Callable[[], object] | None = None,
        parser_factory: Callable[[MinerUApiConfig], object] = MinerUApiParser,
    ) -> None:
        self.provider_factory = provider_factory or DeepSeekPaperReadingModel.from_environment
        self.parser_factory = parser_factory

    def run(self, request: TraceableReadingRequest) -> TraceableReadingOutcome:
        material_root = request.material_root
        try:
            if request.pdf is not None:
                token = os.environ.get("MINERU_API_TOKEN", "").strip()
                if not token:
                    raise RuntimeError("MINERU_API_TOKEN is required")
                material_root = request.cache_root / request.source_id / "material"
                self.parser_factory(MinerUApiConfig(
                    token=token,
                    base_url=os.environ.get("MINERU_API_BASE_URL", "https://mineru.net/api"),
                    model_version=os.environ.get("MINERU_MODEL_VERSION", "vlm"),
                )).parse_pdf(
                    request.pdf,
                    source_id=request.source_id,
                    source_url=request.source_url,
                    output_dir=material_root,
                )
            if material_root is None:
                raise ValueError("material root is required")
            document = load_material_document(
                material_root, source_id=request.source_id, source_url=request.source_url
            )
            receipt = TraceableReadingPipeline(
                DeepSeekOperationAdapter(self.provider_factory()), request.vault_root
            ).run(
                document,
                domain=request.domain,
                publication_status=request.publication_status,
            )
            return TraceableReadingOutcome(0, receipt.to_payload())
        except Exception as exc:
            error = safe_error(exc)
            receipt_root = request.vault_root / "receipts" / request.source_id
            receipt_root.mkdir(parents=True, exist_ok=True)
            stamp = re.sub(r"[^A-Za-z0-9._-]+", "-", datetime.now(timezone.utc).isoformat())
            path = receipt_root / f"{stamp}.failed.json"
            path.write_text(json.dumps({
                "source_id": request.source_id,
                "publication_status": "failed",
                "error": error,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return TraceableReadingOutcome(1, {
                "source_id": request.source_id,
                "publication_status": "failed",
                "error": error,
                "receipt_path": str(path),
            })


def safe_error(error: Exception) -> str:
    value = " ".join(str(error).split()) or type(error).__name__
    for pattern in (
        r"(?i)bearer\s+[A-Za-z0-9._~+\-/=]+",
        r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\bsk-[A-Za-z0-9_-]{8,}\b",
    ):
        value = re.sub(pattern, "[REDACTED]", value)
    return f"{type(error).__name__}:{value[:240]}"
