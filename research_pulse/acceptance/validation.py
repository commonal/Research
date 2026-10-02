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
from research_pulse.knowledge.reader import FilesystemKnowledgeReader, KnowledgeDetail


class AcceptanceValidationError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class KnowledgeIdentity:
    knowledge_id: str
    knowledge_version: str


@dataclass(frozen=True)
class NoteVerification:
    knowledge_id: str
    knowledge_version: str
    markdown: str
    source_urls: tuple[str, ...]
    content_sha256: str
    markdown_relative_path: str


@dataclass(frozen=True)
class ApiVerification:
    list_status: int
    detail_status: int


def snapshot_knowledge(vault_root: Path) -> frozenset[KnowledgeIdentity]:
    """Snapshot the current published-note identities from the canonical vault."""
    identities: set[KnowledgeIdentity] = set()
    reader = FilesystemKnowledgeReader(vault_root)
    for detail in reader.recent(limit=10_000):
        identities.add(KnowledgeIdentity(detail.knowledge_id, detail.knowledge_version))
    return frozenset(identities)


def verify_note_published(
    *,
    vault_root: Path,
    before: frozenset[KnowledgeIdentity],
    source_id: str,
) -> NoteVerification:
    """Assert exactly one new published note appeared for the selected source.

    The note is the canonical knowledge of record (schema-v1 markdown produced by
    the PaperReader route).  We require the note to be present in the canonical
    ``papers/`` bucket, to carry the expected knowledge ID, and to have a
    non-empty, re-readable body.
    """
    reader = FilesystemKnowledgeReader(vault_root)
    expected_knowledge_id = f"kp:arxiv:{source_id}"
    detail = reader.get_current(expected_knowledge_id)
    if detail is None:
        raise AcceptanceValidationError("No published note exists for the selected source.")
    if detail.knowledge_id != expected_knowledge_id:
        raise AcceptanceValidationError("The new note knowledge ID does not match the selected arXiv source ID.")
    if not detail.markdown.strip():
        raise AcceptanceValidationError("The published note has an empty body.")
    content_sha256 = sha256(detail.markdown.encode("utf-8")).hexdigest()
    return NoteVerification(
        knowledge_id=detail.knowledge_id,
        knowledge_version=detail.knowledge_version,
        markdown=detail.markdown,
        source_urls=detail.source_urls,
        content_sha256=content_sha256,
        markdown_relative_path=(
            f"papers/{detail.knowledge_id.removeprefix('kp:arxiv:')}/{detail.knowledge_version}.md"
        ),
    )


def build_acceptance_app(*, interactive_graph: Any, vault_root: Path):
    return create_app(
        interactive_graph=interactive_graph,
        knowledge_reader=FilesystemKnowledgeReader(vault_root),
    )


def verify_note_readable(client: TestClient, verification: NoteVerification) -> ApiVerification:
    """Assert the published note is readable through the knowledge reading API."""
    listing = client.get("/api/knowledge?limit=100")
    if listing.status_code != 200:
        raise AcceptanceValidationError("Knowledge timeline API is unavailable.")
    items = listing.json().get("items", [])
    if not any(
        item.get("knowledge_id") == verification.knowledge_id
        and item.get("knowledge_version") == verification.knowledge_version
        for item in items
    ):
        raise AcceptanceValidationError("Knowledge timeline does not contain the accepted note.")
    detail = client.get(f"/api/knowledge/{quote(verification.knowledge_id, safe='')}")
    if detail.status_code != 200:
        raise AcceptanceValidationError("Knowledge detail API cannot read the accepted paper.")
    payload = detail.json()
    if (
        payload.get("knowledge_version") != verification.knowledge_version
        or payload.get("markdown") != verification.markdown
        or tuple(payload.get("source_urls", ())) != verification.source_urls
    ):
        raise AcceptanceValidationError("Knowledge detail API returned the wrong body, version, or sources.")
    return ApiVerification(listing.status_code, detail.status_code)


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
