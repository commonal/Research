"""Domain contracts shared by production, retrieval, evaluation, and the API.

These are deliberately framework-free.  PostgreSQL rows, LangGraph state, and
frontend DTOs are adapters around these contracts rather than the source of
their meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
import json
import re


_CONTENT_ADDRESS = re.compile(r"^(?:urn:)?sha256:[0-9a-fA-F]{64}$")


def is_source_identity(value: str) -> bool:
    """True for a locatable source identity: an HTTP(S) URL or a sha256
    content address (``sha256:<hex>`` / ``urn:sha256:<hex>``).

    Uploaded papers often carry no HTTP source; their stable content-address
    identity keeps the note resolvable by the knowledge reader.
    """
    return bool(value) and (
        value.startswith(("https://", "http://")) or _CONTENT_ADDRESS.match(value) is not None
    )


ClaimType = Literal["source_fact", "agent_inference", "reading_question"]
ClaimFacet = Literal["problem", "method", "experiment", "limitation"]
EvidenceBlockKind = Literal["text", "formula", "table", "figure", "caption"]
EvidenceParseStatus = Literal["available", "degraded", "unparsed"]
LocatorCompleteness = Literal["exact", "partial", "section_only", "missing"]
ReadingSectionName = Literal[
    "summary",
    "problem",
    "research_question",
    "core_idea",
    "method",
    "workflow",
    "experiments",
    "experiment_design",
    "result_interpretation",
    "limitations",
    "reproduction",
]
EvidenceLevel = Literal["abstract_only", "full_text_text", "full_text_multimodal", "source_linked_unverified"]
PublicationStatus = Literal["draft", "needs_review", "published", "superseded", "rejected"]
ProvenanceStatus = Literal["complete", "legacy_missing_provenance"]


class KnowledgeAssetError(ValueError):
    """Raised when a Markdown file cannot be treated as a publishable asset."""


@dataclass(frozen=True)
class EvidenceAnchor:
    """A stable pointer from one claim to a location in a source paper."""

    anchor_id: str
    source_url: str
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    figure_or_table: str | None = None
    block_kind: EvidenceBlockKind | None = None
    parse_status: EvidenceParseStatus | None = None
    locator_completeness: LocatorCompleteness | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class KnowledgeClaim:
    """A typed statement that may be retrieved or rendered to a user."""

    claim_id: str
    claim_type: ClaimType
    text: str
    anchor_ids: tuple[str, ...]
    source_facet: ClaimFacet | None = None


def normalize_evidence_excerpt(value: str) -> str:
    """Normalize whitespace without changing the words in a source excerpt."""

    return " ".join(value.split())


@dataclass(frozen=True)
class DurableEvidenceAnchor:
    """Bounded, durable evidence needed to verify one published claim."""

    anchor_id: str
    source_url: str
    evidence_excerpt: str
    excerpt_sha256: str
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    figure_or_table: str | None = None
    block_kind: EvidenceBlockKind | None = None
    parse_status: EvidenceParseStatus | None = None
    locator_completeness: LocatorCompleteness | None = None
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        excerpt = normalize_evidence_excerpt(self.evidence_excerpt)
        if not self.anchor_id.strip():
            raise KnowledgeAssetError("A durable anchor needs a non-empty anchor_id.")
        if not self.source_url.startswith(("https://", "http://")):
            raise KnowledgeAssetError("A durable anchor needs an HTTP(S) source_url.")
        if not any((self.section, self.page_start is not None, self.figure_or_table)):
            raise KnowledgeAssetError("A durable anchor needs a section, page, or figure/table locator.")
        if not excerpt or len(excerpt) > 1_000:
            raise KnowledgeAssetError("A durable evidence excerpt must contain 1-1000 characters.")
        if self.page_start is not None and self.page_start < 1:
            raise KnowledgeAssetError("page_start must be positive.")
        if self.page_end is not None and (self.page_start is None or self.page_end < self.page_start):
            raise KnowledgeAssetError("page_end requires page_start and cannot precede it.")
        if self.block_kind is not None and self.block_kind not in {"text", "formula", "table", "figure", "caption"}:
            raise KnowledgeAssetError("block_kind is invalid.")
        if self.parse_status is not None and self.parse_status not in {"available", "degraded", "unparsed"}:
            raise KnowledgeAssetError("parse_status is invalid.")
        if self.locator_completeness is not None and self.locator_completeness not in {"exact", "partial", "section_only", "missing"}:
            raise KnowledgeAssetError("locator_completeness is invalid.")
        if self.bbox is not None and len(self.bbox) != 4:
            raise KnowledgeAssetError("bbox must contain four coordinates.")
        expected_hash = sha256(excerpt.encode("utf-8")).hexdigest()
        if self.excerpt_sha256 != expected_hash:
            raise KnowledgeAssetError("excerpt_sha256 does not match the normalized evidence excerpt.")
        object.__setattr__(self, "evidence_excerpt", excerpt)


@dataclass(frozen=True)
class KnowledgeAsset:
    """The canonical, versioned Markdown asset; never a vector-store record."""

    knowledge_id: str
    knowledge_version: str
    publication_status: PublicationStatus
    evidence_level: EvidenceLevel
    source_urls: tuple[str, ...]
    domain: str
    title: str
    body: str
    content_sha256: str
    supersedes: str | None = None
    schema_version: int = 1
    provenance_file: str | None = None
    provenance_sha256: str | None = None
    rag_eligible: bool = True

    @classmethod
    def from_markdown(cls, path: Path) -> "KnowledgeAsset":
        text = path.read_text(encoding="utf-8")
        metadata, body = split_front_matter(text)
        required = (
            "knowledge_id",
            "knowledge_version",
            "publication_status",
            "evidence_level",
            "source_urls",
            "domain",
            "title",
        )
        missing = [field for field in required if not metadata.get(field)]
        if missing:
            raise KnowledgeAssetError("Missing required front matter: " + ", ".join(missing))
        source_urls = metadata["source_urls"]
        if not isinstance(source_urls, list) or not source_urls or not all(
            isinstance(url, str) and is_source_identity(url) for url in source_urls
        ):
            raise KnowledgeAssetError("source_urls must be a non-empty list of HTTP(S) URLs or sha256 content-addresses.")
        status = metadata["publication_status"]
        level = metadata["evidence_level"]
        if status not in {"draft", "needs_review", "published", "superseded", "rejected"}:
            raise KnowledgeAssetError("publication_status is invalid.")
        if level not in {"abstract_only", "full_text_text", "full_text_multimodal", "source_linked_unverified"}:
            raise KnowledgeAssetError("evidence_level is invalid.")
        rag_eligible = metadata.get("rag_eligible", level != "source_linked_unverified")
        if not isinstance(rag_eligible, bool):
            raise KnowledgeAssetError("rag_eligible must be a boolean.")
        if level == "source_linked_unverified" and rag_eligible:
            raise KnowledgeAssetError("Unverified source-linked knowledge cannot be RAG eligible.")
        expected_hash = sha256(body.encode("utf-8")).hexdigest()
        supplied_hash = metadata.get("content_sha256")
        if supplied_hash is not None and supplied_hash != expected_hash:
            raise KnowledgeAssetError("content_sha256 does not match the Markdown body.")
        schema_version = metadata.get("schema_version", 1)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version not in {1, 2, 3}:
            raise KnowledgeAssetError("schema_version must be 1, 2, or 3.")
        provenance_file = _optional_string(metadata.get("provenance_file"), field="provenance_file")
        provenance_hash = _optional_string(metadata.get("provenance_sha256"), field="provenance_sha256")
        if schema_version >= 2 and (not provenance_file or not provenance_hash):
            raise KnowledgeAssetError("Schema v2+ requires provenance_file and provenance_sha256.")
        return cls(
            knowledge_id=_required_string(metadata, "knowledge_id"),
            knowledge_version=_required_string(metadata, "knowledge_version"),
            publication_status=status,
            evidence_level=level,
            source_urls=tuple(source_urls),
            domain=_required_string(metadata, "domain"),
            title=_required_string(metadata, "title"),
            body=body,
            content_sha256=expected_hash,
            supersedes=_optional_string(metadata.get("supersedes"), field="supersedes"),
            schema_version=schema_version,
            provenance_file=provenance_file,
            provenance_sha256=provenance_hash,
            rag_eligible=rag_eligible,
        )


@dataclass(frozen=True)
class ReadingVisualEvidence:
    """Durable metadata for one selected formula, table, or figure."""

    block_id: str
    kind: EvidenceBlockKind
    role: str
    explanation: str
    asset_path: str | None = None

    def __post_init__(self) -> None:
        if not self.block_id.strip() or self.kind not in {"formula", "table", "figure"}:
            raise KnowledgeAssetError("A reading visual has an invalid source block.")
        if not self.role.strip() or not self.explanation.strip():
            raise KnowledgeAssetError("A reading visual needs a role and explanation.")
        if self.asset_path is not None:
            path = Path(self.asset_path)
            if path.is_absolute() or ".." in path.parts or not self.asset_path.startswith("assets/"):
                raise KnowledgeAssetError("A reading visual asset path must stay under assets/.")


@dataclass(frozen=True)
class ReadingSectionEvidence:
    """Durable section-to-anchor mapping; section prose remains in Markdown."""

    section_name: ReadingSectionName
    anchor_ids: tuple[str, ...] = ()
    visuals: tuple[ReadingVisualEvidence, ...] = ()

    def __post_init__(self) -> None:
        allowed = {
            "summary", "problem", "research_question", "core_idea", "method", "workflow", "experiments",
            "experiment_design", "result_interpretation", "limitations", "reproduction",
        }
        if self.section_name not in allowed:
            raise KnowledgeAssetError("reading section name is invalid.")
        if len(self.anchor_ids) > 8 or len(self.anchor_ids) != len(set(self.anchor_ids)):
            raise KnowledgeAssetError("A reading section needs at most eight unique durable anchor IDs.")
        if any(not anchor_id.strip() for anchor_id in self.anchor_ids):
            raise KnowledgeAssetError("Reading section anchor IDs must be non-empty.")
        visual_ids = [visual.block_id for visual in self.visuals]
        if len(visual_ids) != len(set(visual_ids)) or len(visual_ids) > 4:
            raise KnowledgeAssetError("A reading section needs at most four unique visual objects.")


@dataclass(frozen=True)
class KnowledgeBundle:
    """One immutable knowledge version and its machine-readable provenance."""

    asset: KnowledgeAsset
    claims: tuple[KnowledgeClaim, ...]
    anchors: tuple[DurableEvidenceAnchor, ...]
    provenance_status: ProvenanceStatus = "complete"
    reading_sections: tuple[ReadingSectionEvidence, ...] = ()
    visual_assets: Mapping[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provenance_status == "legacy_missing_provenance":
            if self.claims or self.anchors or self.reading_sections or self.visual_assets:
                raise KnowledgeAssetError("Legacy bundles cannot claim machine-readable provenance.")
            return
        for asset_path, source_path in self.visual_assets.items():
            if not asset_path.startswith("assets/") or Path(asset_path).is_absolute() or ".." in Path(asset_path).parts:
                raise KnowledgeAssetError("Visual assets must use safe relative assets/ paths.")
            if not isinstance(source_path, Path):
                raise KnowledgeAssetError("Visual asset sources must be filesystem paths.")
        claim_ids = [claim.claim_id for claim in self.claims]
        anchor_ids = [anchor.anchor_id for anchor in self.anchors]
        if len(claim_ids) != len(set(claim_ids)):
            raise KnowledgeAssetError("Claim IDs must be unique within a knowledge version.")
        if len(anchor_ids) != len(set(anchor_ids)):
            raise KnowledgeAssetError("Anchor IDs must be unique within a knowledge version.")
        section_names = [section.section_name for section in self.reading_sections]
        if len(section_names) > 12 or len(section_names) != len(set(section_names)):
            raise KnowledgeAssetError("Reading section mappings must use unique bounded section names.")
        anchor_map = {anchor.anchor_id: anchor for anchor in self.anchors}
        for anchor in self.anchors:
            if anchor.source_url not in self.asset.source_urls:
                raise KnowledgeAssetError("Every durable anchor source must belong to the asset.")
        for claim in self.claims:
            if claim.claim_type == "source_fact" and not claim.anchor_ids:
                raise KnowledgeAssetError("Every source_fact needs a durable anchor.")
            if any(anchor_id not in anchor_map for anchor_id in claim.anchor_ids):
                raise KnowledgeAssetError("A claim references a missing durable anchor.")
        source_fact_anchor_ids = {
            anchor_id
            for claim in self.claims
            if claim.claim_type == "source_fact"
            for anchor_id in claim.anchor_ids
        }
        if any(
            anchor_id not in source_fact_anchor_ids
            for section in self.reading_sections
            for anchor_id in section.anchor_ids
        ):
            raise KnowledgeAssetError("A reading section may reference only an anchor used by an approved source_fact.")

    @property
    def answer_eligible(self) -> bool:
        return self.provenance_status == "complete" and self.asset.rag_eligible

    @classmethod
    def from_markdown(cls, path: Path) -> "KnowledgeBundle":
        asset = KnowledgeAsset.from_markdown(path)
        if asset.schema_version == 1:
            return cls(asset=asset, claims=(), anchors=(), provenance_status="legacy_missing_provenance")
        assert asset.provenance_file and asset.provenance_sha256
        if Path(asset.provenance_file).name != asset.provenance_file:
            raise KnowledgeAssetError("provenance_file must be a sibling filename.")
        sidecar_path = path.parent / asset.provenance_file
        try:
            raw = sidecar_path.read_bytes()
        except OSError as error:
            raise KnowledgeAssetError("The provenance sidecar is missing or unreadable.") from error
        if sha256(raw).hexdigest() != asset.provenance_sha256:
            raise KnowledgeAssetError("provenance_sha256 does not match the sidecar.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KnowledgeAssetError("The provenance sidecar is invalid JSON.") from error
        return bundle_from_provenance_payload(asset, payload)


def bundle_from_provenance_payload(asset: KnowledgeAsset, payload: object) -> KnowledgeBundle:
    if not isinstance(payload, dict):
        raise KnowledgeAssetError("The provenance payload must be an object.")
    provenance_schema = payload.get("schema_version")
    if provenance_schema not in {1, 2}:
        raise KnowledgeAssetError("Unsupported provenance schema_version.")
    if asset.schema_version == 3 and provenance_schema != 2:
        raise KnowledgeAssetError("Schema v3 Markdown requires section-aware provenance.")
    if payload.get("knowledge_id") != asset.knowledge_id or payload.get("knowledge_version") != asset.knowledge_version:
        raise KnowledgeAssetError("Markdown and provenance ID/version must match.")
    raw_claims = payload.get("claims")
    raw_anchors = payload.get("anchors")
    if not isinstance(raw_claims, list) or not isinstance(raw_anchors, list):
        raise KnowledgeAssetError("Provenance claims and anchors must be arrays.")
    claims = tuple(_claim_from_payload(value) for value in raw_claims)
    anchors = tuple(_durable_anchor_from_payload(value) for value in raw_anchors)
    raw_sections = payload.get("reading_sections", [])
    if provenance_schema == 2 and not isinstance(raw_sections, list):
        raise KnowledgeAssetError("Provenance reading_sections must be an array.")
    if provenance_schema == 1 and "reading_sections" in payload:
        raise KnowledgeAssetError("Legacy provenance cannot declare reading section mappings.")
    reading_sections = tuple(_reading_section_from_payload(value) for value in raw_sections)
    return KnowledgeBundle(asset=asset, claims=claims, anchors=anchors, reading_sections=reading_sections)


def provenance_payload(bundle: KnowledgeBundle) -> dict[str, Any]:
    if not bundle.answer_eligible:
        raise KnowledgeAssetError("Legacy bundles have no canonical provenance payload.")
    return {
        "schema_version": 2 if bundle.reading_sections else 1,
        "knowledge_id": bundle.asset.knowledge_id,
        "knowledge_version": bundle.asset.knowledge_version,
        "claims": [
            {
                "claim_id": claim.claim_id,
                "claim_type": claim.claim_type,
                "text": claim.text,
                "anchor_ids": list(claim.anchor_ids),
                "source_facet": claim.source_facet,
            }
            for claim in bundle.claims
        ],
        "anchors": [
            {
                "anchor_id": anchor.anchor_id,
                "source_url": anchor.source_url,
                "section": anchor.section,
                "page_start": anchor.page_start,
                "page_end": anchor.page_end,
                "figure_or_table": anchor.figure_or_table,
                "block_kind": anchor.block_kind,
                "parse_status": anchor.parse_status,
                "locator_completeness": anchor.locator_completeness,
                "bbox": list(anchor.bbox) if anchor.bbox is not None else None,
                "evidence_excerpt": anchor.evidence_excerpt,
                "excerpt_sha256": anchor.excerpt_sha256,
            }
            for anchor in bundle.anchors
        ],
        **(
            {
                "reading_sections": [
                    {
                        "section_name": section.section_name,
                        "anchor_ids": list(section.anchor_ids),
                        **(
                            {
                                "visuals": [
                                    {
                                        "block_id": visual.block_id,
                                        "kind": visual.kind,
                                        "role": visual.role,
                                        "explanation": visual.explanation,
                                        **({"asset_path": visual.asset_path} if visual.asset_path else {}),
                                    }
                                    for visual in section.visuals
                                ]
                            }
                            if section.visuals
                            else {}
                        ),
                    }
                    for section in bundle.reading_sections
                ]
            }
            if bundle.reading_sections
            else {}
        ),
    }


def render_provenance(bundle: KnowledgeBundle) -> str:
    return json.dumps(provenance_payload(bundle), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def split_front_matter(markdown: str) -> tuple[dict[str, object], str]:
    """Parse the restricted JSON-value YAML used by knowledge asset front matter.

    Keeping values JSON-compatible avoids introducing a loose YAML parser into
    the first persistence contract.  Rich content belongs in the Markdown body.
    """

    if not markdown.startswith("---\n"):
        raise KnowledgeAssetError("Markdown must start with front matter.")
    try:
        header, body = markdown[4:].split("\n---\n", maxsplit=1)
    except ValueError as error:
        raise KnowledgeAssetError("Markdown front matter is not closed.") from error
    metadata: dict[str, object] = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise KnowledgeAssetError("Front matter lines must be key: JSON-value pairs.")
        key, raw_value = line.split(":", maxsplit=1)
        key = key.strip()
        if not key or key in metadata:
            raise KnowledgeAssetError("Front matter keys must be unique and non-empty.")
        try:
            metadata[key] = json.loads(raw_value.strip())
        except json.JSONDecodeError as error:
            raise KnowledgeAssetError(f"Front matter value for '{key}' must be JSON.") from error
    return metadata, body.lstrip("\n")


def _required_string(metadata: dict[str, object], field: str) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeAssetError(f"{field} must be a non-empty string.")
    return value.strip()


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise KnowledgeAssetError(f"{field} must be a string or null.")
    return value.strip() or None


def _claim_from_payload(value: object) -> KnowledgeClaim:
    if not isinstance(value, dict):
        raise KnowledgeAssetError("Each provenance claim must be an object.")
    claim_type = value.get("claim_type")
    anchor_ids = value.get("anchor_ids")
    if claim_type not in {"source_fact", "agent_inference", "reading_question"}:
        raise KnowledgeAssetError("A provenance claim has an invalid claim_type.")
    if not isinstance(anchor_ids, list) or not all(isinstance(item, str) for item in anchor_ids):
        raise KnowledgeAssetError("A provenance claim needs a string anchor_ids array.")
    return KnowledgeClaim(
        claim_id=_mapping_string(value, "claim_id"),
        claim_type=claim_type,
        text=_mapping_string(value, "text"),
        anchor_ids=tuple(anchor_ids),
        source_facet=_mapping_optional_choice(value, "source_facet", {"problem", "method", "experiment", "limitation"}),
    )


def _durable_anchor_from_payload(value: object) -> DurableEvidenceAnchor:
    if not isinstance(value, dict):
        raise KnowledgeAssetError("Each provenance anchor must be an object.")
    return DurableEvidenceAnchor(
        anchor_id=_mapping_string(value, "anchor_id"),
        source_url=_mapping_string(value, "source_url"),
        evidence_excerpt=_mapping_string(value, "evidence_excerpt"),
        excerpt_sha256=_mapping_string(value, "excerpt_sha256"),
        section=_mapping_optional_string(value, "section"),
        page_start=_mapping_optional_int(value, "page_start"),
        page_end=_mapping_optional_int(value, "page_end"),
        figure_or_table=_mapping_optional_string(value, "figure_or_table"),
        block_kind=_mapping_optional_choice(value, "block_kind", {"text", "formula", "table", "figure", "caption"}),
        parse_status=_mapping_optional_choice(value, "parse_status", {"available", "degraded", "unparsed"}),
        locator_completeness=_mapping_optional_choice(value, "locator_completeness", {"exact", "partial", "section_only", "missing"}),
        bbox=_mapping_optional_bbox(value, "bbox"),
    )


def _reading_section_from_payload(value: object) -> ReadingSectionEvidence:
    if not isinstance(value, dict):
        raise KnowledgeAssetError("Each reading section mapping must be an object.")
    section_name = value.get("section_name")
    anchor_ids = value.get("anchor_ids")
    if not isinstance(section_name, str):
        raise KnowledgeAssetError("A reading section mapping needs a section_name.")
    if not isinstance(anchor_ids, list) or not all(isinstance(item, str) for item in anchor_ids):
        raise KnowledgeAssetError("A reading section mapping needs a string anchor_ids array.")
    raw_visuals = value.get("visuals", [])
    if not isinstance(raw_visuals, list):
        raise KnowledgeAssetError("A reading section visuals field must be an array.")
    visuals: list[ReadingVisualEvidence] = []
    for item in raw_visuals:
        if not isinstance(item, dict):
            raise KnowledgeAssetError("A reading visual must be an object.")
        kind = item.get("kind")
        if kind not in {"formula", "table", "figure"}:
            raise KnowledgeAssetError("A reading visual kind is invalid.")
        visuals.append(
            ReadingVisualEvidence(
                block_id=_mapping_string(item, "block_id"),
                kind=kind,
                role=_mapping_string(item, "role"),
                explanation=_mapping_string(item, "explanation"),
                asset_path=_mapping_optional_string(item, "asset_path"),
            )
        )
    return ReadingSectionEvidence(section_name, tuple(anchor_ids), tuple(visuals))  # type: ignore[arg-type]


def _mapping_string(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise KnowledgeAssetError(f"{field} must be a non-empty string.")
    return item.strip()


def _mapping_optional_string(value: Mapping[str, object], field: str) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str):
        raise KnowledgeAssetError(f"{field} must be a string or null.")
    return item.strip() or None


def _mapping_optional_int(value: Mapping[str, object], field: str) -> int | None:
    item = value.get(field)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int):
        raise KnowledgeAssetError(f"{field} must be an integer or null.")
    return item


def _mapping_optional_choice(value: Mapping[str, object], field: str, allowed: set[str]) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str) or item not in allowed:
        raise KnowledgeAssetError(f"{field} must be one of the supported values or null.")
    return item


def _mapping_optional_bbox(value: Mapping[str, object], field: str) -> tuple[float, float, float, float] | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, list) or len(item) != 4 or any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in item):
        raise KnowledgeAssetError(f"{field} must be a four-number array or null.")
    return tuple(float(number) for number in item)
