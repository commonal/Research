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
    ExtractedDraft,
    PaperCandidate,
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
