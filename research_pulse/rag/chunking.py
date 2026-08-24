"""Deterministic Markdown chunking for the first lexical retrieval baseline."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from research_pulse.knowledge.models import ClaimType, DurableEvidenceAnchor, KnowledgeAssetError, KnowledgeBundle


MAX_CHUNK_CHARS = 1_200


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    knowledge_id: str
    knowledge_version: str
    ordinal: int
    section: str
    anchor_id: str
    text: str
    claim_id: str
    claim_type: ClaimType
    source_anchors: tuple[DurableEvidenceAnchor, ...]


def chunk_bundle(bundle: KnowledgeBundle) -> list[KnowledgeChunk]:
    """Create one typed answer-evidence chunk per machine-readable claim."""

    if not bundle.answer_eligible:
        raise KnowledgeAssetError("Legacy bundles are not answer-eligible.")
    asset = bundle.asset
    anchor_map = {anchor.anchor_id: anchor for anchor in bundle.anchors}
    chunks: list[KnowledgeChunk] = []
    answer_claims = [claim for claim in bundle.claims if claim.claim_type == "source_fact"]
    for ordinal, claim in enumerate(answer_claims, start=1):
        section = "Source Fact" if claim.claim_type == "source_fact" else "Agent Inference"
        anchor_id = _anchor_id(asset, claim.claim_id, ordinal)
        source_anchors = tuple(anchor_map[anchor_id] for anchor_id in claim.anchor_ids)
        if claim.claim_type == "source_fact":
            if not source_anchors:
                raise KnowledgeAssetError("A source_fact cannot be projected without durable evidence.")
            if any(anchor.parse_status not in {None, "available"} for anchor in source_anchors):
                raise KnowledgeAssetError("A degraded or unparsed evidence block cannot enter answer retrieval.")
        chunks.append(
            KnowledgeChunk(
                chunk_id=_chunk_id(asset, section, ordinal, claim.text),
                knowledge_id=asset.knowledge_id,
                knowledge_version=asset.knowledge_version,
                ordinal=ordinal,
                section=section,
                anchor_id=anchor_id,
                text=claim.text,
                claim_id=claim.claim_id,
                claim_type=claim.claim_type,
                source_anchors=source_anchors,
            )
        )
    return chunks


def _sections(markdown: str) -> list[tuple[str, str]]:
    current_section = "Overview"
    buffer: list[str] = []
    sections: list[tuple[str, str]] = []
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            _append_section(sections, current_section, buffer)
            current_section = match.group(1)
            buffer = []
        else:
            buffer.append(line)
    _append_section(sections, current_section, buffer)
    return sections


def _append_section(sections: list[tuple[str, str]], section: str, lines: list[str]) -> None:
    text = "\n".join(lines).strip()
    if text:
        sections.append((section, text))


def _split_text(text: str, max_chars: int) -> list[str]:
    paragraphs = [" ".join(part.split()) for part in text.split("\n\n") if part.strip()]
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_split(paragraph, max_chars))
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _hard_split(text: str, max_chars: int) -> list[str]:
    return [text[start : start + max_chars] for start in range(0, len(text), max_chars)]


def _anchor_id(asset, claim_id: str, ordinal: int) -> str:
    claim_slug = re.sub(r"[^a-z0-9]+", "-", claim_id.casefold()).strip("-") or "claim"
    return f"anchor:{asset.knowledge_id}:{asset.knowledge_version}:{claim_slug}:{ordinal}"


def _chunk_id(asset: KnowledgeAsset, section: str, ordinal: int, text: str) -> str:
    digest = sha256(
        f"{asset.knowledge_id}\n{asset.knowledge_version}\n{section}\n{ordinal}\n{text}".encode("utf-8")
    ).hexdigest()[:24]
    return f"chunk:{digest}"
