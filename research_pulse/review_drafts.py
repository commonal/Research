"""Review-only storage for paper reading drafts that failed automatic quality gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence
import json
import re

from research_pulse.production.pipeline import CandidateReceipt, ExtractedDraft, PaperCandidate
from research_pulse.production.quality import QualityGateResult


ReviewDraftStatus = Literal["needs_review", "rejected", "published", "expired"]


class ReviewDraftError(ValueError):
    """Raised when a review draft is missing, invalid, or in an illegal state."""


@dataclass(frozen=True)
class ReviewIssue:
    code: str
    severity: str
    claim_id: str | None = None
    anchor_id: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ReviewDraft:
    draft_id: str
    source_id: str
    knowledge_id: str
    knowledge_version: str
    title: str
    domain: str
    source_urls: tuple[str, ...]
    status: ReviewDraftStatus
    markdown: str
    content_sha256: str
    quality_issues: tuple[ReviewIssue, ...]
    evidence_boundary: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.draft_id.strip() or not self.source_id.strip() or not self.knowledge_id.strip():
            raise ReviewDraftError("A review draft needs stable identity fields.")
        if not self.knowledge_version.strip() or not self.title.strip() or not self.domain.strip():
            raise ReviewDraftError("A review draft needs version, title, and domain.")
        if not self.source_urls or any(not url.startswith(("https://", "http://")) for url in self.source_urls):
            raise ReviewDraftError("A review draft needs HTTP(S) source URLs.")
        if self.status not in {"needs_review", "rejected", "published", "expired"}:
            raise ReviewDraftError("Review draft status is invalid.")
        expected = sha256(self.markdown.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected:
            raise ReviewDraftError("Review draft content hash does not match Markdown.")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.updated_at, "updated_at")

    @classmethod
    def from_extracted(
        cls,
        *,
        candidate: PaperCandidate,
        draft: ExtractedDraft,
        quality: QualityGateResult,
        draft_id: str,
        now: str | None = None,
    ) -> "ReviewDraft":
        timestamp = now or _utc_now()
        boundary = draft.reading_analysis.reading_boundary if draft.reading_analysis else "自动质量门禁未通过，正文仅供人工复核。"
        issues = tuple(
            ReviewIssue(issue.code, issue.severity, issue.claim_id, issue.anchor_id, issue.message)
            for issue in quality.issues
        )
        return cls(
            draft_id=draft_id,
            source_id=candidate.source_id,
            knowledge_id=draft.asset.knowledge_id,
            knowledge_version=draft.asset.knowledge_version,
            title=draft.asset.title,
            domain=draft.asset.domain,
            source_urls=draft.asset.source_urls,
            status="needs_review",
            markdown=draft.asset.body,
            content_sha256=sha256(draft.asset.body.encode("utf-8")).hexdigest(),
            quality_issues=issues,
            evidence_boundary=boundary,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def transition(self, status: ReviewDraftStatus, *, now: str | None = None) -> "ReviewDraft":
        if self.status != "needs_review":
            raise ReviewDraftError("Only a needs_review draft can be transitioned.")
        return ReviewDraft(
            **{
                **asdict(self),
                "quality_issues": self.quality_issues,
                "source_urls": self.source_urls,
                "status": status,
                "updated_at": now or _utc_now(),
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "source_id": self.source_id,
            "knowledge_id": self.knowledge_id,
            "knowledge_version": self.knowledge_version,
            "title": self.title,
            "domain": self.domain,
            "source_urls": list(self.source_urls),
            "status": self.status,
            "markdown": self.markdown,
            "content_sha256": self.content_sha256,
            "quality_issues": [asdict(issue) for issue in self.quality_issues],
            "evidence_boundary": self.evidence_boundary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ReviewDraftStore(Protocol):
    def save(self, draft: ReviewDraft) -> ReviewDraft: ...

    def get(self, draft_id: str) -> ReviewDraft | None: ...

    def list_for_knowledge(self, knowledge_id: str, *, include_closed: bool = False) -> Sequence[ReviewDraft]: ...

    def transition(self, draft_id: str, status: ReviewDraftStatus) -> ReviewDraft: ...


class FilesystemReviewDraftStore:
    """Store Markdown and safe metadata outside the canonical published papers tree."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root.resolve()
        self.review_root = (self.vault_root / "review-drafts").resolve()
        if not self.review_root.is_relative_to(self.vault_root):
            raise ReviewDraftError("Review draft root escapes the managed vault.")

    def save(self, draft: ReviewDraft) -> ReviewDraft:
        markdown_path, metadata_path = self._paths(draft)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        if metadata_path.exists() or markdown_path.exists():
            existing = self._read(metadata_path, markdown_path)
            if existing.content_sha256 != draft.content_sha256 or existing.knowledge_id != draft.knowledge_id:
                raise ReviewDraftError("A review draft ID cannot be reused with different content.")
        else:
            _atomic_text(markdown_path, draft.markdown)
        _atomic_text(metadata_path, json.dumps(self._metadata(draft), ensure_ascii=False, sort_keys=True) + "\n")
        return draft

    def save_review_draft(
        self,
        *,
        candidate: PaperCandidate,
        draft: ExtractedDraft,
        quality: QualityGateResult,
    ) -> ReviewDraft:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        draft_id = f"review:{_safe(draft.asset.knowledge_id)}:{stamp}"
        return self.save(
            ReviewDraft.from_extracted(
                candidate=candidate,
                draft=draft,
                quality=quality,
                draft_id=draft_id,
            )
        )

    def get(self, draft_id: str) -> ReviewDraft | None:
        if not draft_id.strip():
            return None
        metadata_path = self.review_root / "_by-id" / f"{_path_key(draft_id)}.json"
        if not metadata_path.exists():
            for candidate in self.review_root.rglob(f"{_path_key(draft_id)}.json") if self.review_root.exists() else ():
                metadata_path = candidate
                break
        if not metadata_path.exists():
            return None
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        markdown_path = self._path_from_metadata(payload, metadata_path)
        return self._from_metadata(payload, markdown_path)

    def list_for_knowledge(self, knowledge_id: str, *, include_closed: bool = False) -> Sequence[ReviewDraft]:
        if not self.review_root.exists():
            return ()
        drafts: list[ReviewDraft] = []
        for metadata_path in self.review_root.rglob("*.json"):
            if metadata_path.name == "index.json":
                continue
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                if payload.get("knowledge_id") != knowledge_id:
                    continue
                draft = self._from_metadata(payload, self._path_from_metadata(payload, metadata_path))
                if include_closed or draft.status == "needs_review":
                    drafts.append(draft)
            except (OSError, ValueError, json.JSONDecodeError, ReviewDraftError):
                continue
        return tuple(sorted(drafts, key=lambda item: (item.created_at, item.draft_id), reverse=True))

    def transition(self, draft_id: str, status: ReviewDraftStatus) -> ReviewDraft:
        draft = self.get(draft_id)
        if draft is None:
            raise ReviewDraftError("review draft not found")
        updated = draft.transition(status)
        return self.save(updated)

    def _paths(self, draft: ReviewDraft) -> tuple[Path, Path]:
        folder = self.review_root / _path_key(draft.knowledge_id)
        filename = _path_key(draft.draft_id)
        return folder / f"{filename}.md", folder / f"{filename}.json"

    @staticmethod
    def _metadata(draft: ReviewDraft) -> dict[str, Any]:
        payload = draft.to_payload()
        payload.pop("markdown", None)
        payload["markdown_file"] = f"{_path_key(draft.draft_id)}.md"
        return payload

    def _path_from_metadata(self, payload: dict[str, Any], metadata_path: Path) -> Path:
        filename = payload.get("markdown_file")
        if filename != f"{_path_key(str(payload.get('draft_id', '')))}.md":
            raise ReviewDraftError("Review draft Markdown filename is invalid.")
        path = (metadata_path.parent / filename).resolve()
        if not path.is_relative_to(self.review_root) or path.parent != metadata_path.parent:
            raise ReviewDraftError("Review draft Markdown path escapes the review directory.")
        return path

    def _read(self, metadata_path: Path, markdown_path: Path) -> ReviewDraft:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        return self._from_metadata(payload, markdown_path)

    @staticmethod
    def _from_metadata(payload: dict[str, Any], markdown_path: Path) -> ReviewDraft:
        markdown = markdown_path.read_text(encoding="utf-8")
        raw_issues = payload.get("quality_issues", [])
        if not isinstance(raw_issues, list):
            raise ReviewDraftError("Review draft quality issues are invalid.")
        issues = tuple(ReviewIssue(**item) for item in raw_issues if isinstance(item, dict))
        values = dict(payload)
        values["markdown"] = markdown
        values["source_urls"] = tuple(values.get("source_urls", ()))
        values["quality_issues"] = issues
        values.pop("markdown_file", None)
        return ReviewDraft(**values)


REVIEW_DRAFT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_drafts (
    draft_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    knowledge_id TEXT NOT NULL,
    knowledge_version TEXT NOT NULL,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    source_urls JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('needs_review', 'rejected', 'published', 'expired')),
    content_sha256 TEXT NOT NULL,
    quality_issues JSONB NOT NULL,
    evidence_boundary TEXT NOT NULL,
    markdown_file TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS review_drafts_knowledge_created_idx
    ON review_drafts (knowledge_id, created_at DESC);
"""


class PostgresReviewDraftStore(FilesystemReviewDraftStore):
    """Postgres metadata index backed by the same isolated Markdown store."""

    def __init__(self, *, database_url: str, vault_root: Path) -> None:
        super().__init__(vault_root)
        self.database_url = database_url

    def initialize(self) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(REVIEW_DRAFT_SCHEMA_SQL)

    def save(self, draft: ReviewDraft) -> ReviewDraft:
        saved = super().save(draft)
        markdown_path, _ = self._paths(draft)
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO review_drafts (
                        draft_id, source_id, knowledge_id, knowledge_version, title, domain,
                        source_urls, status, content_sha256, quality_issues, evidence_boundary,
                        markdown_file, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (draft_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        draft.draft_id,
                        draft.source_id,
                        draft.knowledge_id,
                        draft.knowledge_version,
                        draft.title,
                        draft.domain,
                        json.dumps(list(draft.source_urls), ensure_ascii=False),
                        draft.status,
                        draft.content_sha256,
                        json.dumps([asdict(issue) for issue in draft.quality_issues], ensure_ascii=False),
                        draft.evidence_boundary,
                        markdown_path.relative_to(self.vault_root).as_posix(),
                        draft.created_at,
                        draft.updated_at,
                    ),
                )
        return saved

    def get(self, draft_id: str) -> ReviewDraft | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT draft_id FROM review_drafts WHERE draft_id = %s", (draft_id,))
                if cursor.fetchone() is None:
                    return None
        return super().get(draft_id)

    def list_for_knowledge(self, knowledge_id: str, *, include_closed: bool = False) -> Sequence[ReviewDraft]:
        drafts = super().list_for_knowledge(knowledge_id, include_closed=include_closed)
        return drafts

    def transition(self, draft_id: str, status: ReviewDraftStatus) -> ReviewDraft:
        # ``FilesystemReviewDraftStore.transition`` delegates back to ``save``;
        # the overridden save method therefore updates both the isolated file
        # and the Postgres index exactly once.  A second conditional UPDATE here
        # would observe the already-updated status and incorrectly reject the
        # transition.
        return super().transition(draft_id, status)

    def _connection(self):
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("psycopg is not installed.") from error
        return psycopg.connect(self.database_url)


class ReviewDraftApprover(Protocol):
    def approve(self, draft: ReviewDraft) -> CandidateReceipt: ...


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "draft"


def _path_key(value: str, *, max_length: int = 48) -> str:
    """Keep Windows review paths short while retaining deterministic identity."""

    safe = _safe(value)
    if len(safe) <= max_length:
        return safe
    digest = sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{safe[:max_length - len(digest) - 1]}-{digest}"


def _atomic_text(path: Path, text: str) -> None:
    # Keep the write safe even if a concurrent cleanup/retry removed the
    # draft directory after the caller's initial mkdir.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _timestamp(value: str, field: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReviewDraftError(f"{field} must be an ISO timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewDraftError(f"{field} must be timezone-aware.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
