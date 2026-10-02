from __future__ import annotations

"""Production-facing reading route (Reading freeze milestone).

This is the Reading module's single seam into the three-layer architecture
(Acquisition / Reading / Knowledge Consumption):

    MaterialResolver(normalized_root, source_id) -> CanonicalPaperIR
        -> PaperReader.read(candidate, ir, intent) -> ReadingResult(draft, receipt)
        -> note (draft.markdown) -> minimal schema-v1 asset -> publish;
           receipt.status -> published / needs_review / failed (superseded later).

B1 scope (option A): the reading route consumes the server-produced normalized
blocks from a single ``normalized_root`` directory (``<root>/<source_id>/normalized/blocks.jsonl``);
it does NOT parse PDFs.  The Acquisition -> server material production -> sync
leg is a separate operational seam (documented in docs/architecture-consolidation.md).
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import re

from research_pulse.production.normalized import load_complete_normalized
from research_pulse.production.pipeline import PaperCandidate
from research_pulse.production.reading import (
    CanonicalPaperIR,
    DeepSeekPaperReadingModel,
    PaperReader,
    ReadingIntent,
    ReadingReceipt,
    ReadingResult,
)


@dataclass(frozen=True)
class ReaderConfig:
    normalized_root: Path
    language: str = "zh-CN"
    depth: str = "deep"
    image_subpath: str = "mineru/source/auto"
    mode: str = "generic"  # "generic" | "pedagogical"（默认 generic，教学化灰度）


class ReaderMaterialResolver:
    """Resolve a candidate's server-normalized blocks into a CanonicalPaperIR."""

    def __init__(self, normalized_root: Path, image_subpath: str = "mineru/source/auto") -> None:
        self.normalized_root = normalized_root
        self.image_subpath = image_subpath

    def resolve(self, candidate: PaperCandidate) -> CanonicalPaperIR:
        source_root = self.normalized_root / candidate.source_id
        normalized_dir = source_root / "normalized"
        blocks = load_complete_normalized(
            normalized_dir,
            expected_source_id=candidate.source_id,
            image_roots=(normalized_dir, source_root / self.image_subpath),
        )
        paper = CanonicalPaperIR.from_normalized(
            source_id=candidate.source_id,
            title=candidate.title,
            source_url=candidate.source_url,
            blocks=blocks,
        )
        paper = replace(paper, blocks=tuple(
            replace(block, image_path=str(resolved))
            if block.image_path and (resolved := self._resolve_image(candidate.source_id, block.image_path)) else block
            for block in paper.blocks
        ))
        return paper

    def _resolve_image(self, source_id: str, value: str) -> Path | None:
        for candidate in (
            self.normalized_root / source_id / "normalized" / value,
            self.normalized_root / source_id / value,
            self.normalized_root / source_id / self.image_subpath / value,
        ):
            if candidate.is_file():
                return candidate.resolve()
        return None


class ReaderProduction:
    """Read one paper through PaperReader and expose the note + status."""

    def __init__(self, config: ReaderConfig, model: Any | None = None) -> None:
        self.config = config
        self.model = model or DeepSeekPaperReadingModel.from_environment()
        self.resolver = ReaderMaterialResolver(config.normalized_root, config.image_subpath)

    def read(self, candidate: PaperCandidate) -> ReadingResult:
        paper = self.resolver.resolve(candidate)
        # PaperReader now defaults its writer/planner to the model's own, so no
        # lambdas are required.
        reader = PaperReader(self.model)
        result = reader.read(
            candidate,
            paper,
            ReadingIntent(language=self.config.language, depth=self.config.depth),
        )
        # Derive the asset evidence level from the actual source blocks (not a
        # hardcoded text label): multimodal when any formula/table/figure block
        # is present, otherwise text-only.
        evidence_level = (
            "full_text_multimodal"
            if any(block.kind in {"formula", "table", "figure"} for block in paper.blocks)
            else "full_text_text"
        )
        return replace(result, receipt=replace(result.receipt, evidence_level=evidence_level))

    @staticmethod
    def note_asset_markdown(note: str, candidate: PaperCandidate, receipt: Any) -> str:
        """Minimal schema-v1 asset: the reader's note is the knowledge body."""
        version = getattr(receipt, "completed_at", None) or getattr(receipt, "started_at", None) or "manual"
        pub = _receipt_publication(receipt)
        evidence_level = getattr(receipt, "evidence_level", "full_text_text")
        return f"""---
knowledge_id: "kp:arxiv:{candidate.source_id}"
knowledge_version: "{version}"
publication_status: "{pub}"
evidence_level: "{evidence_level}"
source_urls: ["{candidate.source_url}"]
domain: "{candidate.domain}"
title: "{_strip_html(candidate.title)}"
schema_version: 1
---
{note.strip()}
"""

    def produce(self, candidate: PaperCandidate) -> dict[str, Any]:
        """Run the reading route and return the note asset + status (no persistence)."""
        result = self.read(candidate)
        markdown = self.note_asset_markdown(result.draft.markdown, candidate, result.receipt)
        return {
            "receipt_status": result.receipt.status,
            "stop_reason": result.receipt.stop_reason,
            "note_chars": len(result.draft.markdown.strip()),
            "asset_markdown": markdown,
        }


@dataclass(frozen=True)
class ReaderVaultConfig:
    vault_root: Path


def _strip_html(value: str) -> str:
    """Strip inline HTML tags (MinerU 残留如 ``<sub>``/``<sup>``) from titles."""
    return re.sub(r"<[^>]+>", "", value).strip()


def _receipt_publication(receipt: Any) -> str:
    """Map a ReadingReceipt.status onto the PublishedNote publication bucket.

    ReadingReceipt.status is ``completed`` | ``bounded`` | ``failed``.  A
    completed/bounded read yields a human-readable note and is the canonical
    knowledge of record; a failed read must not land in the timeline.
    """
    status = getattr(receipt, "status", "failed")
    return "published" if status in {"completed", "bounded"} else "needs_review"


def _receipt_bucket(receipt: Any) -> str | None:
    """published -> ``papers``, needs_review -> ``staging``, else None (no file)."""
    publication = _receipt_publication(receipt)
    if publication == "published":
        return "papers"
    if publication == "needs_review":
        return "staging"
    return None


def _safe_version(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)


class ReaderNotePublisher:
    """Note-only publish path (option A1): write the schema-v1 markdown directly.

    Does NOT build a KnowledgeBundle/claims/anchors provenance.  The note is the
    knowledge; the retrieval/claims layer adapts later.  Mirrors the existing
    ``knowledge/papers/<source_id>/<version>.md`` location so downstream readers
    can find it the same way.

    NOTE on figures: the note writer contract says the "Renderer owns image
    placement", but image placement is NOT implemented on this route.  Figures
    are therefore described in prose only and their actual image assets are
    never injected into the note.  (Only the older KnowledgeBundle path has
    ``render_asset_markdown``, which is not used here.)  End-to-end figure
    embedding would require a renderer that resolves the normalized image
    assets to URLs the frontend can display via ``assetBaseUrl``.
    """

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root

    def path_for(self, candidate: PaperCandidate, receipt: Any) -> Path | None:
        version = getattr(receipt, "completed_at", None) or getattr(receipt, "started_at", None) or "manual"
        bucket = _receipt_bucket(receipt)
        if bucket is None:
            return None
        return self.vault_root / bucket / candidate.source_id / f"{_safe_version(version)}.md"

    def publish(self, candidate: PaperCandidate, note: str, receipt: Any) -> Path | None:
        """Write the schema-v1 note to the vault, routed by receipt status.

        completed/bounded -> knowledge/papers/<source_id>/<version>.md  (canonical)
        failed            -> knowledge/staging/<source_id>/<version>.md (not in timeline)
        """
        markdown = ReaderProduction.note_asset_markdown(note, candidate, receipt)
        path = self.path_for(candidate, receipt)
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return path


class ReaderProductionService:
    """Reading route as a drop-in production entry: resolve -> read -> publish.

    mode=generic（默认）走既有 PaperReader；mode=pedagogical 且注入 pedagogical 管线时走教学化闭环。
    """

    def __init__(
        self,
        config: ReaderConfig,
        vault_root: Path,
        model: Any | None = None,
        pedagogical: Any | None = None,
    ) -> None:
        self.reader = ReaderProduction(config, model)
        self.publisher = ReaderNotePublisher(vault_root)
        self.pedagogical = pedagogical
        self.config = config
        self._fallbacks: list[str] = []

    def process(self, candidate: PaperCandidate) -> dict[str, Any]:
        if self.config.mode == "pedagogical" and self.pedagogical is not None:
            result = self._process_pedagogical(candidate)
        else:
            result = self._process_generic(candidate)
        return self._persist_run_receipt(candidate, result)

    def _persist_run_receipt(
        self,
        candidate: PaperCandidate,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the process result so route and fallback decisions survive CLI exit."""
        import json

        result = dict(result)
        audit_markdown = result.pop("_audit_candidate_markdown", None)
        pedagogical_markdown = result.pop("_pedagogical_attempt_markdown", None)
        published_path = result.get("published_path")
        run_id = (
            Path(published_path).stem
            if published_path
            else _safe_version(datetime.now(timezone.utc).isoformat())
        )
        if audit_markdown is not None:
            audit_dir = self.publisher.vault_root / "audit" / candidate.source_id / run_id
            candidate_path = audit_dir / "final-candidate.md"
            artifact_report_path = audit_dir / "artifact-report.json"
            audit_dir.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(audit_markdown, encoding="utf-8")
            artifact_report_path.write_text(
                json.dumps(
                    {
                        "passed": result.get("gate_artifact_passed"),
                        "issues": result.get("gate_artifact_issue_details", []),
                        "publication_manifest": result.get("publication_manifest", {}),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            result["audit_candidate_path"] = str(candidate_path)
            result["artifact_report_path"] = str(artifact_report_path)
        if pedagogical_markdown is not None:
            attempt = dict(result.get("pedagogical_attempt") or {})
            audit_dir = self.publisher.vault_root / "audit" / candidate.source_id / run_id
            candidate_path = audit_dir / "pedagogical-candidate.md"
            report_path = audit_dir / "pedagogical-report.json"
            audit_dir.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(pedagogical_markdown, encoding="utf-8")
            report_path.write_text(
                json.dumps(attempt, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            attempt["candidate_path"] = str(candidate_path)
            attempt["report_path"] = str(report_path)
            result["pedagogical_attempt"] = attempt
        path = self.publisher.vault_root / "receipts" / candidate.source_id / f"{run_id}.json"
        persisted = {**result, "receipt_path": str(path)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
        return persisted

    def _process_generic(
        self,
        candidate: PaperCandidate,
        *,
        paper: CanonicalPaperIR | None = None,
        pedagogical_fallbacks: tuple[str, ...] = (),
        pedagogical_attempt: dict[str, Any] | None = None,
        pedagogical_attempt_markdown: str | None = None,
        pedagogical_failure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.reader.read(candidate)
        resolved_paper = paper or self.reader.resolver.resolve(candidate)
        from .pedagogical.contracts import RenderedNote

        extra = {
            "requested_route": "pedagogical" if pedagogical_fallbacks else "generic",
            "delivered_route": "generic",
            "fallback_used": bool(pedagogical_fallbacks),
            "pedagogical_fallbacks": list(pedagogical_fallbacks),
        }
        if pedagogical_attempt is not None:
            extra["pedagogical_attempt"] = pedagogical_attempt
        if pedagogical_attempt_markdown is not None:
            extra["_pedagogical_attempt_markdown"] = pedagogical_attempt_markdown
        if pedagogical_failure is not None:
            extra["pedagogical_failure"] = pedagogical_failure
        return self._finalize_publication(
            candidate,
            resolved_paper,
            RenderedNote(markdown=result.draft.markdown),
            result.receipt,
            extra=extra,
        )

    def _process_pedagogical(self, candidate: PaperCandidate) -> dict[str, Any]:
        from .pedagogical.source_quality import SourceQualityRejected

        paper = self.reader.resolver.resolve(candidate)
        fallback_start = len(self._fallbacks)   # 只报本次 run 的 fallback,不串前一篇
        try:
            result = self.pedagogical.run(paper)  # type: ignore[union-attr]
        except SourceQualityRejected as error:
            return self._source_quality_rejection(candidate, error)
        except Exception as error:
            # 模型输出偶发失败（如 S1 thesis 空、响应缺关键字段）先重试一次——有界、
            # 与 targeted repair 同口径；仍失败 = fatal → 整 run 回退 generic（generic 兜底）。
            try:
                result = self.pedagogical.run(paper)  # type: ignore[union-attr]
            except SourceQualityRejected as error2:
                return self._source_quality_rejection(candidate, error2)
            except Exception as error2:
                self._fallbacks.append(f"pedagogical:{type(error2).__name__}")
                return self._process_generic(
                    candidate,
                    paper=paper,
                    pedagogical_fallbacks=tuple(self._fallbacks[fallback_start:]),
                    pedagogical_failure={
                        "phase": "pedagogical_run",
                        "attempts": [
                            self._error_summary(error),
                            self._error_summary(error2),
                        ],
                    },
                )
        # Evidence + Artifact + Blind Reader + 零锚定：repair 后仍不过则不强发布，回退 generic。
        if (
            not result.evidence.passed
            or not result.artifact.passed
            or result.blind.overall != "pass"
            or result.zero_anchors
            or result.asset_usage.unreferenced_selected_ids
        ):
            reason = "gates:" + (
                "evidence" if not result.evidence.passed
                else "artifact" if not result.artifact.passed
                else result.blind.overall if result.blind.overall != "pass"
                else "zero_anchors"
                if result.zero_anchors
                else "unreferenced_assets"
            )
            self._fallbacks.append(reason)
            return self._process_generic(
                candidate,
                paper=paper,
                pedagogical_fallbacks=tuple(self._fallbacks[fallback_start:]),
                pedagogical_attempt=self._pedagogical_attempt(result),
                pedagogical_attempt_markdown=result.note.markdown,
            )
        evidence_level = (
            "full_text_multimodal"
            if any(block.kind in {"formula", "table", "figure"} for block in paper.blocks)
            else "full_text_text"
        )
        receipt = ReadingReceipt(
            source_id=candidate.source_id,
            status="completed",
            text_model="pedagogical",
            vision_model=None,
            completed_at=datetime.now(timezone.utc).isoformat(),
            stop_reason="completed",
            evidence_level=evidence_level,
        )
        usage = result.asset_usage
        source_report = result.source_quality
        return self._finalize_publication(
            candidate,
            paper,
            result.note,
            receipt,
            extra={
                "requested_route": "pedagogical",
                "delivered_route": "pedagogical",
                "fallback_used": False,
                "gate_source_status": source_report.status if source_report is not None else "unknown",
                "gate_source_issues": [issue.code for issue in source_report.issues] if source_report is not None else [],
                "gate_evidence_passed": bool(result.evidence.passed),
                "gate_blind_overall": result.blind.overall,
                "pedagogical_repaired": bool(result.repaired),
                "pedagogical_fallbacks": list(self._fallbacks[fallback_start:]),
                "asset_usage": {
                    "selected": list(usage.selected_ids),
                    "anchored": list(usage.anchored_ids),
                    "rendered": list(usage.rendered_ids),
                    "unreferenced_selected": list(usage.unreferenced_selected_ids),
                    "failed_render": list(usage.failed_render_ids),
                },
            },
        )

    @staticmethod
    def _pedagogical_attempt(result: Any) -> dict[str, Any]:
        """Serialize a rejected pedagogical result without changing its gate decision."""
        artifact_issues = [
            {
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
                "line": issue.line,
            }
            for issue in result.artifact.issues
        ]
        usage = result.asset_usage
        return {
            "gate_evidence_passed": bool(result.evidence.passed),
            "gate_evidence_issues": list(result.evidence.issues),
            "gate_artifact_passed": bool(result.artifact.passed),
            "gate_artifact_issue_details": artifact_issues,
            "gate_blind_overall": result.blind.overall,
            "gate_blind_dimensions": [
                {
                    "dimension": dimension.dimension,
                    "score": dimension.score,
                    "evidence": dimension.evidence,
                    "note": dimension.note,
                }
                for dimension in result.blind.dimensions
            ],
            "zero_anchors": bool(result.zero_anchors),
            "repaired": bool(result.repaired),
            "asset_usage": {
                "selected": list(usage.selected_ids),
                "anchored": list(usage.anchored_ids),
                "rendered": list(usage.rendered_ids),
                "unreferenced_selected": list(usage.unreferenced_selected_ids),
                "failed_render": list(usage.failed_render_ids),
            },
        }

    @staticmethod
    def _error_summary(error: Exception) -> dict[str, str]:
        summary = " ".join(str(error).split())[:500]
        return {
            "error_type": type(error).__name__,
            "summary": summary,
        }

    def _finalize_publication(
        self,
        candidate: PaperCandidate,
        paper: CanonicalPaperIR,
        note: Any,
        receipt: Any,
        *,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply the one final publication gate shared by every reading route."""
        from .pedagogical.artifact_quality import ArtifactQualityGate
        from .pedagogical.publication import PublicationAssetPublisher

        target_path = self.publisher.path_for(candidate, receipt)
        if target_path is None:
            return self._final_publication_rejection(
                candidate, note.markdown, None, (), "publication_path_unavailable", extra,
            )
        manifest = PublicationAssetPublisher(self.config.normalized_root).publish(
            paper,
            note.markdown,
            target_path.parent / "assets",
        )
        artifact = ArtifactQualityGate().evaluate(note, publication_manifest=manifest)
        if not artifact.passed:
            return self._final_publication_rejection(
                candidate, note.markdown, manifest, artifact.issues, None, extra,
            )

        path = self.publisher.publish(candidate, note.markdown, receipt)
        publication = _receipt_publication(receipt)
        return {
            "source_id": candidate.source_id,
            "receipt_status": receipt.status,
            "publication_status": publication,
            "status": publication,
            "stop_reason": receipt.stop_reason,
            "note_chars": len(note.markdown.strip()),
            "bucket": _receipt_bucket(receipt),
            "published_path": str(path) if path else None,
            "gate_artifact_passed": True,
            "gate_artifact_issues": [],
            "gate_artifact_issue_details": [],
            "publication_manifest": {
                "referenced": list(manifest.referenced_files),
                "copied": list(manifest.copied_files),
                "missing": list(manifest.missing_files),
                "collisions": list(manifest.collision_files),
                "copy_failed": list(manifest.copy_failed_files),
            },
            **extra,
        }

    @staticmethod
    def _final_publication_rejection(
        candidate: PaperCandidate,
        markdown: str,
        manifest: Any | None,
        artifact_issues: Any,
        explicit_reason: str | None,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        issue_codes = [issue.code for issue in artifact_issues]
        issue_details = [
            {
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
                "line": issue.line,
            }
            for issue in artifact_issues
        ]
        primary = explicit_reason or (issue_codes[0] if issue_codes else "publication_rejected")
        manifest_codes = {
            "missing_publication_asset",
            "publication_asset_collision",
            "publication_asset_copy_failed",
            "incomplete_publication_manifest",
            "publication_path_unavailable",
        }
        prefix = "publication_manifest" if primary in manifest_codes else "artifact_quality"
        return {
            "source_id": candidate.source_id,
            "receipt_status": "failed",
            "publication_status": "needs_review",
            "status": "needs_review",
            "stop_reason": f"{prefix}:{primary}",
            "note_chars": len(markdown.strip()),
            "bucket": None,
            "published_path": None,
            "gate_artifact_passed": False,
            "gate_artifact_issues": issue_codes,
            "gate_artifact_issue_details": issue_details,
            "publication_manifest": {
                "referenced": list(manifest.referenced_files) if manifest is not None else [],
                "copied": list(manifest.copied_files) if manifest is not None else [],
                "missing": list(manifest.missing_files) if manifest is not None else [],
                "collisions": list(manifest.collision_files) if manifest is not None else [],
                "copy_failed": list(manifest.copy_failed_files) if manifest is not None else [],
            },
            "_audit_candidate_markdown": markdown,
            **extra,
        }

    @staticmethod
    def _source_quality_rejection(candidate: PaperCandidate, error: Any) -> dict[str, Any]:
        report = error.report
        issue_codes = [issue.code for issue in report.issues]
        primary = issue_codes[0] if issue_codes else "rejected"
        return {
            "source_id": candidate.source_id,
            "receipt_status": "failed",
            "publication_status": "needs_review",
            "status": "needs_review",
            "stop_reason": f"source_quality:{primary}",
            "note_chars": 0,
            "bucket": None,
            "published_path": None,
            "gate_source_status": report.status,
            "gate_source_issues": issue_codes,
            "requested_route": "pedagogical",
            "delivered_route": None,
            "fallback_used": False,
            "pedagogical_fallbacks": [],
        }
