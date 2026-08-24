"""Durable publication manifests and deterministic index reconciliation.

Markdown plus its provenance sidecar remains the immutable source of truth.
This module records and repairs only the rebuildable PostgreSQL projection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
import json
import re

from research_pulse.knowledge.models import KnowledgeAsset, KnowledgeAssetError, KnowledgeBundle
from research_pulse.rag.contracts import IndexReceipt, ResearchRAG


IndexStatus = Literal["index_pending", "indexed", "index_failed"]
RecoveryStatus = Literal["recovered", "already_indexed", "damaged", "failed", "skipped"]


class ProcessedRegistry(Protocol):
    def mark_processed(self, source_id: str, knowledge_id: str) -> None: ...


@dataclass(frozen=True)
class PublicationPaths:
    markdown: Path
    provenance: Path
    manifest: Path


@dataclass(frozen=True)
class PublicationManifest:
    manifest_schema_version: int
    source_id: str
    knowledge_id: str
    knowledge_version: str
    markdown_path: str
    provenance_path: str
    content_sha256: str
    provenance_sha256: str
    publication_status: Literal["published"]
    index_status: IndexStatus
    chunk_ids: tuple[str, ...]
    attempt_count: int
    updated_at: str
    indexed_at: str | None = None
    last_error_code: str | None = None
    last_error_summary: str | None = None

    def __post_init__(self) -> None:
        if self.manifest_schema_version != 1:
            raise KnowledgeAssetError("Unsupported publication manifest schema version.")
        for field, value in (
            ("source_id", self.source_id),
            ("knowledge_id", self.knowledge_id),
            ("knowledge_version", self.knowledge_version),
        ):
            if not value.strip():
                raise KnowledgeAssetError(f"{field} must be a non-empty string.")
        _validate_relative_path(self.markdown_path, suffix=".md", field="markdown_path")
        _validate_relative_path(self.provenance_path, suffix=".provenance.json", field="provenance_path")
        _validate_hash(self.content_sha256, "content_sha256")
        _validate_hash(self.provenance_sha256, "provenance_sha256")
        if self.publication_status != "published":
            raise KnowledgeAssetError("A publication manifest only represents published knowledge.")
        if self.index_status not in {"index_pending", "indexed", "index_failed"}:
            raise KnowledgeAssetError("Publication manifest index_status is invalid.")
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int) or self.attempt_count < 0:
            raise KnowledgeAssetError("attempt_count must be a non-negative integer.")
        _validate_timestamp(self.updated_at, "updated_at")
        if len(self.chunk_ids) != len(set(self.chunk_ids)) or any(not item.strip() for item in self.chunk_ids):
            raise KnowledgeAssetError("Manifest chunk IDs must be unique non-empty strings.")
        if self.index_status == "indexed":
            if not self.chunk_ids or not self.indexed_at:
                raise KnowledgeAssetError("An indexed manifest needs chunk IDs and indexed_at.")
            _validate_timestamp(self.indexed_at, "indexed_at")
            if self.last_error_code or self.last_error_summary:
                raise KnowledgeAssetError("An indexed manifest cannot retain an active error.")
        elif self.indexed_at is not None:
            raise KnowledgeAssetError("Only an indexed manifest may define indexed_at.")
        if self.last_error_code is not None and not self.last_error_code.strip():
            raise KnowledgeAssetError("last_error_code must be null or non-empty.")
        if self.last_error_summary is not None and not self.last_error_summary.strip():
            raise KnowledgeAssetError("last_error_summary must be null or non-empty.")

    @classmethod
    def pending(
        cls,
        *,
        source_id: str,
        asset: KnowledgeAsset,
        markdown_path: str,
        provenance_path: str,
        now: str | None = None,
    ) -> "PublicationManifest":
        if asset.schema_version not in {2, 3} or not asset.provenance_sha256:
            raise KnowledgeAssetError("A manifest requires a persisted schema-v2+ asset.")
        return cls(
            manifest_schema_version=1,
            source_id=source_id,
            knowledge_id=asset.knowledge_id,
            knowledge_version=asset.knowledge_version,
            markdown_path=markdown_path,
            provenance_path=provenance_path,
            content_sha256=asset.content_sha256,
            provenance_sha256=asset.provenance_sha256,
            publication_status="published",
            index_status="index_pending",
            chunk_ids=(),
            attempt_count=0,
            updated_at=now or _utc_now(),
        )

    def begin_attempt(self, *, now: str | None = None) -> "PublicationManifest":
        if self.index_status == "indexed":
            return self
        return replace(
            self,
            index_status="index_pending",
            attempt_count=self.attempt_count + 1,
            updated_at=now or _utc_now(),
            indexed_at=None,
            last_error_code=None,
            last_error_summary=None,
        )

    def failed(self, error: Exception, *, now: str | None = None) -> "PublicationManifest":
        if self.index_status == "indexed":
            return self
        code, summary = safe_error(error)
        return replace(
            self,
            index_status="index_failed",
            updated_at=now or _utc_now(),
            indexed_at=None,
            last_error_code=code,
            last_error_summary=summary,
        )

    def indexed(self, receipt: IndexReceipt, *, now: str | None = None) -> "PublicationManifest":
        if (receipt.knowledge_id, receipt.knowledge_version) != (self.knowledge_id, self.knowledge_version):
            raise KnowledgeAssetError("Index receipt identity does not match the publication manifest.")
        timestamp = now or _utc_now()
        return replace(
            self,
            index_status="indexed",
            chunk_ids=receipt.chunk_ids,
            updated_at=timestamp,
            indexed_at=timestamp,
            last_error_code=None,
            last_error_summary=None,
        )

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["chunk_ids"] = list(self.chunk_ids)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "PublicationManifest":
        if not isinstance(payload, dict):
            raise KnowledgeAssetError("A publication manifest must be a JSON object.")
        expected = {
            "manifest_schema_version", "source_id", "knowledge_id", "knowledge_version",
            "markdown_path", "provenance_path", "content_sha256", "provenance_sha256",
            "publication_status", "index_status", "chunk_ids", "attempt_count", "updated_at",
            "indexed_at", "last_error_code", "last_error_summary",
        }
        if set(payload) != expected:
            raise KnowledgeAssetError("Publication manifest fields are incomplete or unsupported.")
        chunk_ids = payload.get("chunk_ids")
        if not isinstance(chunk_ids, list) or not all(isinstance(item, str) for item in chunk_ids):
            raise KnowledgeAssetError("Manifest chunk_ids must be a string array.")
        values = dict(payload)
        values["chunk_ids"] = tuple(chunk_ids)
        try:
            return cls(**values)
        except TypeError as error:
            raise KnowledgeAssetError("Publication manifest field types are invalid.") from error


@dataclass(frozen=True)
class RecoveryItem:
    manifest_path: str
    status: RecoveryStatus
    knowledge_id: str | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        if not self.manifest_path.strip():
            raise ValueError("A recovery item needs a manifest path.")
        if self.status not in {"recovered", "already_indexed", "damaged", "failed", "skipped"}:
            raise ValueError("Recovery item status is invalid.")


@dataclass(frozen=True)
class ReconciliationSummary:
    items: tuple[RecoveryItem, ...]

    def count(self, status: RecoveryStatus) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def has_failures(self) -> bool:
        return self.count("damaged") > 0 or self.count("failed") > 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "recovered": self.count("recovered"),
            "already_indexed": self.count("already_indexed"),
            "damaged": self.count("damaged"),
            "failed": self.count("failed"),
            "skipped": self.count("skipped"),
            "items": [asdict(item) for item in self.items],
        }


class ManifestStore:
    """Atomic JSON store constrained to the managed knowledge vault."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root.resolve()

    def paths_for(self, asset: KnowledgeAsset) -> PublicationPaths:
        paper_id = safe_filename(asset.knowledge_id.removeprefix("kp:arxiv:"))
        version = safe_filename(asset.knowledge_version)
        markdown = self.vault_root / "papers" / paper_id / f"{version}.md"
        return PublicationPaths(
            markdown=self.ensure_managed(markdown),
            provenance=self.ensure_managed(markdown.with_suffix(".provenance.json")),
            manifest=self.ensure_managed(markdown.with_suffix(".manifest.json")),
        )

    def relative(self, path: Path) -> str:
        return self.ensure_managed(path).relative_to(self.vault_root).as_posix()

    def managed_path(self, relative_path: str) -> Path:
        _validate_relative_path(relative_path, suffix=None, field="managed path")
        pure = PurePosixPath(relative_path)
        return self.ensure_managed(self.vault_root.joinpath(*pure.parts))

    def ensure_managed(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.vault_root):
            raise KnowledgeAssetError("Publication path escapes the managed vault.")
        return resolved

    def read(self, path: Path) -> PublicationManifest:
        managed = self.ensure_managed(path)
        _require_manifest_path(managed)
        try:
            raw = managed.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except OSError as error:
            raise KnowledgeAssetError("Publication manifest is missing or unreadable.") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KnowledgeAssetError("Publication manifest is invalid JSON.") from error
        return PublicationManifest.from_payload(payload)

    def write(self, path: Path, manifest: PublicationManifest) -> PublicationManifest:
        managed = self.ensure_managed(path)
        _require_manifest_path(managed)
        managed.parent.mkdir(parents=True, exist_ok=True)
        if managed.exists():
            existing = self.read(managed)
            if _manifest_immutable_identity(existing) != _manifest_immutable_identity(manifest):
                raise KnowledgeAssetError("A manifest path cannot be reused for another knowledge version.")
            if existing.index_status == "indexed" and manifest.index_status != "indexed":
                return existing
        text = json.dumps(manifest.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        temporary = managed.with_suffix(managed.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        reparsed = PublicationManifest.from_payload(json.loads(temporary.read_text(encoding="utf-8")))
        if reparsed != manifest:
            raise KnowledgeAssetError("The written publication manifest failed verification.")
        temporary.replace(managed)
        return manifest

    def iter_manifest_paths(self) -> tuple[Path, ...]:
        root = self.vault_root / "papers"
        if not root.exists():
            return ()
        return tuple(sorted(self.ensure_managed(path) for path in root.rglob("*.manifest.json") if path.is_file()))

    def iter_markdown_paths(self) -> tuple[Path, ...]:
        root = self.vault_root / "papers"
        if not root.exists():
            return ()
        return tuple(sorted(self.ensure_managed(path) for path in root.rglob("*.md") if path.is_file()))


@dataclass
class PublicationReconciler:
    store: ManifestStore
    rag: ResearchRAG
    processed_registry: ProcessedRegistry

    def reconcile(self, *, include_indexed: bool = False) -> ReconciliationSummary:
        items: list[RecoveryItem] = []
        migrated_paths: set[Path] = set()
        existing_manifests = set(self.store.iter_manifest_paths())
        for markdown_path in self.store.iter_markdown_paths():
            manifest_path = markdown_path.with_suffix(".manifest.json")
            if manifest_path in existing_manifests:
                continue
            migration = self._migrate_manifest(markdown_path, manifest_path)
            items.append(migration)
            if migration.code == "manifest_created":
                migrated_paths.add(manifest_path)

        manifest_paths = tuple(sorted(existing_manifests | migrated_paths))
        for manifest_path in manifest_paths:
            items.append(self._reconcile_manifest(manifest_path, include_indexed=include_indexed))
        return ReconciliationSummary(tuple(items))

    def _migrate_manifest(self, markdown_path: Path, manifest_path: Path) -> RecoveryItem:
        relative = self.store.relative(manifest_path)
        try:
            bundle = KnowledgeBundle.from_markdown(markdown_path)
            asset = bundle.asset
            if not bundle.answer_eligible or asset.schema_version not in {2, 3} or asset.publication_status != "published":
                return RecoveryItem(relative, "skipped", asset.knowledge_id, "legacy_or_unpublished")
            prefix = "kp:arxiv:"
            if not asset.knowledge_id.startswith(prefix) or not asset.knowledge_id.removeprefix(prefix).strip():
                return RecoveryItem(relative, "skipped", asset.knowledge_id, "missing_source_identity")
            assert asset.provenance_file
            provenance_path = markdown_path.parent / asset.provenance_file
            manifest = PublicationManifest.pending(
                source_id=asset.knowledge_id.removeprefix(prefix),
                asset=asset,
                markdown_path=self.store.relative(markdown_path),
                provenance_path=self.store.relative(provenance_path),
            )
            self.store.write(manifest_path, manifest)
            return RecoveryItem(relative, "skipped", asset.knowledge_id, "manifest_created")
        except Exception as error:
            return RecoveryItem(relative, "damaged", code=safe_error(error)[0])

    def _reconcile_manifest(self, manifest_path: Path, *, include_indexed: bool) -> RecoveryItem:
        relative = self.store.relative(manifest_path)
        manifest: PublicationManifest | None = None
        active_manifest: PublicationManifest | None = None
        try:
            manifest = self.store.read(manifest_path)
            active_manifest = manifest
            if manifest.index_status == "indexed" and not include_indexed:
                return RecoveryItem(relative, "already_indexed", manifest.knowledge_id)
            markdown_path = self.store.managed_path(manifest.markdown_path)
            provenance_path = self.store.managed_path(manifest.provenance_path)
            if markdown_path.with_suffix(".manifest.json") != manifest_path:
                raise KnowledgeAssetError("Manifest filename does not match its Markdown version.")
            if markdown_path.with_suffix(".provenance.json") != provenance_path:
                raise KnowledgeAssetError("Manifest provenance path does not match its Markdown version.")
            bundle = KnowledgeBundle.from_markdown(markdown_path)
            validate_manifest_bundle(manifest, bundle, provenance_path)
            attempt = manifest.begin_attempt()
            self.store.write(manifest_path, attempt)
            active_manifest = attempt
            receipt = self.rag.publish(bundle)
            self.processed_registry.mark_processed(manifest.source_id, manifest.knowledge_id)
            self.store.write(manifest_path, attempt.indexed(receipt))
            status: RecoveryStatus = "already_indexed" if manifest.index_status == "indexed" else "recovered"
            return RecoveryItem(relative, status, manifest.knowledge_id)
        except KnowledgeAssetError as error:
            return RecoveryItem(relative, "damaged", manifest.knowledge_id if manifest else None, safe_error(error)[0])
        except Exception as error:
            if active_manifest is not None and active_manifest.index_status != "indexed":
                try:
                    self.store.write(manifest_path, active_manifest.failed(error))
                except Exception:
                    pass
            return RecoveryItem(relative, "failed", manifest.knowledge_id if manifest else None, safe_error(error)[0])


def validate_manifest_bundle(
    manifest: PublicationManifest,
    bundle: KnowledgeBundle,
    provenance_path: Path,
) -> None:
    asset = bundle.asset
    if not bundle.answer_eligible or asset.schema_version not in {2, 3} or asset.publication_status != "published":
        raise KnowledgeAssetError("Manifest recovery requires a complete published schema-v2+ bundle.")
    if (manifest.knowledge_id, manifest.knowledge_version) != (asset.knowledge_id, asset.knowledge_version):
        raise KnowledgeAssetError("Manifest and Markdown knowledge identity do not match.")
    if manifest.content_sha256 != asset.content_sha256 or manifest.provenance_sha256 != asset.provenance_sha256:
        raise KnowledgeAssetError("Manifest and bundle hashes do not match.")
    if not asset.provenance_file or provenance_path.name != asset.provenance_file:
        raise KnowledgeAssetError("Manifest provenance path does not match the Markdown sidecar.")


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "asset"


def safe_error(error: Exception) -> tuple[str, str]:
    code = type(error).__name__[:80]
    message = " ".join(str(error).split()) or code
    patterns = (
        r"(?i)bearer\s+[A-Za-z0-9._~+\-/=]+",
        r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\bsk-[A-Za-z0-9_-]{8,}\b",
    )
    for pattern in patterns:
        message = re.sub(pattern, "[REDACTED]", message)
    return code, message[:240]


def _validate_relative_path(value: str, *, suffix: str | None, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise KnowledgeAssetError(f"{field} must be a non-empty POSIX relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or re.match(r"^[A-Za-z]:/", value) or ".." in path.parts or "." in path.parts:
        raise KnowledgeAssetError(f"{field} must stay inside the managed vault.")
    if suffix and not value.endswith(suffix):
        raise KnowledgeAssetError(f"{field} must end with {suffix}.")


def _validate_hash(value: str, field: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise KnowledgeAssetError(f"{field} must be a lowercase SHA-256 hash.")


def _validate_timestamp(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise KnowledgeAssetError(f"{field} must be an aware ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise KnowledgeAssetError(f"{field} must be an aware ISO timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KnowledgeAssetError(f"{field} must be an aware ISO timestamp.")


def _manifest_immutable_identity(manifest: PublicationManifest) -> tuple[str, ...]:
    return (
        manifest.source_id,
        manifest.knowledge_id,
        manifest.knowledge_version,
        manifest.markdown_path,
        manifest.provenance_path,
        manifest.content_sha256,
        manifest.provenance_sha256,
        manifest.publication_status,
    )


def _require_manifest_path(path: Path) -> None:
    if not path.name.endswith(".manifest.json"):
        raise KnowledgeAssetError("Publication manifests must use the .manifest.json suffix.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
