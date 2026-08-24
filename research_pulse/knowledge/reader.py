"""Framework-free read model for the canonical file knowledge vault."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    KnowledgeAssetError,
    KnowledgeBundle,
    ProvenanceStatus,
    ReadingSectionEvidence,
)


@dataclass(frozen=True)
class KnowledgeSummary:
    knowledge_id: str
    knowledge_version: str
    title: str
    domain: str
    evidence_level: str
    source_url: str
    provenance_status: ProvenanceStatus


@dataclass(frozen=True)
class KnowledgeDetail:
    knowledge_id: str
    knowledge_version: str
    title: str
    domain: str
    evidence_level: str
    source_urls: tuple[str, ...]
    markdown: str
    provenance_status: ProvenanceStatus
    anchors: tuple[DurableEvidenceAnchor, ...]
    reading_mode: str = "legacy"
    evidence_model: str = "legacy"
    reading_sections: tuple[ReadingSectionEvidence, ...] = ()


class KnowledgeReader(Protocol):
    """Read-only boundary used by HTTP and other knowledge consumers."""

    def recent(self, *, limit: int) -> Sequence[KnowledgeSummary]: ...

    def get_current(self, knowledge_id: str) -> KnowledgeDetail | None: ...


class FilesystemKnowledgeReader:
    """Select current published bundles directly from the canonical vault."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root

    def recent(self, *, limit: int) -> Sequence[KnowledgeSummary]:
        current = self._current_bundles()
        ordered = sorted(current.values(), key=lambda item: item[0], reverse=True)
        return [self._summary(bundle) for _, bundle in ordered[:limit]]

    def get_current(self, knowledge_id: str) -> KnowledgeDetail | None:
        selected = self._current_bundles().get(knowledge_id)
        if selected is None:
            return None
        _, bundle = selected
        asset = bundle.asset
        return KnowledgeDetail(
            knowledge_id=asset.knowledge_id,
            knowledge_version=asset.knowledge_version,
            title=asset.title,
            domain=asset.domain,
            evidence_level=asset.evidence_level,
            source_urls=asset.source_urls,
            markdown=asset.body,
            provenance_status=bundle.provenance_status,
            anchors=bundle.anchors,
            reading_mode="deep_reading" if "## 精读证据边界" in asset.body else "legacy",
            evidence_model=(
                "section_anchors"
                if asset.schema_version >= 3
                else "legacy_v2"
                if asset.schema_version == 2
                else "legacy"
            ),
            reading_sections=bundle.reading_sections,
        )

    def read_asset(self, knowledge_id: str, relative_path: str) -> Path | None:
        """Resolve one selected version asset without exposing the vault root."""

        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("assets",):
            return None
        selected: tuple[datetime, Path] | None = None
        for markdown_path in self.vault_root.glob("papers/**/*.md"):
            try:
                bundle = KnowledgeBundle.from_markdown(markdown_path)
                version = _parse_version(bundle.asset.knowledge_version)
            except (KnowledgeAssetError, OSError, ValueError):
                continue
            if bundle.asset.knowledge_id != knowledge_id or bundle.asset.publication_status != "published":
                continue
            if selected is None or version > selected[0]:
                selected = (version, markdown_path)
        if selected is None:
            return None
        root = selected[1].parent.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _current_bundles(self) -> dict[str, tuple[datetime, KnowledgeBundle]]:
        current: dict[str, tuple[datetime, KnowledgeBundle]] = {}
        for path in self.vault_root.glob("papers/**/*.md"):
            try:
                bundle = KnowledgeBundle.from_markdown(path)
                version = _parse_version(bundle.asset.knowledge_version)
            except (KnowledgeAssetError, OSError, ValueError):
                continue
            if bundle.asset.publication_status != "published":
                continue
            previous = current.get(bundle.asset.knowledge_id)
            if previous is None or version > previous[0]:
                current[bundle.asset.knowledge_id] = (version, bundle)
        return current

    @staticmethod
    def _summary(bundle: KnowledgeBundle) -> KnowledgeSummary:
        asset = bundle.asset
        return KnowledgeSummary(
            knowledge_id=asset.knowledge_id,
            knowledge_version=asset.knowledge_version,
            title=asset.title,
            domain=asset.domain,
            evidence_level=asset.evidence_level,
            source_url=asset.source_urls[0],
            provenance_status=bundle.provenance_status,
        )


def _parse_version(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Knowledge versions must be timezone-aware ISO 8601 timestamps.")
    return parsed
