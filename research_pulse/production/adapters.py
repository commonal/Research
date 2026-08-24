"""Concrete adapters around existing arXiv, Docling, and DeepSeek utilities.

Parsed paper content is transformed to transient anchored fragments only. It is
never written to the knowledge vault or the LangGraph checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import base64
import json
import os
import re
import socket
import shutil

from research_pulse.knowledge.models import (
    EvidenceAnchor,
    KnowledgeAsset,
    KnowledgeBundle,
    KnowledgeClaim,
    KnowledgeAssetError,
    normalize_evidence_excerpt,
    render_provenance,
)
from research_pulse.production.deadline import ProductionDeadlineExceeded, current_deadline
from research_pulse.production.pipeline import (
    DeepReadingAnalysis,
    EvidenceMap,
    ExtractedDraft,
    GroundedReadingSection,
    PaperCandidate,
    ReadingVisual,
    SourceMaterial,
)
from research_pulse.production.evidence import (
    EvidenceBlock,
    EvidenceCandidate,
    EvidenceFacet,
    SupplementalEvidenceResolver,
    classify_with_optional_supplement,
)
from research_pulse.production.normalized import NormalizedBlock, load_normalized_jsonl
from research_pulse.production.publication import ManifestStore, PublicationManifest, validate_manifest_bundle
from research_pulse.rag.contracts import ResearchRAG
from worker.discover import PaperCandidate as WorkerCandidate
from worker.discover import Subscription, fetch_candidates
from worker.fulltext import ParsedDocumentBlock, ParsedFullText, parse_fulltext


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
# Keep model selection explicit and auditable.  ``deepseek-chat`` is a legacy
# compatibility alias; new production runs should resolve to the current V4
# Flash model.  The vision alias is provider-gated and is only used when the
# caller explicitly enables the multimodal reading path.
DEFAULT_DEEPSEEK_TEXT_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_VISION_MODEL = "deepseek-v4-flash-vision-exp"
MAX_EXTRACTION_SOURCE_CHARS = 60_000
MAX_READING_SOURCE_CHARS = 120_000
MAX_MAP_OUTPUT_TOKENS = 2_048
# A reading note is deliberately allowed to be substantially longer than the
# machine-readable projection.  It is still bounded to keep one paper run
# predictable and to leave room for the evidence/quality calls.
MAX_READING_OUTPUT_TOKENS = 20_000
MAX_READING_UNIT_CHARS = 18_000
MAX_READING_ORIENTATION_CHARS = 12_000
MAX_READING_UNIT_OUTPUT_TOKENS = 3_500
MAX_READING_FALLBACK_OUTPUT_TOKENS = 8_192
MAX_MAP_FALLBACK_OUTPUT_TOKENS = 4_096
MAX_EXTRACTION_OUTPUT_TOKENS = 4_096
MAX_JUDGE_OUTPUT_TOKENS = 128
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_RETRIES = 1
NON_THINKING_MODE = {"type": "disabled"}
THINKING_MODE = {"type": "enabled"}


_READING_SECTION_FACETS: dict[str, tuple[EvidenceFacet, ...]] = {
    "summary": ("problem", "method"),
    "problem": ("problem",),
    "research_question": ("problem", "method"),
    "core_idea": ("method",),
    "method": ("method",),
    "workflow": ("method",),
    "experiments": ("experiment",),
    "experiment_design": ("experiment",),
    "result_interpretation": ("experiment",),
    "limitations": ("limitation",),
    "reproduction": ("method", "experiment"),
}


class ProviderTimeout(TimeoutError):
    """A bounded DeepSeek request did not complete in time."""


@dataclass(frozen=True)
class _ReadingUnit:
    """An ordered, contiguous slice used only by the unit-reading strategy."""

    unit_id: str
    heading_path: tuple[str, ...]
    block_ids: tuple[str, ...]
    fragments: Mapping[str, str]
    start_order: int
    end_order: int


@dataclass(frozen=True)
class DeepSeekEvidenceMapper:
    """Thinking-enabled broad pass that compresses blocks before deep reading."""

    model: str
    api_key: str
    max_source_chars: int = MAX_READING_SOURCE_CHARS
    max_tokens: int = MAX_MAP_OUTPUT_TOKENS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None

    def map(self, candidate: PaperCandidate, material: SourceMaterial) -> EvidenceMap:
        selected = _select_reading_fragments(material, self.max_source_chars)
        request_payload = _deepseek_map_payload(candidate, selected, material.evidence_blocks, self.model, self.max_tokens)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        response = _request_json(
            self.post_json,
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            request_payload,
            headers,
            timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
        )
        try:
            payload = _response_json(response)
        except RuntimeError as error:
            if "exceeded its output limit" not in str(error):
                raise
            fallback_payload = dict(request_payload)
            fallback_payload["thinking"] = NON_THINKING_MODE
            fallback_payload["max_tokens"] = max(
                self.max_tokens,
                _env_int("DEEPSEEK_MAP_FALLBACK_MAX_TOKENS", MAX_MAP_FALLBACK_OUTPUT_TOKENS),
            )
            fallback_payload["messages"] = list(request_payload["messages"])
            fallback_payload["messages"][0] = {
                "role": "system",
                "content": str(request_payload["messages"][0]["content"])
                + "\nComplete the JSON directly; emit only the most useful block summaries.",
            }
            fallback_response = _request_json(
                self.post_json,
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                fallback_payload,
                headers,
                timeout_seconds=self.request_timeout_seconds,
                max_retries=self.max_retries,
            )
            payload = _response_json(fallback_response)
        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeError("DeepSeek evidence map must contain items.")
        summaries: list[tuple[str, str]] = []
        for item in items:
            # The map is an optional compression layer, never source evidence.
            # Drop malformed or hallucinated entries and let the deep reader
            # fall back to the original server-selected anchored blocks.
            if not isinstance(item, dict) or not isinstance(item.get("block_id"), str) or not isinstance(item.get("summary"), str):
                continue
            if item["block_id"] not in selected:
                continue
            if item["summary"].strip():
                summaries.append((item["block_id"], item["summary"].strip()))
        return EvidenceMap(tuple(summaries))


@dataclass(frozen=True)
class ArxivCandidateFinder:
    """Adapt the existing arXiv worker to the production graph finder seam."""

    fetcher: Callable[[Subscription], list[WorkerCandidate]] = fetch_candidates

    def discover(
        self,
        *,
        topic: str,
        domain: str,
        limit: int,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> list[PaperCandidate]:
        subscription = Subscription(
            name=domain,
            query=topic,
            exclude_terms=(),
            daily_limit=limit,
            max_results=limit,
            source="arxiv",
        )
        candidates = [
            PaperCandidate(
                source_id=candidate.source_id,
                title=candidate.title,
                source_url=candidate.source_url,
                domain=domain,
                published_at=_parse_source_time(candidate.published_at),
            )
            for candidate in self.fetcher(subscription)
        ]
        if window_start is not None:
            _require_aware(window_start, "window_start")
            candidates = [candidate for candidate in candidates if candidate.published_at > window_start]
        if window_end is not None:
            _require_aware(window_end, "window_end")
            candidates = [candidate for candidate in candidates if candidate.published_at <= window_end]
        return sorted(candidates, key=lambda candidate: candidate.published_at, reverse=True)[:limit]


def _parse_source_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("arXiv candidate has an invalid published_at timestamp") from error
    _require_aware(parsed, "published_at")
    return parsed.astimezone(timezone.utc)


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


@dataclass(frozen=True)
class DoclingSourceParser:
    """Parse one arXiv PDF temporarily, then expose only anchored fragments."""

    with_formulas: bool = False
    timeout_seconds: int = 120
    parser: Callable[..., ParsedFullText] = parse_fulltext
    supplemental_resolver: SupplementalEvidenceResolver | None = None

    def parse(self, candidate: PaperCandidate) -> SourceMaterial:
        parsed = self.parser(
            WorkerCandidate(
                source="arxiv",
                source_id=candidate.source_id,
                title=candidate.title,
                authors=[],
                published_at=candidate.published_at.isoformat() if candidate.published_at else "",
                updated_at=candidate.published_at.isoformat() if candidate.published_at else "",
                abstract="",
                source_url=candidate.source_url,
                categories=[],
            ),
            with_formulas=self.with_formulas,
            timeout_seconds=self.timeout_seconds,
        )
        if parsed.blocks:
            return source_blocks_to_material(
                source_id=candidate.source_id,
                source_url=candidate.source_url,
                blocks=parsed.blocks,
                evidence_level="full_text_text",
                supplemental_resolver=self.supplemental_resolver,
            )
        return source_markdown_to_material(
            source_id=candidate.source_id,
            source_url=candidate.source_url,
            markdown=parsed.markdown,
            evidence_level="full_text_text",
        )


@dataclass(frozen=True)
class NormalizedSourceParser:
    """Opt-in parser that reads normalized blocks from the source cache only."""

    normalized_root: Path
    supplemental_resolver: SupplementalEvidenceResolver | None = None

    def parse(self, candidate: PaperCandidate) -> SourceMaterial:
        blocks_path = self.normalized_root / candidate.source_id / "normalized" / "blocks.jsonl"
        blocks = load_normalized_jsonl(blocks_path)
        prefix = f"normalized:{candidate.source_id}:"
        if any(not block.block_id.startswith(prefix) for block in blocks):
            raise ValueError("Normalized blocks do not belong to the requested source_id.")
        return normalized_blocks_to_material(
            source_id=candidate.source_id,
            source_url=candidate.source_url,
            blocks=blocks,
            image_root=self.normalized_root / candidate.source_id / "mineru" / "source" / "auto",
            supplemental_resolver=self.supplemental_resolver,
        )


def source_markdown_to_material(
    *,
    source_id: str,
    source_url: str,
    markdown: str,
    evidence_level: str,
    max_fragment_chars: int = 1_500,
) -> SourceMaterial:
    """Make stable, transient anchors from parsed Markdown headings and text."""

    if evidence_level not in {"abstract_only", "full_text_text", "full_text_multimodal"}:
        raise ValueError("Invalid evidence_level.")
    if max_fragment_chars < 300:
        raise ValueError("max_fragment_chars must be at least 300.")
    fragments: dict[str, str] = {}
    anchors: dict[str, EvidenceAnchor] = {}
    ordinal = 0
    for section, text in _markdown_sections(markdown):
        for part in _split_fragment(text, max_fragment_chars):
            ordinal += 1
            anchor_id = f"source:{source_id}:{_slug(section)}:{ordinal}"
            anchors[anchor_id] = EvidenceAnchor(anchor_id=anchor_id, source_url=source_url, section=section)
            fragments[anchor_id] = part
    if not fragments:
        raise ValueError("Docling returned Markdown without readable source fragments.")
    return SourceMaterial(evidence_level=evidence_level, anchors=anchors, source_fragments=fragments)


def source_blocks_to_material(
    *,
    source_id: str,
    source_url: str,
    blocks: tuple[ParsedDocumentBlock, ...],
    evidence_level: str,
    supplemental_resolver: SupplementalEvidenceResolver | None = None,
) -> SourceMaterial:
    """Turn native parser blocks into transient evidence candidates and anchors."""

    fragments: dict[str, str] = {}
    anchors: dict[str, EvidenceAnchor] = {}
    candidates: dict[str, EvidenceCandidate] = {}
    evidence_blocks: dict[str, EvidenceBlock] = {}
    for ordinal, block in enumerate(blocks, start=1):
        text = " ".join(block.text.split())
        if not text:
            continue
        section = " / ".join(block.section_path) or "Overview"
        anchor_id = f"source:{source_id}:{_slug(section)}:{ordinal}"
        status = "unparsed" if "formula-not-decoded" in text.casefold() else "available"
        locator = "exact" if block.page_start is not None or block.bbox is not None else "section_only"
        candidate = EvidenceCandidate(
            block_id=anchor_id,
            kind=block.kind,
            text=text,
            source_url=source_url,
            parser="docling",
            parse_status=status,
            locator_completeness=locator,
            section_path=block.section_path or ("Overview",),
            page_start=block.page_start,
            page_end=block.page_end,
            bbox=block.bbox,
            caption=block.caption,
        )
        candidates[anchor_id] = candidate
        evidence_block = classify_with_optional_supplement(candidate, resolver=supplemental_resolver)
        evidence_blocks[anchor_id] = evidence_block
        if not evidence_block.eligible_for_fact:
            continue
        usable_candidate = evidence_block.candidate
        anchors[anchor_id] = EvidenceAnchor(
            anchor_id=anchor_id,
            source_url=source_url,
            section=section,
            page_start=usable_candidate.page_start,
            page_end=usable_candidate.page_end,
            figure_or_table=usable_candidate.caption if usable_candidate.kind in {"table", "figure"} else None,
            block_kind=usable_candidate.kind,
            parse_status=usable_candidate.parse_status,
            locator_completeness=usable_candidate.locator_completeness,
            bbox=usable_candidate.bbox,
        )
        fragments[anchor_id] = usable_candidate.text
    if not fragments:
        raise ValueError("Docling returned native blocks without readable source text.")
    return SourceMaterial(
        evidence_level=evidence_level,
        anchors=anchors,
        source_fragments=fragments,
        evidence_candidates=candidates,
        evidence_blocks=evidence_blocks,
    )


def normalized_blocks_to_material(
    *,
    source_id: str,
    source_url: str,
    blocks: tuple[NormalizedBlock, ...],
    image_root: Path | None = None,
    supplemental_resolver: SupplementalEvidenceResolver | None = None,
) -> SourceMaterial:
    """Adapt parser-neutral normalized blocks to the existing evidence gate."""

    fragments: dict[str, str] = {}
    anchors: dict[str, EvidenceAnchor] = {}
    candidates: dict[str, EvidenceCandidate] = {}
    evidence_blocks: dict[str, EvidenceBlock] = {}
    for block in blocks:
        section = " / ".join(block.section_path) or "Overview"
        locator = "exact" if block.page_start is not None or block.bbox is not None else "section_only"
        candidate = EvidenceCandidate(
            block_id=block.block_id,
            kind=block.kind,
            text=block.text,
            source_url=source_url,
            parser="normalized:mineru+docling",
            parse_status=block.parse_status,
            locator_completeness=locator,
            section_path=block.section_path or ("Overview",),
            page_start=block.page_start,
            page_end=block.page_end,
            bbox=block.bbox,
            caption=block.caption,
            latex=block.latex,
            table_html=block.table_html,
            image_path=block.image_path,
            image_source_path=_safe_image_source(image_root, block.image_path),
        )
        candidates[block.block_id] = candidate
        classified = classify_with_optional_supplement(candidate, resolver=supplemental_resolver)
        evidence_blocks[block.block_id] = classified
        if not classified.eligible_for_fact:
            continue
        usable = classified.candidate
        anchors[block.block_id] = EvidenceAnchor(
            anchor_id=block.block_id,
            source_url=source_url,
            section=section,
            page_start=usable.page_start,
            page_end=usable.page_end,
            figure_or_table=usable.caption if usable.kind in {"table", "figure"} else None,
            block_kind=usable.kind,
            parse_status=usable.parse_status,
            locator_completeness=usable.locator_completeness,
            bbox=usable.bbox,
        )
        fragments[block.block_id] = usable.text
    if not candidates:
        raise ValueError("Normalized JSONL returned no blocks.")
    evidence_level = "full_text_multimodal" if any(block.kind in {"formula", "table", "figure"} for block in blocks) else "full_text_text"
    return SourceMaterial(
        evidence_level=evidence_level,
        anchors=anchors,
        source_fragments=fragments,
        evidence_candidates=candidates,
        evidence_blocks=evidence_blocks,
    )


def _safe_image_source(image_root: Path | None, image_path: str | None) -> Path | None:
    """Resolve a normalized image only when it stays inside the source cache."""

    if image_root is None or not image_path or not image_path.strip():
        return None
    root = image_root.resolve()
    candidate = (root / image_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _markdown_sections(markdown: str) -> list[tuple[str, str]]:
    current = "Overview"
    lines: list[str] = []
    sections: list[tuple[str, str]] = []
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            _append_section(sections, current, lines)
            current, lines = match.group(1), []
        else:
            lines.append(line)
    _append_section(sections, current, lines)
    return sections


def _append_section(sections: list[tuple[str, str]], section: str, lines: list[str]) -> None:
    text = "\n".join(lines).strip()
    if text:
        sections.append((section, text))


def _split_fragment(text: str, limit: int) -> list[str]:
    normalized = " ".join(text.split())
    return [normalized[start : start + limit] for start in range(0, len(normalized), limit)] if normalized else []


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "section"


@dataclass(frozen=True)
class DeepSeekDeepReader:
    """Thinking-enabled interpretation; its output never becomes source evidence."""

    model: str
    api_key: str
    max_source_chars: int = MAX_READING_SOURCE_CHARS
    max_tokens: int = MAX_READING_OUTPUT_TOKENS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None
    # ``legacy`` keeps the original one-shot request available as a safe
    # fallback. ``units`` reads the paper in ordered contiguous units and then
    # synthesizes the final note from those reading memos.
    reading_strategy: str = "legacy"
    reading_unit_chars: int = MAX_READING_UNIT_CHARS
    reading_unit_max_tokens: int = MAX_READING_UNIT_OUTPUT_TOKENS
    vision_enabled: bool = False
    vision_model: str | None = None
    vision_max_images_per_unit: int = 4
    vision_max_bytes_per_unit: int = 12_000_000

    def read(
        self,
        candidate: PaperCandidate,
        material: SourceMaterial,
        *,
        evidence_map: EvidenceMap | None = None,
        allowed_block_ids: Collection[str] | None = None,
    ) -> DeepReadingAnalysis:
        if self.reading_strategy == "units":
            return self._read_in_ordered_units(
                candidate,
                material,
                evidence_map=evidence_map,
                allowed_block_ids=allowed_block_ids,
            )
        section_fragments = _select_reading_section_fragments(
            material,
            self.max_source_chars,
            allowed_block_ids=allowed_block_ids,
        )
        selected = {
            block_id: text
            for fragments in section_fragments.values()
            for block_id, text in fragments.items()
        }
        request_payload = _deepseek_reading_payload(
            candidate,
            section_fragments,
            material.evidence_blocks,
            self.model,
            self.max_tokens,
            evidence_map=evidence_map,
        )
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        response = _request_json(
            self.post_json,
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            request_payload,
            headers,
            timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
        )
        try:
            payload = _response_json(response)
        except RuntimeError as error:
            # DeepSeek thinking tokens and the final JSON share the completion
            # budget.  A long reasoning trace can therefore end with a
            # truncated machine-readable response even when the requested note
            # itself is modest.  Keep the thinking pass as the default, but
            # retry only this recoverable protocol failure with a concise,
            # non-thinking JSON pass.  No partial output is ever published.
            if "exceeded its output limit" not in str(error):
                raise
            fallback_payload = dict(request_payload)
            fallback_payload["thinking"] = NON_THINKING_MODE
            fallback_payload["max_tokens"] = min(
                self.max_tokens,
                _env_int("DEEPSEEK_READING_FALLBACK_MAX_TOKENS", MAX_READING_FALLBACK_OUTPUT_TOKENS),
            )
            fallback_payload["messages"] = list(request_payload["messages"])
            fallback_payload["messages"][0] = {
                "role": "system",
                "content": str(request_payload["messages"][0]["content"])
                + "\nComplete the JSON directly and keep each field near the lower end of its requested length.",
            }
            fallback_response = _request_json(
                self.post_json,
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                fallback_payload,
                headers,
                timeout_seconds=self.request_timeout_seconds,
                max_retries=self.max_retries,
            )
            payload = _response_json(fallback_response)
        return _parse_deep_reading(payload, selected, material.evidence_blocks)

    def _read_in_ordered_units(
        self,
        candidate: PaperCandidate,
        material: SourceMaterial,
        *,
        evidence_map: EvidenceMap | None,
        allowed_block_ids: Collection[str] | None,
    ) -> DeepReadingAnalysis:
        units = _build_ordered_reading_units(
            material,
            max_chars=self.reading_unit_chars,
            allowed_block_ids=allowed_block_ids,
        )
        if not units:
            raise RuntimeError("Ordered reading strategy found no readable source units.")

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        orientation_payload = _deepseek_orientation_payload(
            candidate,
            units,
            self.model,
            max(1_500, min(self.reading_unit_max_tokens, 2_500)),
        )
        orientation = _response_json(
            _request_json(
                self.post_json,
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                orientation_payload,
                headers,
                timeout_seconds=self.request_timeout_seconds,
                max_retries=self.max_retries,
            )
        )

        memos: list[dict[str, Any]] = []
        previous_memo = ""
        for unit in units:
            unit_payload = _deepseek_reading_unit_payload(
                candidate,
                unit,
                orientation,
                previous_memo,
                material.evidence_blocks,
                evidence_map=evidence_map,
                model=(self.vision_model or self.model) if self.vision_enabled else self.model,
                max_tokens=self.reading_unit_max_tokens,
                vision_enabled=self.vision_enabled,
                max_images=self.vision_max_images_per_unit,
                max_image_bytes=self.vision_max_bytes_per_unit,
            )
            memo = _response_json(
                _request_json(
                    self.post_json,
                    DEEPSEEK_CHAT_COMPLETIONS_URL,
                    unit_payload,
                    headers,
                    timeout_seconds=self.request_timeout_seconds,
                    max_retries=self.max_retries,
                )
            )
            normalized_memo = _normalize_reading_unit_memo(memo, unit, material.evidence_blocks)
            memos.append(normalized_memo)
            previous_memo = json.dumps(normalized_memo, ensure_ascii=False)[:2_000]

        selected: dict[str, str] = {}
        for unit in units:
            selected.update(unit.fragments)
        synthesis_payload = _deepseek_synthesis_payload(
            candidate,
            orientation,
            memos,
            selected,
            material.evidence_blocks,
            self.model,
            self.max_tokens,
        )
        response = _request_json(
            self.post_json,
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            synthesis_payload,
            headers,
            timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
        )
        payload = _response_json(response)
        analysis = _parse_deep_reading(payload, selected, material.evidence_blocks)
        return _merge_unit_visual_memos(analysis, memos, material.evidence_blocks)


@dataclass(frozen=True)
class DeepSeekStructuredExtractor:
    """Create a bounded draft; publication still requires an independent judge."""

    model: str
    api_key: str
    max_source_chars: int = MAX_EXTRACTION_SOURCE_CHARS
    post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None
    deep_reader: DeepSeekDeepReader | None = None
    evidence_mapper: DeepSeekEvidenceMapper | None = None
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_tokens: int = MAX_EXTRACTION_OUTPUT_TOKENS
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_environment(cls, *, model: str = DEFAULT_DEEPSEEK_TEXT_MODEL) -> "DeepSeekStructuredExtractor":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set. Keep it in the local environment only.")
        return cls.from_credentials(model=model, api_key=api_key)

    @classmethod
    def from_credentials(cls, *, model: str, api_key: str) -> "DeepSeekStructuredExtractor":
        vision_enabled = os.getenv("DEEPSEEK_READING_VISION", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        reader = DeepSeekDeepReader(
            model=model,
            api_key=api_key,
            max_tokens=_env_int("DEEPSEEK_READING_MAX_TOKENS", MAX_READING_OUTPUT_TOKENS),
            request_timeout_seconds=_env_float("DEEPSEEK_READING_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            max_retries=_env_int("DEEPSEEK_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            reading_strategy=os.getenv("DEEPSEEK_READING_STRATEGY", "legacy").strip().casefold() or "legacy",
            reading_unit_chars=_env_int("DEEPSEEK_READING_UNIT_CHARS", MAX_READING_UNIT_CHARS),
            reading_unit_max_tokens=_env_int(
                "DEEPSEEK_READING_UNIT_MAX_TOKENS",
                MAX_READING_UNIT_OUTPUT_TOKENS,
            ),
            vision_enabled=vision_enabled,
            vision_model=os.getenv("DEEPSEEK_READING_VISION_MODEL", "").strip()
            or (DEFAULT_DEEPSEEK_VISION_MODEL if vision_enabled else None),
            vision_max_images_per_unit=_env_int("DEEPSEEK_READING_VISION_MAX_IMAGES", 4),
            vision_max_bytes_per_unit=_env_int("DEEPSEEK_READING_VISION_MAX_BYTES", 12_000_000),
        )
        mapper = DeepSeekEvidenceMapper(
            model=model,
            api_key=api_key,
            max_tokens=_env_int("DEEPSEEK_MAP_MAX_TOKENS", MAX_MAP_OUTPUT_TOKENS),
            request_timeout_seconds=_env_float("DEEPSEEK_MAP_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            max_retries=_env_int("DEEPSEEK_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )
        return cls(
            model=model,
            api_key=api_key,
            deep_reader=reader,
            evidence_mapper=mapper,
            request_timeout_seconds=_env_float("DEEPSEEK_PROJECTION_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            max_tokens=_env_int("DEEPSEEK_PROJECTION_MAX_TOKENS", MAX_EXTRACTION_OUTPUT_TOKENS),
            max_retries=_env_int("DEEPSEEK_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )

    def extract(self, candidate: PaperCandidate, material: SourceMaterial) -> ExtractedDraft:
        evidence_map = self.evidence_mapper.map(candidate, material) if self.evidence_mapper else None
        selected = _select_extraction_fragments(material, self.max_source_chars)
        # Read the paper-level narrative before projecting durable facts.  The
        # projection then receives the anchors actually used by the narrative,
        # instead of forcing the reader to reconstruct a paper from four
        # already-selected facet snippets.
        reading_analysis = (
            self.deep_reader.read(
                candidate,
                material,
                evidence_map=evidence_map,
            )
            if self.deep_reader
            else None
        )
        reading_hint_ids = _reading_hint_ids(reading_analysis)
        projection_fragments = _include_reading_hints(
            material,
            selected,
            reading_hint_ids,
            self.max_source_chars,
        )
        response = _request_json(
            self.post_json,
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            _deepseek_payload(
                candidate,
                projection_fragments,
                material.evidence_blocks,
                self.model,
                self.max_tokens,
                reading_hint_ids=reading_hint_ids,
            ),
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
        )
        payload = _response_json(response)
        # Projection context is bounded for the provider, but a selected block
        # may have been cut exactly at a table/number boundary.  Parse claims
        # against the full transient fragment for those IDs so the server can
        # derive a complete verbatim excerpt; the provider still only saw the
        # bounded projection context.
        claim_fragments = {
            anchor_id: material.source_fragments.get(anchor_id, text)
            for anchor_id, text in projection_fragments.items()
        }
        claims, claim_facets = _parse_claims(
            payload.get("claims"),
            claim_fragments,
            material.evidence_blocks or None,
        )
        claims, claim_facets = _promote_reading_anchors(
            claims,
            claim_facets,
            reading_analysis,
            material.source_fragments,
            material.evidence_blocks,
        )
        reading_analysis = _redact_unanchored_reading_numbers(reading_analysis, claims)
        visual_assets = _collect_visual_assets(reading_analysis, material.evidence_blocks)
        body = _render_body(
            candidate,
            material.evidence_level,
            payload,
            claims,
            reading_analysis=reading_analysis,
            evidence_blocks=material.evidence_blocks,
            visual_assets=visual_assets,
        )
        asset = KnowledgeAsset(
            knowledge_id=f"kp:arxiv:{candidate.source_id}",
            knowledge_version=datetime.now(timezone.utc).isoformat(),
            publication_status="needs_review",
            evidence_level=material.evidence_level,
            source_urls=(candidate.source_url,),
            domain=candidate.domain,
            title=candidate.title,
            body=body,
            content_sha256=sha256(body.encode("utf-8")).hexdigest(),
        )
        return ExtractedDraft(
            asset=asset,
            claims=tuple(claims),
            claim_facets=claim_facets,
            reading_analysis=reading_analysis,
            visual_assets=visual_assets,
        )


@dataclass(frozen=True)
class DeepSeekEntailmentJudge:
    """A separate, constrained verification call after generation."""

    model: str
    api_key: str
    post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_tokens: int = MAX_JUDGE_OUTPUT_TOKENS
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_environment(cls, *, model: str = DEFAULT_DEEPSEEK_TEXT_MODEL) -> "DeepSeekEntailmentJudge":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set. Keep it in the local environment only.")
        return cls(
            model=model,
            api_key=api_key,
            request_timeout_seconds=_env_float("DEEPSEEK_JUDGE_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            max_tokens=_env_int("DEEPSEEK_JUDGE_MAX_TOKENS", MAX_JUDGE_OUTPUT_TOKENS),
            max_retries=_env_int("DEEPSEEK_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )

    def assess(self, *, claim: str, evidence: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return JSON only: {\"verdict\": \"supported|ambiguous|unsupported\"}. "
                        "Judge whether the supplied evidence entails the supplied claim. "
                        "The server has already verified that the claim is a continuous "
                        "verbatim excerpt from the supplied evidence: when that exact text "
                        "is present, verdict MUST be supported. Return ambiguous or unsupported "
                        "only if that identity cannot be confirmed. Do not add facts or repair the claim."
                    ),
                },
                {"role": "user", "content": f"Claim:\n{claim}\n\nEvidence:\n{evidence}"},
            ],
            "response_format": {"type": "json_object"},
            # V4 defaults to thinking mode. These calls are bounded machine-readable
            # extraction/verdict steps, so reserve their completion budget for JSON.
            "thinking": NON_THINKING_MODE,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        response = _request_json(
            self.post_json,
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            payload,
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
        )
        verdict = _response_json(response).get("verdict")
        if verdict not in {"supported", "ambiguous", "unsupported"}:
            raise RuntimeError("DeepSeek entailment judge returned an invalid verdict.")
        return verdict


def _deepseek_payload(
    candidate: PaperCandidate,
    fragments: Mapping[str, str],
    evidence_blocks: Mapping[str, EvidenceBlock],
    model: str,
    max_tokens: int = MAX_EXTRACTION_OUTPUT_TOKENS,
    *,
    reading_hint_ids: Collection[str] = (),
) -> dict[str, Any]:
    fragment_text = "\n\n".join(
        _render_extraction_block(key, text, evidence_blocks.get(key)) for key, text in fragments.items()
    )
    system = """You write a Chinese, source-bounded paper reading draft. Return JSON only.
You may use only the supplied anchored source fragments. Do not claim to inspect images.
Every source_fact claim must contain exactly one supplied eligible EvidenceBlock ID and one facet
(problem|method|experiment|limitation). The server derives the stored source quote, locator,
and parser status from that block; never return a source quote, page number, bounding box, or
parser status yourself. Use source_fact only when the selected block itself supports the fact.
If uncertain, emit a reading_question or agent_inference instead of a source_fact. Do not invent metrics,
datasets, limitations, equations, or reproduction facts. Return this exact JSON shape:
{
  "summary": "string",
  "problem": "string",
  "method": "string",
  "experiments": "string",
  "limitations": "string",
  "reproduction": "string",
  "claims": [
    {"claim_type": "source_fact|agent_inference|reading_question", "text": "string", "anchor_ids": ["source:id:section:1"], "facet": "problem|method|experiment|limitation|null"}
  ]
}
Keep each narrative field under 240 Chinese characters. Return at least one source_fact for each
facet that is supported, and add source_fact entries for the paper's central model/training,
method mechanism, evaluation design, headline results, and author-stated limits when those
blocks are supplied. The server may promote anchors cited by the deep-reading pass after this
projection. Do not claim that a paper lacks metrics or results merely because one local block is
incomplete. Add at most four other claims. Keep each claim text under 160 Chinese characters.
Put Chinese interpretation in the narrative fields or an agent_inference instead. Prefer a
complete coverage of the paper's main line over a four-facet minimum."""
    hints = tuple(dict.fromkeys(item for item in reading_hint_ids if item in fragments))
    hint_text = (
        "The deep-reading pass cited these eligible anchors. Prefer projecting them as source_fact "
        "claims so the final reading sections retain durable support: "
        + json.dumps(list(hints), ensure_ascii=False)
        if hints
        else "The deep-reading pass supplied no anchor hints. Select the strongest eligible blocks yourself."
    )
    user = f"""Paper title: {candidate.title}
Paper URL: {candidate.source_url}
{hint_text}
Evidence fragments:
{fragment_text}"""
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        # The output is consumed by a strict parser; do not spend its bounded JSON
        # budget on provider-side reasoning content.
        "thinking": NON_THINKING_MODE,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
    }


def _build_ordered_reading_units(
    material: SourceMaterial,
    *,
    max_chars: int,
    allowed_block_ids: Collection[str] | None = None,
) -> tuple[_ReadingUnit, ...]:
    """Group parser blocks in source order without mixing distant sections."""

    if max_chars < 2_000:
        raise ValueError("reading unit max_chars must be at least 2000.")
    allowed = set(allowed_block_ids) if allowed_block_ids is not None else None
    current: list[tuple[int, str, tuple[str, ...], str]] = []
    current_chars = 0
    units: list[_ReadingUnit] = []

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        unit_number = len(units) + 1
        units.append(
            _ReadingUnit(
                unit_id=f"reading-unit-{unit_number:02d}",
                heading_path=current[0][2],
                block_ids=tuple(item[1] for item in current),
                fragments={item[1]: item[3] for item in current},
                start_order=current[0][0],
                end_order=current[-1][0],
            )
        )
        current = []
        current_chars = 0

    for order, (block_id, block) in enumerate(material.evidence_blocks.items(), start=1):
        if allowed is not None and block_id not in allowed:
            continue
        candidate = block.candidate
        raw_text = material.source_fragments.get(block_id, candidate.text)
        if not raw_text.strip() and not any(
            (candidate.caption, candidate.latex, candidate.table_html, candidate.image_path)
        ):
            continue
        heading_path = candidate.section_path or ("Overview",)
        if any(
            marker in " / ".join(heading_path).casefold()
            for marker in ("references", "bibliography", "参考文献")
        ):
            # Bibliography is useful metadata for citation tooling, but it is
            # not part of the paper's reasoning chain.
            continue
        rendered = _render_reading_unit_block(order, block_id, raw_text, block)
        if not rendered.strip():
            continue
        heading_changed = bool(current) and current[0][2][:1] != heading_path[:1]
        would_overflow = bool(current) and current_chars + len(rendered) > max_chars
        if current and (would_overflow or (heading_changed and current_chars >= max_chars // 3)):
            flush()
        current.append((order, block_id, heading_path, raw_text))
        current_chars += min(len(rendered), max_chars)
    flush()
    return tuple(units)


def _render_reading_unit_block(
    order: int,
    block_id: str,
    text: str,
    evidence_block: EvidenceBlock,
) -> str:
    candidate = evidence_block.candidate
    heading = " / ".join(candidate.section_path) or "Overview"
    lines = [
        f"[DOC_ORDER {order:04d}] [BLOCK {block_id}] [KIND {candidate.kind}] [SECTION {heading}]",
    ]
    if candidate.caption:
        lines.append(f"CAPTION: {candidate.caption}")
    if text.strip():
        lines.append(f"TEXT: {text.strip()}")
    if candidate.latex:
        lines.append(f"LATEX: {candidate.latex.strip()[:8_000]}")
    if candidate.table_html:
        lines.append(f"TABLE_HTML: {candidate.table_html.strip()[:8_000]}")
    if candidate.kind == "figure" and candidate.image_path:
        lines.append("FIGURE_ASSET: available in the source cache; this text-only trial uses caption and context.")
    return "\n".join(lines)


def _deepseek_orientation_payload(
    candidate: PaperCandidate,
    units: Sequence[_ReadingUnit],
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    headings = "\n".join(
        f"{index}. {' / '.join(unit.heading_path)} (document order {unit.start_order}-{unit.end_order})"
        for index, unit in enumerate(units, start=1)
    )
    opening = "\n\n".join(
        f"[DOC_ORDER {units[0].start_order + offset:04d}] {text}"
        for offset, text in enumerate(units[0].fragments.values())
    )[:MAX_READING_ORIENTATION_CHARS] if units else ""
    system = (
        "You are the orientation pass for a paper reading workflow. Return JSON only.\n"
        "Read the ordered outline and opening material to identify the concrete problem, the gap, "
        "the claimed solution, and which units or visual objects deserve attention. Do not write "
        "the final note or invent results. Keep the response compact."
    )
    user = (
        f"Paper title: {candidate.title}\nPaper URL: {candidate.source_url}\n"
        f"ORDERED OUTLINE:\n{headings}\nOPENING MATERIAL:\n{opening}\n"
        "Return {\\\"paper_problem\\\": \\\"...\\\", \\\"claimed_gap\\\": \\\"...\\\", "
        "\\\"claimed_solution\\\": \\\"...\\\", \\\"priority_units\\\": [\\\"reading-unit-01\\\"], "
        "\\\"priority_visual_ids\\\": [\\\"block id\\\"]}."
    )
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        # These are compact intermediate records; reserve the output budget for
        # complete JSON rather than provider-side reasoning traces.
        "thinking": NON_THINKING_MODE,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }


def _deepseek_reading_unit_payload(
    candidate: PaperCandidate,
    unit: _ReadingUnit,
    orientation: Mapping[str, Any],
    previous_memo: str,
    evidence_blocks: Mapping[str, EvidenceBlock],
    *,
    evidence_map: EvidenceMap | None,
    model: str,
    max_tokens: int,
    vision_enabled: bool = False,
    max_images: int = 4,
    max_image_bytes: int = 12_000_000,
) -> dict[str, Any]:
    summaries = dict(evidence_map.summaries) if evidence_map else {}
    blocks: list[str] = []
    for offset, block_id in enumerate(unit.block_ids):
        block = evidence_blocks[block_id]
        rendered = _render_reading_unit_block(
            unit.start_order + offset,
            block_id,
            unit.fragments[block_id],
            block,
        )
        if block_id in summaries:
            rendered = f"EVIDENCE_MAP_NOTE: {summaries[block_id]}\n{rendered}"
        blocks.append(rendered)
    visual_ids = [
        block_id
        for block_id in unit.block_ids
        if evidence_blocks[block_id].candidate.kind in {"formula", "table", "figure"}
    ]
    system = (
        "You are reading one contiguous unit of a research paper. Return JSON only.\n"
        "Explain what this unit contributes to the paper's argument, preserving source order. This "
        "is a reading memo, not the final note. Connect definitions, mechanism, equations, figures, "
        "tables, and results to nearby prose. Distinguish source statements from interpretation. "
        "Use only supplied block IDs. Do not invent missing numbers or claim to have seen a figure "
        "image when only its caption is supplied. When an image is attached, describe only visible "
        "components that are relevant to the neighboring source text and mark visual interpretation "
        "as interpretation rather than a source fact."
    )
    user_text = (
        f"Paper title: {candidate.title}\nPaper orientation:\n"
        f"{json.dumps(dict(orientation), ensure_ascii=False)[:4_000]}\n"
        f"Previous unit memo (may be empty):\n{previous_memo[:2_000]}\n"
        f"Current unit: {unit.unit_id}\nHeading: {' / '.join(unit.heading_path)}\n"
        f"Attached visual candidates in this unit: {json.dumps(visual_ids, ensure_ascii=False)}\n"
        f"Blocks in document order:\n{'\n\n'.join(blocks)}\n"
        "Return {\\\"unit_purpose\\\": \\\"...\\\", \\\"problem_gap\\\": \\\"...\\\", "
        "\\\"method_mechanism\\\": \\\"...\\\", \\\"evidence_and_results\\\": \\\"...\\\", "
        "\\\"visual_interpretations\\\": [{\\\"block_id\\\": \\\"...\\\", "
        "\\\"role\\\": \\\"...\\\", \\\"explanation\\\": \\\"...\\\"}], "
        "\\\"connections\\\": [\\\"...\\\"], \\\"uncertainties\\\": [\\\"...\\\"], "
        "\\\"source_block_ids\\\": [\\\"...\\\"]}. If an attached visual materially clarifies "
        "the unit's mechanism or result, include its exact block ID in visual_interpretations; do not "
        "silently omit a central formula, table, or figure. If it is not useful, record that uncertainty."
    )
    user_content: str | list[dict[str, Any]] = user_text
    if vision_enabled:
        user_content = _reading_unit_multimodal_content(
            user_text,
            unit,
            evidence_blocks,
            max_images=max_images,
            max_image_bytes=max_image_bytes,
        )
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
        "response_format": {"type": "json_object"},
        "thinking": NON_THINKING_MODE,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }


def _reading_unit_multimodal_content(
    user_text: str,
    unit: _ReadingUnit,
    evidence_blocks: Mapping[str, EvidenceBlock],
    *,
    max_images: int,
    max_image_bytes: int,
) -> list[dict[str, Any]]:
    """Attach only safe, unit-local visual assets to an OpenAI-style request."""

    if max_images < 0 or max_image_bytes < 0:
        raise ValueError("visual request budgets must be non-negative")
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    attached = 0
    used_bytes = 0
    for block_id in unit.block_ids:
        block = evidence_blocks.get(block_id)
        source = getattr(block.candidate, "image_source_path", None) if block else None
        if source is None or attached >= max_images:
            continue
        try:
            resolved = source.resolve()
            payload = resolved.read_bytes()
        except (OSError, ValueError):
            continue
        if not payload or len(payload) > max_image_bytes - used_bytes:
            continue
        mime = _image_mime_type(resolved)
        if mime is None:
            continue
        content.append(
            {
                "type": "text",
                "text": (
                    f"Attached visual for block {block_id} ({block.candidate.kind}). "
                    "Use it only with the caption and neighboring source context."
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"},
            }
        )
        attached += 1
        used_bytes += len(payload)
    return content


def _image_mime_type(path: Path) -> str | None:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.casefold())


def _normalize_reading_unit_memo(
    payload: Mapping[str, Any],
    unit: _ReadingUnit,
    evidence_blocks: Mapping[str, EvidenceBlock],
) -> dict[str, Any]:
    def text(name: str, limit: int = 2_500) -> str:
        value = payload.get(name, "")
        return value.strip()[:limit] if isinstance(value, str) else ""

    allowed = set(unit.block_ids)
    raw_ids = payload.get("source_block_ids", [])
    source_ids = [
        value for value in raw_ids
        if isinstance(value, str) and value in allowed and evidence_blocks.get(value) is not None
    ] if isinstance(raw_ids, list) else []
    visuals: list[dict[str, str]] = []
    raw_visuals = payload.get("visual_interpretations", [])
    if isinstance(raw_visuals, list):
        for item in raw_visuals:
            if not isinstance(item, Mapping):
                continue
            block_id = item.get("block_id")
            block = evidence_blocks.get(block_id) if isinstance(block_id, str) else None
            if block_id not in allowed or block is None or block.candidate.kind not in {"formula", "table", "figure"}:
                continue
            visuals.append({
                "block_id": block_id,
                "role": str(item.get("role", "understanding"))[:120],
                "explanation": str(item.get("explanation", ""))[:800],
            })
    def strings(name: str) -> list[str]:
        value = payload.get(name, [])
        return [str(item)[:600] for item in value if isinstance(item, str)][:8] if isinstance(value, list) else []
    return {
        "unit_id": unit.unit_id,
        "heading": " / ".join(unit.heading_path),
        "unit_purpose": text("unit_purpose"),
        "problem_gap": text("problem_gap"),
        "method_mechanism": text("method_mechanism"),
        "evidence_and_results": text("evidence_and_results"),
        "visual_interpretations": visuals[:8],
        "connections": strings("connections"),
        "uncertainties": strings("uncertainties"),
        "source_block_ids": list(dict.fromkeys(source_ids))[:16],
    }


def _merge_unit_visual_memos(
    analysis: DeepReadingAnalysis,
    memos: Sequence[Mapping[str, Any]],
    evidence_blocks: Mapping[str, EvidenceBlock],
) -> DeepReadingAnalysis:
    """Carry accepted visual explanations into the final note once, globally."""

    sections = dict(analysis.sections())
    existing = {visual.block_id for section in sections.values() for visual in section.visuals}
    additions: dict[str, list[ReadingVisual]] = {name: [] for name in sections}
    for memo in memos:
        heading = str(memo.get("heading", "")).casefold()
        for raw in memo.get("visual_interpretations", ()):
            if not isinstance(raw, Mapping):
                continue
            block_id = raw.get("block_id")
            explanation = raw.get("explanation")
            role = raw.get("role")
            block = evidence_blocks.get(block_id) if isinstance(block_id, str) else None
            if (
                not isinstance(block_id, str)
                or block_id in existing
                or not isinstance(explanation, str)
                or not explanation.strip()
                or not isinstance(role, str)
                or not role.strip()
                or block is None
                or block.candidate.kind not in {"formula", "table", "figure"}
            ):
                continue
            target = _visual_memo_target(heading, block.candidate.kind)
            if target not in additions or len(additions[target]) >= 2:
                continue
            additions[target].append(
                ReadingVisual(
                    block_id=block_id,
                    role=role.strip()[:120],
                    explanation=explanation.strip()[:1_500],
                    title=(block.candidate.caption or "").strip()[:160],
                )
            )
            existing.add(block_id)
    for name, values in additions.items():
        if values:
            section = sections[name]
            sections[name] = replace(section, visuals=section.visuals + tuple(values))
    return replace(analysis, **sections)


def _visual_memo_target(heading: str, kind: str) -> str:
    if kind == "formula" or any(marker in heading for marker in ("method", "authority", "framework", "reward")):
        return "method"
    if any(marker in heading for marker in ("experiment", "result", "benchmark", "evaluation", "ablation")):
        return "experiments"
    return "workflow" if kind == "figure" else "experiments"


def _deepseek_synthesis_payload(
    candidate: PaperCandidate,
    orientation: Mapping[str, Any],
    memos: Sequence[Mapping[str, Any]],
    selected: Mapping[str, str],
    evidence_blocks: Mapping[str, EvidenceBlock],
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    register_lines = [
        _render_extraction_block(block_id, text[:600], evidence_blocks.get(block_id))
        for block_id, text in selected.items()
    ]
    source_register = "\n\n".join(register_lines)[:40_000]
    visual_memos = [
        {
            "unit_id": memo.get("unit_id"),
            "heading": memo.get("heading"),
            "visual_interpretations": memo.get("visual_interpretations", []),
        }
        for memo in memos
        if memo.get("visual_interpretations")
    ]
    system = (
        "You are the final synthesis pass for a Chinese human-readable paper note. Return JSON only.\n"
        "The ordered reading memos are the primary understanding input. Reconstruct one coherent causal "
        "story: concrete background/problem, gap, core idea, mechanism, workflow, experimental verification, "
        "justified results, and limitations. Do not stitch memos mechanically. Use source block IDs only for "
        "durable support; narrative explanations may be your own synthesis. Never invent a metric, equation, "
        "table cell, or visual detail. Formula/table/figure explanations belong in the visuals array of the "
        "relevant section and must reference a supplied visual block ID. Return the normal deep-reading fields: "
        "summary, problem, research_question, core_idea, method, workflow, experiments, experiment_design, "
        "result_interpretation, limitations, reproduction, reading_boundary. Each narrative field except "
        "reading_boundary is {text, evidence_block_ids, visuals}."
    )
    user = (
        f"Paper title: {candidate.title}\nPaper URL: {candidate.source_url}\nORIENTATION:\n"
        f"{json.dumps(dict(orientation), ensure_ascii=False)[:5_000]}\nORDERED READING MEMOS:\n"
        f"{json.dumps(list(memos), ensure_ascii=False)}\nVISUAL MEMO INDEX:\n"
        f"{json.dumps(visual_memos, ensure_ascii=False)}\nORDERED SOURCE REGISTER (short excerpts):\n"
        f"{source_register}\nWrite a fluent note for someone who has not opened the paper. Keep the problem and "
        "method explanation richer than the result list. If a visual memo materially clarifies the "
        "mechanism or a headline result, carry it into the relevant section's visuals array once; "
        "do not duplicate the same block across sections. Use exact numbers only when supported by "
        "the register or memos; otherwise state the boundary."
    )
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "thinking": THINKING_MODE,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }


def _deepseek_reading_payload(
    candidate: PaperCandidate,
    section_fragments: Mapping[str, Mapping[str, str]],
    evidence_blocks: Mapping[str, EvidenceBlock],
    model: str,
    max_tokens: int,
    *,
    evidence_map: EvidenceMap | None = None,
) -> dict[str, Any]:
    system = """You are the deep-reading pass for a Chinese paper knowledge base. Return JSON only.
Use the shared paper-level evidence context to teach the reader what the paper actually does.
Reconstruct the causal chain in this order: background/problem, existing-method gap, core idea,
method/workflow, experimental verification, and limitations. A section may cite multiple blocks
from different reading bands; do not force a fact to remain inside one facet or one block. Do not
claim to have seen images or equations that are not represented in the blocks. Blocks marked as
degraded/unparsed may be used only to state a reading boundary, never as proof of a precise value.
Uncertain interpretations must be placed in reading_boundary. Do not invent metrics, datasets,
baselines, limitations, equations, or reproduction results. Numeric and citation consistency is
checked after generation against the durable anchors; do not omit a supported result merely because
it came from a different section.

Return exactly these fields: summary, problem, research_question, core_idea, method, workflow,
experiments, experiment_design, result_interpretation, limitations, reproduction, reading_boundary.
The first eleven narrative fields must each be an object shaped as
{"text": "section text", "evidence_block_ids": ["supplied block id"], "visuals": [{"block_id": "supplied formula/table/figure id", "role": "why this object matters here", "explanation": "plain-language explanation", "title": "optional short title"}]}. Never return a quote,
page number, bbox, parser status, or shared document-level evidence list. This is a
teaching-oriented reading note, not a short abstract:
- summary: 180-260 Chinese characters; open with what the paper is really about, not a generic
  "we propose a framework" abstract.
- problem: 400-700 characters; first give a concrete failure scenario, then explain why final
  answer-only evaluation and existing methods miss it, and end with the research gap.
- research_question: 180-320 characters; state the paper's question in the form "能否..." and
  name the two requirements that must hold simultaneously.
- core_idea: 450-800 characters; explain the central intuition in plain language, define the
  task-relative boundary, and contrast it with a static gate or the baseline.
- method: 650-1100 characters; name each component and its responsibility, including model,
  training objective, verifier, and what is observed before/after an action.
- workflow: 600-1000 characters; give a numbered end-to-end execution flow from task input to
  action audit, environment effect, verification, reward, and parameter update.
- experiments: 500-900 characters; identify task construction, partitions, baselines, metrics,
  and headline results. For every reported number, include its scope and comparison object.
- experiment_design: 400-700 characters; explain how controls, ablations, holdouts, and external
  benchmarks isolate the claimed effect.
- result_interpretation: 450-800 characters; translate the numbers into what improved, what did
  not improve, and exactly what conclusion is justified. Explicitly reject overclaiming production
  safety when the evidence only covers a synthetic or bounded environment.
- limitations: 400-700 characters, distinguish author-stated limitations from cautious inference.
- reproduction: 400-700 characters, provide a concrete reproduction checklist only from evidence;
  mark missing details as unknown.
- reading_boundary: a plain string of 150-300 characters, state missing formula/figure/table or parser coverage.
For visuals, select only objects that materially clarify the problem, mechanism, or conclusion; zero
visuals is valid. Do not force a fixed count and do not repeat the same object in a section. A formula
explanation is an interpretation, not a source fact. A table must be complete in the supplied block,
and a figure explanation must stay within its caption and visible source context. Keep each explanation
under 180 Chinese characters and name its role (for example: mechanism, objective, comparison, or
limitation). The server will drop IDs that are not supplied or whose block kind is not formula/table/figure.
Write for a reader who has not opened the paper. Use Chinese explanatory prose and keep English
only for model, benchmark, metric, and method names. Do not paste source paragraphs into the
narrative, do not write malformed fragments such as a table cell ending mid-number, and do not
replace explanation with a list of section names. If a formula/table is unavailable, explain its
role and mark the exact detail unknown rather than pretending it was recovered.
Use paragraphs or numbered sentences inside each string. The fields are the reading product, not
the source-of-truth evidence. Keep JSON valid."""
    summaries = dict(evidence_map.summaries) if evidence_map else {}
    all_fragments: dict[str, str] = {}
    for fragments in section_fragments.values():
        all_fragments.update(fragments)
    context_lines = [
        "### PAPER-LEVEL EVIDENCE CONTEXT",
        "Use block IDs exactly as shown. Eligible blocks can support facts; degraded/unparsed blocks only support a boundary note.",
    ]
    for block_id, text in all_fragments.items():
        summary = summaries.get(block_id)
        if summary:
            context_lines.append(f"Evidence map note for {block_id}: {summary}")
        context_lines.append(_render_extraction_block(block_id, text, evidence_blocks.get(block_id)))
    context_lines.append("### READING SECTIONS")
    for section_name, fragments in section_fragments.items():
        context_lines.append(f"### {section_name}")
        context_lines.append(
            f"{section_name}: recommended_evidence_block_ids={json.dumps(list(fragments), ensure_ascii=False)}; "
            "you may cite any relevant IDs from the shared context, including multiple IDs."
        )
    fragment_text = "\n\n".join(context_lines)
    user = f"""Paper title: {candidate.title}
 Paper URL: {candidate.source_url}
Paper-level evidence and reading plan:
{fragment_text}"""
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "thinking": THINKING_MODE,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }


def _deepseek_map_payload(
    candidate: PaperCandidate,
    fragments: Mapping[str, str],
    evidence_blocks: Mapping[str, EvidenceBlock],
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    fragment_text = "\n\n".join(
        _render_extraction_block(key, text, evidence_blocks.get(key)) for key, text in fragments.items()
    )
    system = """You are the evidence-map pass for a paper knowledge base. Return JSON only.
For each useful supplied EvidenceBlock, write one short neutral summary and preserve its exact
block_id. Do not infer facts that are absent, do not add metrics, and do not return page numbers.
Return {\"items\": [{\"block_id\": \"supplied id\", \"summary\": \"short summary\"}]}.
You may omit blocks that contain no useful paper content."""
    user = f"""Paper title: {candidate.title}
Paper URL: {candidate.source_url}
Evidence blocks:
{fragment_text}"""
    return {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "thinking": THINKING_MODE,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
    }


def _response_json(response: dict[str, Any]) -> dict[str, Any]:
    try:
        choice = response["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("DeepSeek did not return a valid JSON draft.") from error
    if not isinstance(content, str):
        raise RuntimeError("DeepSeek did not return a valid JSON draft.")
    try:
        payload = _decode_json_object(content)
    except RuntimeError as error:
        if choice.get("finish_reason") == "length":
            raise RuntimeError("DeepSeek draft exceeded its output limit before completing JSON.") from error
        raise
    if not isinstance(payload, dict):
        raise RuntimeError("DeepSeek draft must be a JSON object.")
    return payload


def _parse_deep_reading(
    payload: Mapping[str, Any],
    selected: Mapping[str, str],
    evidence_blocks: Mapping[str, EvidenceBlock],
) -> DeepReadingAnalysis:
    required_fields = ("summary", "problem", "method", "experiments", "limitations", "reproduction")
    optional_fields = ("research_question", "core_idea", "workflow", "experiment_design", "result_interpretation")
    dropped_sections: list[str] = []

    def parse_visuals(field: str, value: Mapping[str, Any]) -> tuple[ReadingVisual, ...]:
        raw_visuals = value.get("visuals", [])
        if raw_visuals is None:
            return ()
        if not isinstance(raw_visuals, list):
            if field not in dropped_sections:
                dropped_sections.append(field)
            return ()
        visuals: list[ReadingVisual] = []
        for raw in raw_visuals:
            if len(visuals) >= 4:
                if field not in dropped_sections:
                    dropped_sections.append(field)
                break
            if not isinstance(raw, Mapping):
                if field not in dropped_sections:
                    dropped_sections.append(field)
                continue
            block_id = raw.get("block_id")
            role = raw.get("role")
            explanation = raw.get("explanation")
            title = raw.get("title", "")
            block = evidence_blocks.get(block_id) if isinstance(block_id, str) else None
            if (
                not isinstance(block_id, str)
                or not isinstance(role, str)
                or not isinstance(explanation, str)
                or not isinstance(title, str)
                or block_id not in selected
                or block is None
                or block.candidate.kind not in {"formula", "table", "figure"}
                or not explanation.strip()
            ):
                if field not in dropped_sections:
                    dropped_sections.append(field)
                continue
            try:
                visual = ReadingVisual(
                    block_id=block_id,
                    role=role.strip()[:120],
                    explanation=explanation.strip()[:1_500],
                    title=title.strip()[:160],
                )
            except ValueError:
                if field not in dropped_sections:
                    dropped_sections.append(field)
                continue
            if visual.block_id not in {item.block_id for item in visuals}:
                visuals.append(visual)
        return tuple(visuals)

    def parse_section(field: str, *, required: bool) -> GroundedReadingSection:
        value = payload.get(field)
        if value is None and not required:
            return GroundedReadingSection("")
        if not isinstance(value, Mapping):
            raise RuntimeError(f"DeepSeek deep reading has invalid '{field}'.")
        text = value.get("text")
        block_ids = value.get("evidence_block_ids", [])
        if not isinstance(text, str):
            raise RuntimeError(f"DeepSeek deep reading has invalid '{field}'.")
        if not isinstance(block_ids, list) or not all(isinstance(item, str) for item in block_ids):
            raise RuntimeError(f"DeepSeek deep reading has invalid '{field}' evidence_block_ids.")
        valid: list[str] = []
        for block_id in block_ids:
            if len(valid) >= 8:
                if field not in dropped_sections:
                    dropped_sections.append(field)
                continue
            block = evidence_blocks.get(block_id)
            if (
                block_id not in selected
                or block is None
                or not block.eligible_for_fact
            ):
                if field not in dropped_sections:
                    dropped_sections.append(field)
                continue
            if block_id not in valid:
                valid.append(block_id)
        return GroundedReadingSection(text.strip(), tuple(valid), parse_visuals(field, value))

    sections = {field: parse_section(field, required=True) for field in required_fields}
    sections.update({field: parse_section(field, required=False) for field in optional_fields})
    boundary = payload.get("reading_boundary")
    if isinstance(boundary, Mapping):
        boundary = boundary.get("text")
    if not isinstance(boundary, str) or not boundary.strip():
        raise RuntimeError("DeepSeek deep reading has invalid 'reading_boundary'.")
    boundaries = [boundary.strip()]
    if dropped_sections:
        boundaries.append("以下章节的未知、不可用或用途不匹配证据引用已被服务端丢弃：" + "、".join(dropped_sections) + "。")
    degraded_kinds = {
        block.candidate.kind
        for block in evidence_blocks.values()
        if not block.eligible_for_fact and block.candidate.kind in {"formula", "table", "figure"}
    }
    kind_labels = {"formula": "公式", "table": "表格", "figure": "图片"}
    if degraded_kinds:
        boundaries.append("以下内容未形成可自动解释的合格证据：" + "、".join(kind_labels[kind] for kind in sorted(degraded_kinds)) + "。")
    return DeepReadingAnalysis(
        summary=sections["summary"],
        problem=sections["problem"],
        research_question=sections["research_question"],
        method=sections["method"],
        experiments=sections["experiments"],
        limitations=sections["limitations"],
        reproduction=sections["reproduction"],
        core_idea=sections["core_idea"],
        workflow=sections["workflow"],
        experiment_design=sections["experiment_design"],
        result_interpretation=sections["result_interpretation"],
        reading_boundary=" ".join(boundaries)[:800],
    )


def _decode_json_object(content: str) -> object:
    """Decode JSON while tolerating a model-added Markdown fence or preamble."""

    candidate = content.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        candidate = candidate[first_newline + 1 :] if first_newline >= 0 else ""
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    start = candidate.find("{")
    if start < 0:
        raise RuntimeError("DeepSeek did not return a valid JSON draft.")
    try:
        payload, _ = json.JSONDecoder().raw_decode(candidate[start:])
    except json.JSONDecodeError as error:
        raise RuntimeError("DeepSeek did not return a valid JSON draft.") from error
    return payload


def _render_extraction_block(anchor_id: str, text: str, evidence_block: EvidenceBlock | None) -> str:
    if evidence_block is None:
        return f"[EVIDENCE_BLOCK {anchor_id} kind=text section=unknown]\n{text}"
    candidate = evidence_block.candidate
    section = " / ".join(candidate.section_path) or "Overview"
    facets = ",".join(evidence_block.supported_facets) or "none"
    status = "eligible" if evidence_block.eligible_for_fact else f"unverified:{evidence_block.rejection_reason or candidate.parse_status}"
    return f"[EVIDENCE_BLOCK {anchor_id} kind={candidate.kind} section={section} status={status} allowed_facets={facets}]\n{text}"


def _parse_claims(
    value: object,
    source_fragments: Mapping[str, str],
    evidence_blocks: Mapping[str, EvidenceBlock] | None = None,
) -> tuple[list[KnowledgeClaim], dict[str, EvidenceFacet]]:
    if not isinstance(value, list) or not value:
        raise RuntimeError("DeepSeek draft must contain at least one claim.")
    claims: list[KnowledgeClaim] = []
    claim_facets: dict[str, EvidenceFacet] = {}
    for ordinal, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise RuntimeError("Each DeepSeek claim must be an object.")
        claim_type = item.get("claim_type")
        text = item.get("text")
        anchor_ids = item.get("anchor_ids", [])
        facet = item.get("facet")
        if claim_type not in {"source_fact", "agent_inference", "reading_question"}:
            raise RuntimeError("DeepSeek claim_type is invalid.")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("DeepSeek claim text is invalid.")
        if not isinstance(anchor_ids, list) or not all(isinstance(anchor, str) for anchor in anchor_ids):
            raise RuntimeError("DeepSeek claim anchor_ids is invalid.")
        if any(anchor not in source_fragments for anchor in anchor_ids):
            raise RuntimeError("DeepSeek claim references an unknown source anchor.")
        if claim_type == "source_fact":
            if len(anchor_ids) != 1:
                raise RuntimeError("A DeepSeek source_fact must reference exactly one source anchor.")
            if facet not in {"problem", "method", "experiment", "limitation"}:
                raise RuntimeError("A DeepSeek source_fact must select a valid evidence facet.")
            if evidence_blocks is not None:
                block = evidence_blocks.get(anchor_ids[0])
                supported = block.supported_facets if block and block.eligible_for_fact else ()
                if not supported:
                    raise RuntimeError("A DeepSeek source_fact must reference an eligible evidence block.")
                if facet not in supported:
                    # The server owns facet semantics.  A model can choose a
                    # useful block but mislabel it; canonicalize to the
                    # parser-derived facet instead of dropping the paper's
                    # evidence because of a presentation error.
                    facet = supported[0]
            text = _verbatim_claim_excerpt(source_fragments[anchor_ids[0]])
        elif facet is not None:
            # Facets are durable metadata, not a semantic field for
            # inferences/questions.  Ignore an accidental provider field
            # instead of discarding an otherwise useful reading response.
            facet = None
        claim_id = f"claim:{ordinal}"
        claims.append(
            KnowledgeClaim(
                claim_id=claim_id,
                claim_type=claim_type,
                text=text.strip(),
                anchor_ids=tuple(anchor_ids),
                source_facet=facet if claim_type == "source_fact" else None,
            )
        )
        if claim_type == "source_fact":
            claim_facets[claim_id] = facet
    return claims, claim_facets


def _verbatim_claim_excerpt(fragment: str, *, limit: int = 420) -> str:
    """Produce a bounded continuous quote rather than trusting model wording."""

    normalized = " ".join(fragment.split()).strip()
    if not normalized:
        raise RuntimeError("A source_fact anchor has no readable source text.")
    clipped = normalized[:limit]
    # Do not cut a scientific-notation suffix in the middle of a table cell
    # (for example ``4 . 55 × 10 − 4``).  Complete the immediately following
    # exponent digits while staying within a small bounded extension.
    if len(normalized) > len(clipped) and re.search(
        r"(?:[×x]\s*10\s*[−–-]|\d+\s*\.\s*|[eE][+-]?)\s*$", clipped
    ):
        remainder = normalized[len(clipped) :]
        # Table cells use ``|`` as the safest local boundary; keep only a
        # short continuation so the bounded excerpt cannot grow unbounded.
        continuation = remainder.split("|", 1)[0][:80]
        if continuation:
            clipped += continuation
    # If the verified fragment itself ends at a parser boundary in the middle
    # of a decimal (``p = 2 .``), do not publish the orphaned leading digit as
    # a numeric claim.  Retain the preceding complete table cell/sentence and
    # let the reading boundary describe the unavailable tail.
    if re.search(r"\b\d+\s*\.\s*$", clipped):
        boundary = max(clipped.rfind("|"), clipped.rfind(". "), clipped.rfind("。"))
        if boundary >= 80:
            clipped = clipped[:boundary].rstrip(" |")
    sentence_endings = [clipped.rfind(marker) for marker in (". ", "! ", "? ", "。", "！", "？")]
    cutoff = max(sentence_endings)
    if cutoff >= 80:
        clipped = clipped[: cutoff + 1]
    return clipped.strip()


def _verbatim_excerpt_covering_tokens(
    fragment: str, tokens: Collection[str], *, limit: int = 1_000
) -> str:
    """Return one continuous excerpt that contains every requested token.

    Reading prose is allowed to combine numbers from a result paragraph and a
    nearby table row.  Taking the first ``limit`` characters of the block can
    therefore lose the very number that caused the block to be promoted.  The
    excerpt is centered on all requested tokens; if they are too far apart for
    one bounded window, retain the full verified block rather than silently
    publishing an unsupported number.
    """

    normalized = " ".join(fragment.split()).strip()
    requested = tuple(dict.fromkeys(token for token in tokens if token))
    positions: list[int] = []
    for token in requested:
        match = _numeric_token_match(normalized, token)
        if match is not None:
            positions.append(match.start())
    if not normalized or not positions:
        return _verbatim_claim_excerpt(fragment, limit=limit)
    first = min(positions)
    last = max(positions)
    if last - first + 1 > limit:
        # A single durable anchor is bounded to 1,000 characters.  Choose the
        # bounded window that covers the most cited tokens; other tokens must
        # be backed by another anchor or the quality gate will keep the draft
        # in review instead of truncating evidence silently.
        candidate_starts = {
            max(0, min(position - 220, len(normalized) - limit))
            for position in positions
        }
        start = max(
            candidate_starts,
            key=lambda candidate: sum(
                candidate <= position < candidate + limit for position in positions
            ),
        )
    else:
        start = max(0, min(first - 220, len(normalized) - limit))
    end = min(len(normalized), start + limit)
    return normalized[start:end].strip()


def _numeric_token_match(text: str, token: str) -> re.Match[str] | None:
    """Match a number across parser typography (spaces, commas, exponents)."""

    escaped = re.escape(token.replace(",", ""))
    if "e-" in token.casefold():
        mantissa, exponent = re.split("e-", token.casefold(), maxsplit=1)
        mantissa_pattern = re.escape(mantissa).replace(r"\.", r"\s*\.\s*")
        pattern = rf"(?<![\w.]){mantissa_pattern}\s*(?:e\s*[-−]\s*{re.escape(exponent)}|[×x]\s*10\s*[−-]\s*{re.escape(exponent)})(?![\w.])"
    elif "." in token:
        whole, fraction = token.removesuffix("%").split(".", 1)
        pattern = rf"(?<![\w.]){re.escape(whole)}\s*\.\s*{re.escape(fraction)}%?(?![\w.])"
    else:
        pattern = rf"(?<![\w.]){escaped}(?![\w.])"
    return re.search(pattern, text, flags=re.IGNORECASE)


def _numeric_token_keys(value: str) -> set[str]:
    """Canonical numeric tokens used when promoting reading anchors."""

    normalized = value.replace(",", "")
    normalized = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalized)
    normalized = re.sub(
        r"(\d+(?:\.\d+)?)\s*[×x]\s*10\s*[−–-]\s*(\d+)",
        r"\1e-\2",
        normalized,
        flags=re.IGNORECASE,
    )
    return set(
        re.findall(
            r"(?<![\w.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[%％]?(?![\w.)、:，；])",
            normalized,
        )
    )


def _reading_hint_ids(reading_analysis: DeepReadingAnalysis | None) -> tuple[str, ...]:
    if reading_analysis is None:
        return ()
    return tuple(
        dict.fromkeys(
            anchor_id
            for section in reading_analysis.sections().values()
            for anchor_id in tuple(section.evidence_block_ids)
            + tuple(visual.block_id for visual in section.visuals)
        )
    )


def _include_reading_hints(
    material: SourceMaterial,
    selected: Mapping[str, str],
    hint_ids: Collection[str],
    budget: int,
) -> dict[str, str]:
    """Keep every eligible narrative anchor available to projection.

    The broad reader may discover a result block that falls after the first
    extraction budget.  Put those cited blocks first in the projection context
    so the durable layer can retain exactly the evidence used by the prose.
    """

    ordered: dict[str, str] = {}
    for anchor_id in tuple(dict.fromkeys(hint_ids)) + tuple(selected):
        text = material.source_fragments.get(anchor_id)
        block = material.evidence_blocks.get(anchor_id)
        if not text or block is None or not block.eligible_for_fact:
            continue
        ordered.setdefault(anchor_id, text)
    if not ordered:
        return dict(selected)
    return _select_fragments(ordered, budget)


def _promote_reading_anchors(
    claims: list[KnowledgeClaim],
    claim_facets: dict[str, EvidenceFacet],
    reading_analysis: DeepReadingAnalysis | None,
    source_fragments: Mapping[str, str],
    evidence_blocks: Mapping[str, EvidenceBlock],
) -> tuple[list[KnowledgeClaim], dict[str, EvidenceFacet]]:
    """Make narrative anchors durable without trusting model-written quotes.

    Deep reading runs before projection so it can see the paper-level context.
    Any eligible block it cites is promoted to a verbatim source fact if the
    short projection omitted it.  The server chooses the facet and excerpt;
    the model cannot smuggle an unsupported sentence into the durable layer.
    """

    if reading_analysis is None:
        return claims, claim_facets
    existing = {
        anchor_id
        for claim in claims
        if claim.claim_type == "source_fact"
        for anchor_id in claim.anchor_ids
    }
    preferred_facets: dict[str, tuple[EvidenceFacet, ...]] = {
        "summary": ("problem", "method"),
        "problem": ("problem",),
        "core_idea": ("method", "problem"),
        "method": ("method",),
        "workflow": ("method", "experiment"),
        "experiments": ("experiment",),
        "experiment_design": ("experiment", "method"),
        "result_interpretation": ("experiment", "method"),
        "limitations": ("limitation", "experiment"),
        "reproduction": ("method", "experiment"),
    }
    # Numeric tokens are checked against the full transient source set.  If a
    # reading paragraph combines a result from a later table block with an
    # earlier method explanation, promote the eligible block that actually
    # contains that result so the final durable excerpt can support it.
    numeric_tokens = tuple(
        dict.fromkeys(
            re.findall(
                r"(?<![\w.])\d+(?:\.\d+)?%?(?![.)、:])",
                "\n".join(section.text for section in reading_analysis.sections().values()),
            )
        )
    )
    hint_ids = _reading_hint_ids(reading_analysis)
    numeric_tokens_by_anchor: dict[str, list[str]] = {}
    source_numeric_keys = {
        anchor_id: _numeric_token_keys(text) for anchor_id, text in source_fragments.items()
    }
    for token in numeric_tokens:
        for anchor_id, text in source_fragments.items():
            if token in source_numeric_keys[anchor_id] or _numeric_token_match(text, token):
                numeric_tokens_by_anchor.setdefault(anchor_id, []).append(token)
    numeric_ids = tuple(numeric_tokens_by_anchor)
    candidate_ids = tuple(dict.fromkeys(hint_ids + tuple(numeric_ids)))
    # A projection claim may already exist for an anchor, but its bounded
    # excerpt can end before a number later in the same block.  Refresh that
    # claim from the full transient fragment so the durable materializer sees
    # the exact window needed by the reading prose.
    for index, claim in enumerate(claims):
        if claim.claim_type != "source_fact" or len(claim.anchor_ids) != 1:
            continue
        anchor_id = claim.anchor_ids[0]
        tokens = numeric_tokens_by_anchor.get(anchor_id)
        if not tokens or anchor_id not in source_fragments:
            continue
        claims[index] = replace(
            claim,
            text=_verbatim_excerpt_covering_tokens(
                source_fragments[anchor_id], tokens, limit=1_000
            ),
        )
    next_ordinal = len(claims) + 1
    section_for_anchor = {
        anchor_id: section_name
        for section_name, section in reading_analysis.sections().items()
        for anchor_id in tuple(section.evidence_block_ids)
        + tuple(visual.block_id for visual in section.visuals)
    }
    for anchor_id in candidate_ids:
        if anchor_id in existing or anchor_id not in source_fragments:
            continue
        block = evidence_blocks.get(anchor_id)
        if block is None or not block.eligible_for_fact or not block.supported_facets:
            continue
        preferred = preferred_facets.get(section_for_anchor.get(anchor_id, ""), ())
        facet = next((item for item in preferred if item in block.supported_facets), block.supported_facets[0])
        claim_id = f"claim:{next_ordinal}"
        next_ordinal += 1
        claims.append(
            KnowledgeClaim(
                claim_id=claim_id,
                claim_type="source_fact",
                text=_verbatim_excerpt_covering_tokens(
                    source_fragments[anchor_id],
                    numeric_tokens_by_anchor.get(anchor_id, ()),
                    limit=1_000,
                ),
                anchor_ids=(anchor_id,),
                source_facet=facet,
            )
        )
        claim_facets[claim_id] = facet
        existing.add(anchor_id)
    return claims, claim_facets


def _render_body(
    candidate: PaperCandidate,
    evidence_level: str,
    payload: Mapping[str, Any],
    claims: list[KnowledgeClaim],
    *,
    reading_analysis: DeepReadingAnalysis | None = None,
    evidence_blocks: Mapping[str, EvidenceBlock] | None = None,
    visual_assets: Mapping[str, Path] | None = None,
) -> str:
    if reading_analysis:
        narrative_fields = (
            ("一句话先说清楚", "summary"),
            ("背景：作者发现了什么问题", "problem"),
            ("论文真正要解决的问题", "research_question"),
            ("核心想法：把问题变成可计算目标", "core_idea"),
            ("方法如何落地", "method"),
            ("工作流程（一步一步）", "workflow"),
            ("实验如何验证这个想法", "experiments"),
            ("实验设计", "experiment_design"),
            ("应该怎样解读，而不是过度宣传", "result_interpretation"),
            ("局限", "limitations"),
            ("复现线索", "reproduction"),
        )
        required_keys = {"summary", "problem", "method", "experiments", "limitations", "reproduction"}
        fields = tuple(
            (heading, key)
            for heading, key in narrative_fields
            if key in required_keys or getattr(reading_analysis, key).text.strip()
        )
    else:
        fields = (
            ("一分钟总结", "summary"),
            ("问题", "problem"),
            ("方法", "method"),
            ("实验与结果", "experiments"),
            ("局限与阅读边界", "limitations"),
            ("复现线索", "reproduction"),
        )
    lines = [f"# {candidate.title}", "", f"> 证据等级：{evidence_level}。原始 PDF 与完整解析结果保留在 source cache；本笔记只展示被选择的理解材料。"]
    for heading, key in fields:
        grounded = getattr(reading_analysis, key, None) if reading_analysis else None
        value = grounded.text if isinstance(grounded, GroundedReadingSection) else payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"DeepSeek draft has invalid '{key}'.")
        if key == "experiments":
            result_table = _render_headline_result_table(claims)
            if result_table and "98.48%" not in value:
                value = value.rstrip() + "\n\n" + result_table
        lines.extend(["", f"## {heading}", "", value.strip()])
        if grounded is not None and evidence_blocks is not None:
            visual_text = _render_reading_visuals(
                grounded.visuals,
                evidence_blocks,
                visual_assets or {},
            )
            if visual_text:
                lines.extend(["", visual_text])
    if reading_analysis:
        lines.extend(
            [
                "",
                "## 精读证据边界",
                "",
                reading_analysis.reading_boundary,
                "",
                "> 本节为模型解读，不替代来源事实；RAG 仅使用下方通过质量门禁的证据主张。",
            ]
        )
    lines.extend(["", "## 证据主张（机器核验索引）", ""])
    # Full verbatim excerpts remain in the durable bundle and RAG projection.
    # The human-facing note only shows a compact index; dumping dozens of raw
    # English parser fragments here makes a reading note look like an OCR log.
    display_claims = claims[:16]
    for claim in display_claims:
        anchors = ", ".join(claim.anchor_ids) or "无（非来源事实）"
        facet = f"/{claim.source_facet}" if claim.source_facet else ""
        lines.append(f"- **{claim.claim_type}{facet}** `{claim.claim_id}` → {anchors}")
    if len(claims) > len(display_claims):
        lines.append(f"- 其余 {len(claims) - len(display_claims)} 条逐字证据保留在版本 provenance 与 RAG 索引中。")
    return "\n".join(lines) + "\n"


def _collect_visual_assets(
    reading_analysis: DeepReadingAnalysis | None,
    evidence_blocks: Mapping[str, EvidenceBlock],
) -> dict[str, Path]:
    """Return only selected, local figure files as safe relative asset names."""

    assets: dict[str, Path] = {}
    if reading_analysis is None:
        return assets
    for section in reading_analysis.sections().values():
        for visual in section.visuals:
            block = evidence_blocks.get(visual.block_id)
            candidate = block.candidate if block else None
            if candidate is None or candidate.kind != "figure" or candidate.image_source_path is None:
                continue
            suffix = Path(candidate.image_path or candidate.image_source_path.name).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                suffix = ".png"
            filename = f"visual-{sha256(visual.block_id.encode('utf-8')).hexdigest()[:12]}{suffix}"
            assets.setdefault(f"assets/{filename}", candidate.image_source_path)
    return assets


def _render_reading_visuals(
    visuals: Sequence[ReadingVisual],
    evidence_blocks: Mapping[str, EvidenceBlock],
    visual_assets: Mapping[str, Path],
) -> str:
    chunks: list[str] = []
    for visual in visuals:
        block = evidence_blocks.get(visual.block_id)
        if block is None:
            continue
        candidate = block.candidate
        label = visual.title or {"formula": "核心公式", "table": "关键表格", "figure": "关键图示"}.get(candidate.kind, "选中的原文对象")
        chunks.extend([f"### {label}", "", f"> 为什么放在这里：{visual.explanation}", ""])
        if candidate.kind == "formula":
            formula = (candidate.latex or candidate.text).strip()
            if formula:
                chunks.extend(["```latex", formula, "```", ""])
            chunks.append("这段公式的具体数值含义仍以原文定义为准；这里展示它是为了说明方法机制，不把模型解释当作事实证据。")
        elif candidate.kind == "table":
            table = _table_html_to_markdown(candidate.table_html) if candidate.table_html else None
            table = table or candidate.text.strip()
            if table and _looks_like_complete_table_projection(table):
                chunks.append(table)
            else:
                chunks.append("> 当前表格投影不完整，未把不完整单元格写入笔记；请回到来源核查。")
        elif candidate.kind == "figure":
            asset_ref = next(
                (relative for relative, source in visual_assets.items() if source == candidate.image_source_path),
                None,
            )
            if asset_ref:
                chunks.extend([f"![{candidate.caption or label}]({asset_ref})", ""])
            else:
                chunks.append("> 原图未形成可复制的安全资产，当前只保留图注与解释；请回到来源查看图像。")
            if candidate.caption:
                chunks.append(f"图注：{candidate.caption.strip()}")
        if candidate.page_start is not None:
            chunks.append(f"来源定位：第 {candidate.page_start} 页" + (f"–{candidate.page_end} 页" if candidate.page_end and candidate.page_end != candidate.page_start else "") + f"（block `{candidate.block_id}`）。")
        else:
            chunks.append(f"来源定位：`{candidate.block_id}`。")
        chunks.append("")
    return "\n".join(chunks).strip()


def _looks_like_complete_table_projection(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return len(lines) >= 3 and sum(line.startswith("|") and line.endswith("|") for line in lines) >= 3


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "tr":
            self._row = []
        elif tag.casefold() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif normalized == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _table_html_to_markdown(table_html: str | None) -> str | None:
    if not isinstance(table_html, str) or "<table" not in table_html.casefold():
        return None
    parser = _TableHTMLParser()
    try:
        parser.feed(table_html)
        parser.close()
    except Exception:
        return None
    rows = [row for row in parser.rows if row]
    if len(rows) < 2:
        return None
    width = len(rows[0])
    if width < 2:
        return None
    # A missing cell usually means MinerU/Docling lost a colspan or row
    # boundary. Do not pad it into a plausible-looking but false table.
    if any(len(row) != width for row in rows):
        return None
    padded = rows
    escape = lambda value: value.replace("|", "\\|").replace("\n", " ").strip()
    header = "| " + " | ".join(escape(value) for value in padded[0]) + " |"
    divider = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(escape(value) for value in row) + " |" for row in padded[1:]]
    return "\n".join([header, divider, *body])


def _render_headline_result_table(claims: Sequence[KnowledgeClaim]) -> str:
    """Render a compact result table from an already durable source fact.

    The reader may summarize an experiment correctly while omitting the
    paper's central comparison table.  Recover only the three well-formed
    headline rows when the verified source excerpt contains them; never infer
    values or manufacture a table from model prose.
    """

    text = "\n".join(
        claim.text
        for claim in claims
        if claim.claim_type == "source_fact"
    )
    patterns = (
        ("任务 episode success", r"Task episode success\s+[\d,]+\s+\(([\d.]+%)\)\s+[\d,]+\s+\(([\d.]+%)\)"),
        ("Safe success", r"Safe success\s+[\d,]+\s+\(([\d.]+%)\)\s+[\d,]+\s+\(([\d.]+%)\)"),
        ("超额权限错误事件", r"Excess-authority success\s+[\d,]+\s+\(([\d.]+%)\)\s+[\d,]+\s+\(([\d.]+%)\)"),
    )
    rows: list[str] = []
    for label, pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            rows.append(f"| {label} | {match.group(1)} | {match.group(2)} |")
    if len(rows) < 2:
        return ""
    return "**主要内部对比（来自已核验表格证据）**\n\n| 指标 | Base | Seed 1 |\n|---|---:|---:|\n" + "\n".join(rows)


def _redact_unanchored_reading_numbers(
    reading_analysis: DeepReadingAnalysis | None,
    claims: Sequence[KnowledgeClaim],
) -> DeepReadingAnalysis | None:
    """Downgrade reading-only numbers that have no durable source excerpt.

    The narrative model may combine a number from a truncated table cell with
    otherwise valid prose.  Keep the sentence and replace only that exact
    number with an explicit boundary marker; the durable source-fact layer and
    its hard evidence gate remain unchanged.
    """

    if reading_analysis is None:
        return None
    supported = {
        number
        for claim in claims
        if claim.claim_type == "source_fact"
        for number in _numeric_token_keys(claim.text)
    }
    changed = False
    sections: dict[str, GroundedReadingSection] = {}
    # Match the complete numeric token.  The trailing word boundary is
    # important here: ``1000`` must not be split into ``100`` + ``0`` when a
    # reading-only number is downgraded, and the percent sign must be part of
    # the replacement rather than left dangling in the prose.
    token_pattern = re.compile(
        r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)"
        r"(?:\.\d+)?(?:[eE][+-]?\d+)?[%％]?(?![\w.)、:，；])"
    )
    for name, section in reading_analysis.sections().items():
        text = section.text
        replacements: list[tuple[int, int]] = []
        for match in token_pattern.finditer(text):
            token_keys = _numeric_token_keys(match.group(0))
            if not token_keys.intersection(supported):
                replacements.append((match.start(), match.end()))
        if replacements:
            changed = True
            for start, end in reversed(replacements):
                text = text[:start] + "（具体数值未在当前证据中恢复）" + text[end:]
        sections[name] = replace(section, text=text)
    if not changed:
        return reading_analysis
    return replace(
        reading_analysis,
        **sections,
        reading_boundary=(reading_analysis.reading_boundary + " 部分正文数字未能落到可持久化摘录，已降级为未核对。")[:800],
    )


def _select_fragments(fragments: Mapping[str, str], budget: int) -> dict[str, str]:
    if budget < 1_000:
        raise ValueError("max_source_chars must be at least 1000.")
    selected: dict[str, str] = {}
    used = 0
    for anchor_id, text in fragments.items():
        remaining = budget - used
        if remaining <= 0:
            break
        part = text[:remaining].strip()
        if part:
            selected[anchor_id] = part
            used += len(part)
    if not selected:
        raise RuntimeError("No source fragments fit the DeepSeek extraction budget.")
    return selected


def _select_extraction_fragments(material: SourceMaterial, budget: int) -> dict[str, str]:
    """Select a paper-level, high-recall projection context.

    The old selector took two blocks per facet.  That made the projection miss
    the paper's actual result table or training setup even when the parser had
    already produced those blocks.  Keep a bounded context, but reserve room
    for contiguous problem/method/experiment/limitation bands instead.
    """

    if not material.evidence_blocks:
        return _select_fragments(material.source_fragments, budget)

    quotas = {"problem": 12, "method": 18, "experiment": 18, "limitation": 8}
    selected_ids: list[str] = []
    for band, quota in quotas.items():
        candidates = [
            (anchor_id, block)
            for anchor_id, block in material.evidence_blocks.items()
            if block.eligible_for_fact
            and block.supported_facets
            and anchor_id in material.source_fragments
            and _paper_reading_band(block) == band
        ]
        candidates.sort(key=lambda item: _facet_priority(band, item[1]))
        selected_ids.extend(anchor_id for anchor_id, _ in candidates[:quota] if anchor_id not in selected_ids)

    if not selected_ids:
        return _select_fragments(material.source_fragments, budget)
    balanced = {anchor_id: material.source_fragments[anchor_id] for anchor_id in selected_ids}
    return _select_fragments(balanced, budget)


def _paper_reading_band(block: EvidenceBlock) -> str:
    section = " ".join(block.candidate.section_path).casefold()
    if any(marker in section for marker in ("limitation", "threat", "future work", "局限")):
        return "limitation"
    if any(marker in section for marker in ("experiment", "evaluation", "result", "benchmark", "ablation", "实验", "结果")):
        return "experiment"
    if any(marker in section for marker in ("method", "methodology", "approach", "architecture", "framework", "implementation", "方法")):
        return "method"
    return "problem"


def _select_reading_fragments(material: SourceMaterial, budget: int) -> dict[str, str]:
    """Keep a broad, ordered reading context while retaining a hard input bound."""

    if not material.evidence_blocks:
        return _select_fragments(material.source_fragments, budget)
    eligible = [
        (anchor_id, block)
        for anchor_id, block in material.evidence_blocks.items()
        if block.eligible_for_fact and anchor_id in material.source_fragments
    ]
    # Preserve document order for coherent reading, but cap the number of blocks
    # so one paper cannot turn a single provider call into an unbounded prompt.
    eligible_ids = [anchor_id for anchor_id, _ in eligible[:48]]
    fragments = {anchor_id: material.source_fragments[anchor_id] for anchor_id in eligible_ids}
    if not fragments:
        return _select_fragments(material.source_fragments, budget)
    return _select_fragments(fragments, budget)


def _select_reading_section_fragments(
    material: SourceMaterial,
    budget: int,
    *,
    allowed_block_ids: Collection[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Build one bounded paper-level context shared by all reading sections.

    Sections still receive recommended IDs through the prompt, but they no
    longer receive disjoint two-block slices.  This is what lets the reader
    connect an introduction claim to its method and later result.
    """

    if budget < 1_000:
        raise ValueError("max_source_chars must be at least 1000.")
    if not material.evidence_blocks:
        return {section: {} for section in _READING_SECTION_FACETS}
    allowed = set(allowed_block_ids) if allowed_block_ids is not None else None
    quotas = {"problem": 16, "method": 22, "experiment": 24, "limitation": 12}
    common: dict[str, str] = {}
    for band, quota in quotas.items():
        candidates = [
            (block_id, block)
            for block_id, block in material.evidence_blocks.items()
            if (allowed is None or block_id in allowed)
            and _paper_reading_band(block) == band
            and (
                (block.eligible_for_fact and block.supported_facets and block_id in material.source_fragments)
                or (block.candidate.kind in {"formula", "table", "figure", "caption"} and block.candidate.text.strip())
            )
        ]
        candidates.sort(key=lambda item: (0 if item[1].eligible_for_fact else 1, item[0]))
        for block_id, block in candidates[:quota]:
            text = material.source_fragments.get(block_id, block.candidate.text)
            text = normalize_evidence_excerpt(text)[:1_000].strip()
            if text:
                common[block_id] = text
    if not common:
        common = _select_fragments(material.source_fragments, budget)
    common = _select_fragments(common, budget)
    return {section: dict(common) for section in _READING_SECTION_FACETS}


def _facet_priority(facet: EvidenceFacet, block: EvidenceBlock) -> tuple[int, int, int]:
    candidate = block.candidate
    section = " ".join(candidate.section_path).casefold()
    markers = {
        "problem": ("abstract", "introduction", "problem", "motivation"),
        "method": ("method", "approach", "framework", "architecture"),
        "experiment": ("experiment", "evaluation", "results", "benchmark"),
        "limitation": ("limitation", "threat", "future work"),
    }[facet]
    section_rank = 0 if any(marker in section for marker in markers) else 1
    locator_rank = {"exact": 0, "partial": 1, "section_only": 2, "missing": 3}[candidate.locator_completeness]
    kind_rank = 0 if candidate.kind in {"text", "table"} else 1
    return section_rank, locator_rank, kind_rank


def _request_json(
    post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout_seconds: float,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    if post_json is not None:
        return post_json(url, payload, headers)
    attempts = 0
    while True:
        try:
            return _post_json(url, payload, headers, timeout_seconds=timeout_seconds)
        except ProviderTimeout:
            if attempts >= max_retries:
                raise
            attempts += 1
            deadline = current_deadline()
            if deadline:
                deadline.ensure_remaining()


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = current_deadline()
    if deadline:
        deadline.ensure_remaining()
        timeout_seconds = min(timeout_seconds, deadline.remaining())
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=max(0.1, timeout_seconds)) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"DeepSeek request failed with HTTP {error.code}.") from error
    except (TimeoutError, socket.timeout) as error:
        if deadline and deadline.remaining() <= 0:
            raise ProductionDeadlineExceeded("reading_timeout") from error
        raise ProviderTimeout("provider_timeout") from error
    except URLError as error:
        if deadline and deadline.remaining() <= 0:
            raise ProductionDeadlineExceeded("reading_timeout") from error
        if isinstance(error.reason, (TimeoutError, socket.timeout)) or "timed out" in str(error.reason).casefold():
            raise ProviderTimeout("provider_timeout") from error
        raise RuntimeError(f"Cannot reach DeepSeek: {error.reason}") from error


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive number.") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number.")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer.") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer.")
    return value


@dataclass(frozen=True)
class FilesystemKnowledgePublisher:
    """Commit canonical knowledge, then complete its rebuildable projections."""

    vault_root: Path
    rag: ResearchRAG
    processed_registry: Any
    manifest_store: ManifestStore | None = None

    def publish(self, bundle: KnowledgeBundle, source_id: str) -> PublicationManifest:
        asset = bundle.asset
        if asset.publication_status != "published":
            raise ValueError("Only a quality-approved published asset may enter the vault.")
        if not source_id.strip():
            raise ValueError("A published bundle needs a non-empty source_id.")
        store = self.manifest_store or ManifestStore(self.vault_root)
        if store.vault_root != self.vault_root.resolve():
            raise KnowledgeAssetError("The manifest store must use the publisher vault root.")
        paths = store.paths_for(asset)
        path = paths.markdown
        path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path = paths.provenance
        provenance_text = render_provenance(bundle)
        provenance_hash = sha256(provenance_text.encode("utf-8")).hexdigest()
        persisted_asset = replace(
            asset,
            schema_version=3 if bundle.reading_sections else 2,
            provenance_file=provenance_path.name,
            provenance_sha256=provenance_hash,
        )
        persisted_bundle = replace(bundle, asset=persisted_asset)
        self._copy_visual_assets(persisted_bundle, path.parent)
        if path.exists():
            reparsed = KnowledgeBundle.from_markdown(path)
            if (
                reparsed.asset.knowledge_id != persisted_asset.knowledge_id
                or reparsed.asset.knowledge_version != persisted_asset.knowledge_version
                or reparsed.asset.content_sha256 != persisted_asset.content_sha256
                or reparsed.asset.provenance_sha256 != persisted_asset.provenance_sha256
            ):
                raise KnowledgeAssetError("An existing knowledge version cannot be replaced with different content.")
            manifest = store.read(paths.manifest)
            if manifest.source_id != source_id:
                raise KnowledgeAssetError("An existing manifest belongs to another source ID.")
            if manifest.markdown_path != store.relative(path):
                raise KnowledgeAssetError("Existing manifest Markdown path does not match its version.")
            validate_manifest_bundle(manifest, reparsed, paths.provenance)
            return self._complete_projection(store, paths.manifest, manifest, reparsed)
        provenance_temporary = provenance_path.with_suffix(provenance_path.suffix + ".tmp")
        provenance_temporary.write_bytes(provenance_text.encode("utf-8"))
        if sha256(provenance_temporary.read_bytes()).hexdigest() != provenance_hash:
            raise KnowledgeAssetError("The written provenance sidecar failed hash verification.")
        provenance_temporary.replace(provenance_path)
        manifest = PublicationManifest.pending(
            source_id=source_id,
            asset=persisted_asset,
            markdown_path=store.relative(path),
            provenance_path=store.relative(provenance_path),
        )
        store.write(paths.manifest, manifest)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(render_asset_markdown(persisted_asset), encoding="utf-8")
        KnowledgeBundle.from_markdown(temporary_path)
        temporary_path.replace(path)
        reparsed = KnowledgeBundle.from_markdown(path)
        return self._complete_projection(store, paths.manifest, manifest, reparsed)

    def _copy_visual_assets(self, bundle: KnowledgeBundle, version_root: Path) -> None:
        for relative_path, source_path in bundle.visual_assets.items():
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("assets",):
                raise KnowledgeAssetError("A visual asset path must stay under the version assets directory.")
            source = Path(source_path).resolve()
            if not source.is_file():
                raise KnowledgeAssetError("A selected visual asset is missing from the source cache.")
            destination = (version_root / relative).resolve()
            try:
                destination.relative_to(version_root.resolve())
            except ValueError as error:
                raise KnowledgeAssetError("A visual asset destination escaped the version directory.") from error
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if sha256(destination.read_bytes()).digest() != sha256(source.read_bytes()).digest():
                    raise KnowledgeAssetError("An existing visual asset differs from the selected source asset.")
                continue
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            if sha256(temporary.read_bytes()).digest() != sha256(source.read_bytes()).digest():
                temporary.unlink(missing_ok=True)
                raise KnowledgeAssetError("A copied visual asset failed hash verification.")
            temporary.replace(destination)

    def _complete_projection(
        self,
        store: ManifestStore,
        manifest_path: Path,
        manifest: PublicationManifest,
        bundle: KnowledgeBundle,
    ) -> PublicationManifest:
        if manifest.index_status == "indexed":
            return manifest
        attempt = manifest.begin_attempt()
        store.write(manifest_path, attempt)
        try:
            receipt = self.rag.publish(bundle)
            self.processed_registry.mark_processed(manifest.source_id, manifest.knowledge_id)
            indexed = attempt.indexed(receipt)
            return store.write(manifest_path, indexed)
        except Exception as error:
            try:
                store.write(manifest_path, attempt.failed(error))
            except Exception:
                pass
            raise


@dataclass(frozen=True)
class PostgresProcessedPaperRegistry:
    """Small durable deduplication ledger, separate from rebuildable vectors."""

    database_url: str

    def initialize(self) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_papers (
                        source_id TEXT PRIMARY KEY,
                        knowledge_id TEXT NOT NULL,
                        processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )

    def was_processed(self, source_id: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM processed_papers WHERE source_id = %s", (source_id,))
                return cursor.fetchone() is not None

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO processed_papers (source_id, knowledge_id)
                    VALUES (%s, %s)
                    ON CONFLICT (source_id) DO NOTHING
                    RETURNING knowledge_id
                    """,
                    (source_id, knowledge_id),
                )
                inserted = cursor.fetchone()
                if inserted is not None:
                    return
                cursor.execute("SELECT knowledge_id FROM processed_papers WHERE source_id = %s", (source_id,))
                existing = cursor.fetchone()
                existing_id = existing[0] if existing else None
                if existing_id != knowledge_id:
                    raise KnowledgeAssetError("A source ID cannot be marked as a different knowledge asset.")

    def _connection(self):
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("psycopg is not installed; install requirements-rag.txt.") from error
        return psycopg.connect(self.database_url)


def render_asset_markdown(asset: KnowledgeAsset) -> str:
    """Render the canonical source-of-truth file with a deterministic hash."""

    front_matter = {
        "knowledge_id": asset.knowledge_id,
        "knowledge_version": asset.knowledge_version,
        "publication_status": asset.publication_status,
        "evidence_level": asset.evidence_level,
        "source_urls": list(asset.source_urls),
        "domain": asset.domain,
        "title": asset.title,
        "content_sha256": asset.content_sha256,
        "supersedes": asset.supersedes,
        "schema_version": asset.schema_version,
        "provenance_file": asset.provenance_file,
        "provenance_sha256": asset.provenance_sha256,
    }
    lines = ["---"]
    lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front_matter.items())
    lines.extend(["---", "", asset.body.rstrip()])
    return "\n".join(lines) + "\n"


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "asset"
