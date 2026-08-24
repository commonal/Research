"""Post-publication assertions for a single-paper acceptance run."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from fastapi.testclient import TestClient

from research_pulse.acceptance.orchestration import CostBoundary
from research_pulse.api.app import create_app
from research_pulse.knowledge.models import KnowledgeAssetError, KnowledgeBundle
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.production.publication import ManifestStore, PublicationManifest, validate_manifest_bundle
from research_pulse.rag.contracts import EvidenceHit, ResearchRAG, SearchRequest


class AcceptanceValidationError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class KnowledgeIdentity:
    knowledge_id: str
    knowledge_version: str


@dataclass(frozen=True)
class BundleVerification:
    bundle: KnowledgeBundle
    manifest: PublicationManifest
    markdown_path: Path
    markdown_relative_path: str
    source_fact_ids: tuple[str, ...]
    source_anchor_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalVerification:
    hits: tuple[EvidenceHit, ...]
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class ApiVerification:
    list_status: int
    detail_status: int


@dataclass(frozen=True)
class ChatVerification:
    answer_sha256: str
    answer_length: int
    citation_ids: tuple[str, ...]
    source_anchor_ids: tuple[str, ...]


def snapshot_knowledge(vault_root: Path) -> frozenset[KnowledgeIdentity]:
    identities: set[KnowledgeIdentity] = set()
    store = ManifestStore(vault_root)
    for path in store.iter_markdown_paths():
        try:
            bundle = KnowledgeBundle.from_markdown(path)
        except (KnowledgeAssetError, OSError, ValueError):
            continue
        identities.add(KnowledgeIdentity(bundle.asset.knowledge_id, bundle.asset.knowledge_version))
    return frozenset(identities)


def verify_new_bundle(
    *,
    vault_root: Path,
    before: frozenset[KnowledgeIdentity],
    source_id: str,
) -> BundleVerification:
    store = ManifestStore(vault_root)
    candidates: list[tuple[Path, KnowledgeBundle]] = []
    for path in store.iter_markdown_paths():
        try:
            bundle = KnowledgeBundle.from_markdown(path)
        except (KnowledgeAssetError, OSError, ValueError) as error:
            raise AcceptanceValidationError("A managed Markdown bundle is unreadable after production.") from error
        identity = KnowledgeIdentity(bundle.asset.knowledge_id, bundle.asset.knowledge_version)
        if identity not in before:
            candidates.append((path, bundle))
    if len(candidates) != 1:
        raise AcceptanceValidationError(f"Expected exactly one new knowledge version, found {len(candidates)}.")
    markdown_path, bundle = candidates[0]
    expected_knowledge_id = f"kp:arxiv:{source_id}"
    if bundle.asset.knowledge_id != expected_knowledge_id:
        raise AcceptanceValidationError("The new knowledge ID does not match the selected arXiv source ID.")
    if bundle.asset.publication_status != "published" or not bundle.answer_eligible:
        raise AcceptanceValidationError("The new bundle is not a complete published knowledge asset.")
    manifest_path = markdown_path.with_suffix(".manifest.json")
    try:
        manifest = store.read(manifest_path)
        provenance_path = markdown_path.with_suffix(".provenance.json")
        validate_manifest_bundle(manifest, bundle, provenance_path)
    except (KnowledgeAssetError, OSError, ValueError) as error:
        raise AcceptanceValidationError("The publication manifest does not validate against the bundle.") from error
    if manifest.source_id != source_id or manifest.index_status != "indexed":
        raise AcceptanceValidationError("The publication manifest is not indexed for the selected source ID.")
    source_fact_ids, source_anchor_ids = verify_source_facts(bundle)
    return BundleVerification(
        bundle=bundle,
        manifest=manifest,
        markdown_path=markdown_path,
        markdown_relative_path=store.relative(markdown_path),
        source_fact_ids=source_fact_ids,
        source_anchor_ids=source_anchor_ids,
    )


def verify_source_facts(bundle: KnowledgeBundle, *, minimum: int = 2) -> tuple[tuple[str, ...], tuple[str, ...]]:
    facts = tuple(claim for claim in bundle.claims if claim.claim_type == "source_fact")
    if len(facts) < minimum:
        raise AcceptanceValidationError(f"Expected at least {minimum} source facts, found {len(facts)}.")
    anchor_map = {anchor.anchor_id: anchor for anchor in bundle.anchors}
    used: list[str] = []
    for fact in facts:
        if not fact.anchor_ids:
            raise AcceptanceValidationError("A source fact has no durable anchor.")
        for anchor_id in fact.anchor_ids:
            anchor = anchor_map.get(anchor_id)
            if anchor is None:
                raise AcceptanceValidationError("A source fact references a missing durable anchor.")
            if anchor.source_url not in bundle.asset.source_urls:
                raise AcceptanceValidationError("A durable anchor points outside the bundle source URLs.")
            if not any((anchor.section, anchor.page_start is not None, anchor.figure_or_table)):
                raise AcceptanceValidationError("A durable anchor has no resolvable locator.")
            expected = sha256(anchor.evidence_excerpt.encode("utf-8")).hexdigest()
            if anchor.excerpt_sha256 != expected:
                raise AcceptanceValidationError("A durable anchor excerpt hash is invalid.")
            used.append(anchor_id)
    return tuple(claim.claim_id for claim in facts), tuple(dict.fromkeys(used))


def verify_postgres_retrieval(rag: ResearchRAG, verification: BundleVerification) -> RetrievalVerification:
    bundle = verification.bundle
    facts = [claim for claim in bundle.claims if claim.claim_type == "source_fact"]
    hits_by_id: dict[str, EvidenceHit] = {}
    manifest_chunk_ids = set(verification.manifest.chunk_ids)
    bundle_anchor_ids = {anchor.anchor_id for anchor in bundle.anchors}
    for fact in facts:
        hits = rag.search(
            SearchRequest(
                query=fact.text,
                domain=bundle.asset.domain,
                knowledge_ids=(bundle.asset.knowledge_id,),
                claim_types=("source_fact",),
                limit=8,
            )
        )
        for hit in hits:
            if (hit.knowledge_id, hit.knowledge_version) != (
                bundle.asset.knowledge_id,
                bundle.asset.knowledge_version,
            ):
                raise AcceptanceValidationError("FTS returned a different knowledge identity or version.")
            if hit.chunk_id not in manifest_chunk_ids:
                raise AcceptanceValidationError("FTS returned a chunk absent from the indexed manifest.")
            if hit.claim_type != "source_fact" or not hit.claim_id or not hit.source_anchors:
                raise AcceptanceValidationError("FTS returned an ungrounded or non-fact chunk.")
            if any(anchor.anchor_id not in bundle_anchor_ids for anchor in hit.source_anchors):
                raise AcceptanceValidationError("FTS returned an unresolved source anchor.")
            hits_by_id[hit.chunk_id] = hit
    if len(hits_by_id) < 2:
        raise AcceptanceValidationError("PostgreSQL FTS did not return two current source-addressable facts.")
    ordered = tuple(sorted(hits_by_id.values(), key=lambda item: item.chunk_id))
    return RetrievalVerification(ordered, tuple(hit.chunk_id for hit in ordered))


def build_acceptance_app(*, interactive_graph: Any, vault_root: Path):
    return create_app(
        interactive_graph=interactive_graph,
        knowledge_reader=FilesystemKnowledgeReader(vault_root),
    )


def verify_reading_api(client: TestClient, bundle: KnowledgeBundle) -> ApiVerification:
    listing = client.get("/api/knowledge?limit=100")
    if listing.status_code != 200:
        raise AcceptanceValidationError("Knowledge timeline API is unavailable.")
    items = listing.json().get("items", [])
    if not any(
        item.get("knowledge_id") == bundle.asset.knowledge_id
        and item.get("knowledge_version") == bundle.asset.knowledge_version
        for item in items
    ):
        raise AcceptanceValidationError("Knowledge timeline does not contain the accepted version.")
    detail = client.get(f"/api/knowledge/{quote(bundle.asset.knowledge_id, safe='')}")
    if detail.status_code != 200:
        raise AcceptanceValidationError("Knowledge detail API cannot read the accepted paper.")
    payload = detail.json()
    if (
        payload.get("knowledge_version") != bundle.asset.knowledge_version
        or payload.get("markdown") != bundle.asset.body
        or tuple(payload.get("source_urls", ())) != bundle.asset.source_urls
    ):
        raise AcceptanceValidationError("Knowledge detail API returned the wrong body, version, or sources.")
    return ApiVerification(listing.status_code, detail.status_code)


def verify_scoped_chat(
    client: TestClient,
    *,
    query: str,
    bundle: KnowledgeBundle,
    budget: CostBoundary,
) -> ChatVerification:
    budget.consume_question()
    response = client.post(
        "/api/chat",
        json={
            "query": query,
            "domain": bundle.asset.domain,
            "knowledge_ids": [bundle.asset.knowledge_id],
            "thread_id": "real-acceptance-scoped",
        },
    )
    if response.status_code != 200:
        raise AcceptanceValidationError("Scoped chat API is unavailable.")
    payload = response.json()
    if payload.get("status") != "completed":
        raise AcceptanceValidationError("Scoped chat reported insufficient evidence or requested supplementation.")
    answer = payload.get("answer")
    citations = payload.get("citations")
    if not isinstance(answer, str) or not answer.strip() or not isinstance(citations, list) or not citations:
        raise AcceptanceValidationError("Scoped chat returned no grounded answer or citations.")
    bundle_anchor_ids = {anchor.anchor_id for anchor in bundle.anchors}
    citation_ids: list[str] = []
    source_anchor_ids: list[str] = []
    for citation in citations:
        if (
            citation.get("knowledge_id") != bundle.asset.knowledge_id
            or citation.get("knowledge_version") != bundle.asset.knowledge_version
            or not citation.get("anchor_id")
            or not citation.get("claim_id")
        ):
            raise AcceptanceValidationError("Scoped chat returned an out-of-scope or incomplete citation.")
        raw_anchors = citation.get("source_anchors")
        if not isinstance(raw_anchors, list) or not raw_anchors:
            raise AcceptanceValidationError("Scoped chat citation has no durable source anchor.")
        for anchor in raw_anchors:
            anchor_id = anchor.get("anchor_id") if isinstance(anchor, dict) else None
            if anchor_id not in bundle_anchor_ids:
                raise AcceptanceValidationError("Scoped chat citation source anchor cannot be resolved.")
            source_anchor_ids.append(anchor_id)
        citation_ids.append(
            f"{citation['knowledge_id']}@{citation['knowledge_version']}|{citation['anchor_id']}|{citation['claim_id']}"
        )
    normalized = answer.strip()
    return ChatVerification(
        answer_sha256=sha256(normalized.encode("utf-8")).hexdigest(),
        answer_length=len(normalized),
        citation_ids=tuple(dict.fromkeys(citation_ids)),
        source_anchor_ids=tuple(dict.fromkeys(source_anchor_ids)),
    )


_RAW_SUFFIXES = (".pdf", ".doctags", ".docling.json", ".fulltext.json")


def snapshot_raw_material(roots: Iterable[Path]) -> frozenset[str]:
    found: set[str] = set()
    for index, root in enumerate(roots):
        resolved = root.resolve(strict=False)
        if not resolved.exists():
            continue
        for path in resolved.rglob("*"):
            if path.is_file() and _is_raw_material(path):
                found.add(f"root-{index}/{path.relative_to(resolved).as_posix()}")
    return frozenset(found)


def verify_no_new_raw_material(*, before: frozenset[str], roots: Iterable[Path]) -> tuple[str, ...]:
    new_files = tuple(sorted(snapshot_raw_material(roots) - before))
    if new_files:
        raise AcceptanceValidationError("Raw paper material persisted in managed roots: " + ", ".join(new_files))
    return new_files


def _is_raw_material(path: Path) -> bool:
    lower = path.name.casefold()
    return lower.endswith(_RAW_SUFFIXES) or lower in {"source.pdf", "parsed-fulltext.md"}
