"""Independent NoteRun lifecycle over the fixed traceable reading service.

Observability: every note run now emits an append-only, sanitised event stream
(run_started / phase_changed / operation_completed / note_completed /
note_failed). Operation events carry the real reading operation name, model,
and provider-reported token usage plus elapsed time, so the workbench can show
*which* operations ran and *how much* they cost without persisting prompts,
keys, or block bodies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
import re
from threading import Lock
import time
import json
from typing import Protocol
from uuid import uuid4

from research_pulse.knowledge.models import KnowledgeAssetError, KnowledgeBundle
from research_pulse.knowledge.reader import KnowledgeReader
from research_pulse.traceable_reading.service import (
    TraceableReadingRequest,
    safe_error as _safe_error_text,
)
from research_pulse.workbench.agent_runtime import SafeAgentEvent
from research_pulse.workbench.models import NoteRun, NoteRunStatus, ParseStatus
from research_pulse.workbench.note_events import NoteRunEventProjector, RunOperationContext

_KNOWLEDGE_ID_FRONTMATTER = re.compile(r"(?ms)^---\s*\n(.*?)\n---")

_NO_HTTP_SOURCE_ERROR = (
    "本地上传论文缺少 HTTP 来源，生成的知识笔记无法通过知识库解析"
    "（source_urls 必须是 HTTP(S) URL）。请改用 arXiv 链接或公开 PDF URL 添加该论文后重试。"
)


def _knowledge_id_from_markdown(path: Path) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = _KNOWLEDGE_ID_FRONTMATTER.search(text)
    if not match:
        return None
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "knowledge_id":
            cleaned = value.strip().strip('"').strip("'").strip()
            return cleaned or None
    return None


def _managed_receipt_path(value: object, vault_root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value)
    candidate = (Path.cwd() / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        candidate.relative_to(vault_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.suffix.lower() == ".json" and candidate.is_file() else None


def _cleanup_orphan_asset(path: Path) -> None:
    """Best-effort removal of a note asset the knowledge layer cannot parse."""
    if not path:
        return
    target = Path(path)
    try:
        if target.is_file():
            target.unlink()
        for suffix in (".note.json", ".evidence.json", ".provenance.json", ".manifest.json"):
            sidecar = target.with_name(target.name + suffix)
            if sidecar.is_file():
                sidecar.unlink()
        assets = target.parent / "assets"
        if assets.is_dir():
            for child in assets.iterdir():
                if child.is_file():
                    child.unlink()
            try:
                assets.rmdir()
            except OSError:
                pass
        try:
            target.parent.rmdir()
        except OSError:
            pass
    except OSError:
        pass


class NotePipeline(Protocol):
    def run(self, request: TraceableReadingRequest): ...


class NoteRunRepository(Protocol):
    def get(self, session_id: str): ...
    def get_paper(self, paper_id: str): ...
    def has_paper_link(self, session_id: str, paper_id: str) -> bool: ...
    def insert_note_run(self, run: NoteRun) -> None: ...
    def save_note_run(self, run: NoteRun) -> None: ...
    def get_note_run(self, note_run_id: str) -> NoteRun | None: ...
    def list_note_runs(self, session_id: str) -> tuple[NoteRun, ...]: ...
    def append_note_run_event(self, note_run_id: str, event: SafeAgentEvent) -> None: ...
    def list_note_run_events(self, note_run_id: str) -> tuple[SafeAgentEvent, ...]: ...


class NoteRunService:
    def __init__(
        self,
        repository: NoteRunRepository,
        pipeline: NotePipeline,
        *,
        vault_root: Path,
        cache_root: Path,
        note_run_id_factory: Callable[[], str] = lambda: str(uuid4()),
        events: NoteRunEventProjector | None = None,
        reader: KnowledgeReader | None = None,
    ) -> None:
        self.repository = repository
        self.pipeline = pipeline
        self.vault_root = vault_root
        self.cache_root = cache_root
        self.note_run_id_factory = note_run_id_factory
        self.events = events
        self.reader = reader
        # The HTTP boundary may enqueue the same run more than once (double
        # clicks, retries, or two browser tabs).  A per-run lock makes the
        # generation and publication transitions single-owner without
        # serialising unrelated papers.
        self._run_locks: dict[str, Lock] = {}
        self._run_locks_guard = Lock()

    def create(self, session_id: str, paper_id: str) -> NoteRun:
        session = self.repository.get(session_id)
        paper = self.repository.get_paper(paper_id)
        if session is None or paper is None or not self.repository.has_paper_link(session_id, paper_id):
            raise KeyError("session paper not found")
        if paper.parse_status != ParseStatus.READY or not paper.material_root:
            raise ValueError("paper material is not ready")
        attempts = self.repository.list_note_runs(session_id)
        # A double click, a retrying browser request, or two open tabs must not
        # start two expensive reading jobs for the same paper.  A terminal
        # published/failed run remains available for an explicit new attempt.
        active = next(
            (
                item for item in reversed(attempts)
                if item.paper_id == paper_id
                and item.status in {NoteRunStatus.QUEUED, NoteRunStatus.GENERATING, NoteRunStatus.AWAITING_APPROVAL}
            ),
            None,
        )
        if active is not None:
            return active
        paper_attempts = [item.attempt for item in attempts if item.paper_id == paper_id]
        run = NoteRun(
            self.note_run_id_factory(),
            paper_id,
            session_id,
            NoteRunStatus.QUEUED,
            attempt=max(paper_attempts, default=0) + 1,
        )
        self.repository.insert_note_run(run)
        return run

    def _managed_draft_path(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            candidate.relative_to(self.vault_root.resolve())
        except ValueError:
            return None
        return candidate if candidate.suffix.lower() == ".md" and candidate.is_file() else None

    @staticmethod
    def _rewrite_receipt_status(path: Path | None, status: str) -> str | None:
        """Best-effort status mirror for the receipt written beside a note."""
        if path is None:
            return None
        try:
            original = path.read_text(encoding="utf-8")
            payload = json.loads(original)
            if not isinstance(payload, dict):
                return None
            payload["publication_status"] = status
            updated = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(updated, encoding="utf-8")
            temporary.replace(path)
            return original
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _rewrite_publication_status(path: Path, status: str) -> str:
        """Rewrite only front matter, returning the original text for rollback."""
        original = path.read_text(encoding="utf-8")
        match = _KNOWLEDGE_ID_FRONTMATTER.search(original)
        if not match:
            raise ValueError("note draft is missing front matter")
        front = match.group(1)
        status_line = f"publication_status: {json.dumps(status, ensure_ascii=False)}"
        status_pattern = re.compile(r"^publication_status:\s*[^\r\n]*$")
        replacement_lines: list[str] = []
        found_status = False
        for line in front.splitlines():
            if status_pattern.fullmatch(line):
                # Keep one canonical status field.  This also repairs drafts
                # produced by the old non-idempotent implementation, where a
                # second call with the same status appended a duplicate key.
                if not found_status:
                    replacement_lines.append(status_line)
                    found_status = True
                continue
            replacement_lines.append(line)
        if not found_status:
            replacement_lines.append(status_line)
        replacement = "\n".join(replacement_lines)
        if replacement == front:
            return original
        updated = original[: match.start(1)] + replacement + original[match.end(1) :]
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(path)
        return original

    def _hold_for_approval(self, value: object) -> Path | None:
        path = self._managed_draft_path(value)
        if path is None:
            return None
        self._rewrite_publication_status(path, "needs_review")
        return path

    def _lock_for(self, note_run_id: str) -> Lock:
        with self._run_locks_guard:
            return self._run_locks.setdefault(note_run_id, Lock())

    # -- observability -------------------------------------------------------

    def _emit(
        self,
        note_run_id: str,
        event_type: str,
        summary: str,
        *,
        stable_ids: Mapping[str, str] | None = None,
        counters: Mapping[str, int] | None = None,
    ) -> None:
        if self.events is None:
            return
        self.events.emit(
            note_run_id, event_type, summary,
            stable_ids=dict(stable_ids or {}),
            counters=dict(counters or {}),
        )

    def _emit_operation(self, operation: str, model: str, usage: Mapping[str, object] | None, elapsed_ms: int) -> None:
        run_id = RunOperationContext.note_run_id()
        if run_id is None or self.events is None:
            return
        counters: dict[str, int] = {"elapsed_ms": max(0, elapsed_ms)}
        if isinstance(usage, Mapping):
            if isinstance(usage.get("prompt_tokens"), int):
                counters["input_tokens"] = usage["prompt_tokens"]  # type: ignore[assignment]
            if isinstance(usage.get("completion_tokens"), int):
                counters["output_tokens"] = usage["completion_tokens"]  # type: ignore[assignment]
        self._emit(run_id, "operation_completed", f"只读操作 {operation} 已完成", stable_ids={"operation": operation, "model": str(model or "")}, counters=counters)

    # -- lifecycle -----------------------------------------------------------

    def execute(self, note_run_id: str, *, domain: str = "research") -> NoteRun:
        """Run one queued note generation, at most once per process."""
        lock = self._lock_for(note_run_id)
        if not lock.acquire(blocking=False):
            return self.get(note_run_id)
        try:
            return self._execute(note_run_id, domain=domain)
        finally:
            lock.release()

    def _execute(self, note_run_id: str, *, domain: str = "research") -> NoteRun:
        run = self.get(note_run_id)
        # A duplicate background task must be harmless after the first task
        # has already moved the run out of the queue.
        if run.status != NoteRunStatus.QUEUED:
            return run
        paper = self.repository.get_paper(run.paper_id)
        assert paper is not None and paper.material_root is not None
        run_started = time.monotonic()
        try:
            self._emit(note_run_id, "run_started", "正式笔记生成已启动", stable_ids={"note_run_id": note_run_id})
            generating = run.transition(NoteRunStatus.GENERATING)
            generating = NoteRun(**{**generating.__dict__, "stage": "reading_material"})
            self.repository.save_note_run(generating)
            self._emit(note_run_id, "phase_changed", "读取论文材料与证据块", stable_ids={"stage": "reading_material"})

            generating = NoteRun(**{**generating.__dict__, "stage": "generating_note"})
            self.repository.save_note_run(generating)
            self._emit(note_run_id, "phase_changed", "调用阅读模型生成并校验笔记", stable_ids={"stage": "generating_note"})

            RunOperationContext.set(note_run_id)
            started = time.monotonic()
            outcome = self.pipeline.run(TraceableReadingRequest(
                source_id=paper.paper_id, source_url=paper.source_url or f"urn:sha256:{paper.paper_id}",
                domain=domain, vault_root=self.vault_root, cache_root=self.cache_root,
                material_root=Path(paper.material_root),
                # The generation task creates a reviewable draft.  The
                # explicit publish action is the only place that promotes it
                # into the published knowledge read model.
                publication_status="needs_review",
            ))
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            if outcome.exit_code == 0:
                payload = outcome.payload
                published_path = str(payload.get("published_path") or payload.get("receipt_path") or "")
                knowledge_id = str(payload.get("knowledge_id") or "")
                if not knowledge_id and published_path:
                    knowledge_id = _knowledge_id_from_markdown(Path(published_path)) or ""
                draft_path = self._hold_for_approval(published_path)
                receipt_path = _managed_receipt_path(payload.get("receipt_path"), self.vault_root)
                self._rewrite_receipt_status(receipt_path, "needs_review")
                # A real production pipeline always returns a Markdown path.
                # Keep the stricter resolvability check for the configured
                # knowledge reader, while allowing small test/fallback
                # pipelines to expose a draft state without a filesystem path.
                if self.reader is not None and (draft_path is None or not knowledge_id):
                    _cleanup_orphan_asset(Path(published_path))
                    failed = generating.transition(NoteRunStatus.FAILED)
                    failed = NoteRun(**{**failed.__dict__, "stage": "failed", "receipt_path": published_path, "safe_error": _NO_HTTP_SOURCE_ERROR})
                    self.repository.save_note_run(failed)
                    self._emit(note_run_id, "note_failed", _NO_HTTP_SOURCE_ERROR, counters={"elapsed_ms": elapsed_ms})
                    return self.get(note_run_id)
                if self.reader is not None and draft_path is not None:
                    # Validate the canonical Markdown before exposing a
                    # publishable draft.  The reader intentionally ignores
                    # malformed files, which otherwise turns a deterministic
                    # format error into the vague "cannot be read" message
                    # only after the user clicks Publish.
                    try:
                        KnowledgeBundle.from_markdown(draft_path)
                    except (KnowledgeAssetError, OSError, ValueError) as exc:
                        _cleanup_orphan_asset(draft_path)
                        safe_error = f"论文笔记草稿校验失败：{_safe_error_text(exc)}"
                        failed = generating.transition(NoteRunStatus.FAILED)
                        failed = NoteRun(**{
                            **failed.__dict__,
                            "stage": "failed",
                            "receipt_path": str(receipt_path) if receipt_path else (str(payload.get("receipt_path") or "") or None),
                            "safe_error": safe_error,
                        })
                        self.repository.save_note_run(failed)
                        self._emit(note_run_id, "note_failed", "论文笔记草稿校验失败", counters={"elapsed_ms": elapsed_ms})
                        return self.get(note_run_id)
                awaiting = generating.transition(NoteRunStatus.AWAITING_APPROVAL)
                awaiting = NoteRun(**{
                    **awaiting.__dict__,
                    "stage": "awaiting_approval",
                    "receipt_path": str(receipt_path) if receipt_path else (str(payload.get("receipt_path") or "") or None),
                    "draft_path": str(draft_path) if draft_path else (published_path or None),
                    "knowledge_id": None,
                    "safe_error": None,
                })
                self.repository.save_note_run(awaiting)
                self._emit(
                    note_run_id,
                    "note_ready",
                    "论文笔记草稿已生成，等待你确认发布",
                    stable_ids={"stage": "awaiting_approval"},
                    counters={"elapsed_ms": elapsed_ms},
                )
            else:
                payload = outcome.payload
                published_path = str(payload.get("published_path") or payload.get("receipt_path") or "")
                failed = generating.transition(NoteRunStatus.FAILED)
                failed = NoteRun(**{**failed.__dict__, "stage": "failed", "receipt_path": published_path, "safe_error": str(payload.get("error") or "正式笔记生成失败，可重试")})
                self.repository.save_note_run(failed)
                self._emit(note_run_id, "note_failed", "正式笔记生成失败", counters={"elapsed_ms": elapsed_ms})
            return self.get(note_run_id)
        except Exception as exc:
            error_text = _safe_error_text(exc)
            try:
                failed = run.transition(NoteRunStatus.FAILED)
                failed = NoteRun(**{**failed.__dict__, "stage": "failed", "safe_error": error_text})
                self.repository.save_note_run(failed)
                self._emit(
                    note_run_id,
                    "note_failed",
                    "正式笔记生成失败",
                    counters={"elapsed_ms": max(0, round((time.monotonic() - run_started) * 1000))},
                )
            except Exception:
                pass
            return self.get(note_run_id)
        finally:
            RunOperationContext.set(None)

    def publish(self, note_run_id: str) -> NoteRun:
        """Publish a generated draft only after an explicit user action."""
        lock = self._lock_for(note_run_id)
        if not lock.acquire(blocking=False):
            return self.get(note_run_id)
        try:
            return self._publish(note_run_id)
        finally:
            lock.release()

    def _publish(self, note_run_id: str) -> NoteRun:
        """Publish a generated draft only after an explicit user action."""
        run = self.get(note_run_id)
        # Retrying a completed click is idempotent.  This is important when a
        # browser times out after the server committed the file and retries
        # the same action.
        if run.status == NoteRunStatus.PUBLISHED:
            return run
        if run.status != NoteRunStatus.AWAITING_APPROVAL:
            raise ValueError("only a note draft awaiting approval can be published")
        draft_path = self._managed_draft_path(run.draft_path)
        if draft_path is None:
            failed = NoteRun(**{
                **run.__dict__,
                "safe_error": "论文笔记草稿文件不可用，请重新生成",
            })
            self.repository.save_note_run(failed)
            self._emit(note_run_id, "publish_failed", "发布失败：论文笔记草稿文件不可用")
            return self.get(note_run_id)
        knowledge_id = run.knowledge_id or _knowledge_id_from_markdown(draft_path) or ""
        self._emit(note_run_id, "phase_changed", "已确认发布，正在写入知识库", stable_ids={"stage": "publishing"})
        original: str | None = None
        receipt_path = _managed_receipt_path(run.receipt_path, self.vault_root)
        original_receipt: str | None = None
        try:
            original = self._rewrite_publication_status(draft_path, "published")
            original_receipt = self._rewrite_receipt_status(receipt_path, "published")
            if not knowledge_id:
                raise ValueError("论文笔记缺少 knowledge_id")
            if self.reader is not None:
                try:
                    KnowledgeBundle.from_markdown(draft_path)
                except (KnowledgeAssetError, OSError, ValueError) as exc:
                    raise ValueError(f"发布前知识库格式校验失败：{_safe_error_text(exc)}") from exc
                if self.reader.get_current(knowledge_id) is None:
                    raise ValueError("发布后的论文笔记无法通过知识库读取")
            published = run.transition(NoteRunStatus.PUBLISHED)
            published = NoteRun(**{
                **published.__dict__,
                "stage": "published",
                # Keep the JSON receipt identity stable across the approval
                # boundary.  ``draft_path`` is the Markdown artifact being
                # promoted; it is not a receipt and must not replace it.
                "receipt_path": run.receipt_path,
                "draft_path": None,
                "knowledge_id": knowledge_id,
                "safe_error": None,
            })
            self.repository.save_note_run(published)
            self._emit(note_run_id, "note_completed", "论文笔记已发布并写入知识库", stable_ids={"knowledge_id": knowledge_id})
            return self.get(note_run_id)
        except Exception as exc:
            if original is not None:
                try:
                    draft_path.write_text(original, encoding="utf-8")
                except OSError:
                    pass
            if original_receipt is not None and receipt_path is not None:
                try:
                    receipt_path.write_text(original_receipt, encoding="utf-8")
                except OSError:
                    pass
            awaiting = NoteRun(**{
                **run.__dict__,
                "safe_error": _safe_error_text(exc),
                "stage": "awaiting_approval",
            })
            self.repository.save_note_run(awaiting)
            self._emit(note_run_id, "publish_failed", "论文笔记发布失败，可重试")
            return self.get(note_run_id)

    def get(self, note_run_id: str) -> NoteRun:
        run = self.repository.get_note_run(note_run_id)
        if run is None: raise KeyError(note_run_id)
        return run

    def list_for_session(self, session_id: str) -> tuple[NoteRun, ...]:
        if self.repository.get(session_id) is None:
            raise KeyError(session_id)
        return self.repository.list_note_runs(session_id)

    def draft_preview(self, note_run_id: str, *, max_chars: int = 12_000) -> str | None:
        """Return a bounded, body-only preview for a draft awaiting approval.

        The filesystem path remains an internal detail.  The preview is read
        only from the managed vault and strips front matter so the confirmation
        card shows the note a user is about to publish, rather than a storage
        manifest or an absolute path.
        """
        run = self.get(note_run_id)
        if run.status != NoteRunStatus.AWAITING_APPROVAL:
            return None
        path = self._managed_draft_path(run.draft_path)
        if path is None:
            return None
        try:
            body = _KNOWLEDGE_ID_FRONTMATTER.sub("", path.read_text(encoding="utf-8"), count=1).strip()
        except OSError:
            return None
        if not body:
            return None
        limit = max(1, int(max_chars))
        return body if len(body) <= limit else body[:limit].rstrip() + "\n\n…"

    def retry(self, note_run_id: str) -> NoteRun:
        previous = self.get(note_run_id)
        if previous.status != NoteRunStatus.FAILED:
            raise ValueError("only failed note runs can be retried")
        attempts = self.repository.list_note_runs(previous.triggering_session_id)
        paper_attempts = [item.attempt for item in attempts if item.paper_id == previous.paper_id]
        run = NoteRun(
            self.note_run_id_factory(),
            previous.paper_id,
            previous.triggering_session_id,
            NoteRunStatus.QUEUED,
            attempt=max(paper_attempts, default=0) + 1,
            previous_note_run_id=previous.note_run_id,
        )
        self.repository.insert_note_run(run)
        return run
