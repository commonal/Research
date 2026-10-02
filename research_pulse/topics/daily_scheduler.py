"""Single-worker daily paper scheduler for the note-only deployment.

Wires the file-backed topic store, the de-duplicating note-only reader runner
and the framework-free APScheduler wrapper into one entry point.  This process
is the *only* scheduler owner: run uvicorn with ``--workers 1`` so a daily tick
(or a manual re-run) cannot be duplicated by multiple workers reading the same
papers.

The heavy reader work is submitted to a bounded thread pool so a slow model
read never blocks the APScheduler thread; ``max_instances=1`` + ``coalesce`` on
the cron job keeps a missed/fast tick from stacking.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from research_pulse.scheduling import DailyScheduler, SchedulerConfig, ScheduleCoordinator
from research_pulse.topics.daily_runner import ReadingBatchRunner
from research_pulse.topics.file_store import FileTopicStore
from research_pulse.topics.service import TopicRunService
from research_pulse.topics.traceable_candidate_reader import TraceableCandidateReader


class DailyPaperScheduler:
    """Daily scheduler owner wired to the note-only reading chain.

    ``vault_root`` holds both the note feed (``papers/``) and the control state
    (``.topics/state.json``). ``normalized_root`` is retained as a compatibility
    name for the MinerU material cache; ``reconcile=True`` marks leftover active
    runs (from a previous crash/restart) as failed before scheduling anything new.
    """

    def __init__(
        self,
        *,
        vault_root: Path,
        normalized_root: Path,
        config: SchedulerConfig | None = None,
        reader_language: str = "zh-CN",
        reader_depth: str = "deep",
        reader_image_subpath: str = "mineru/source/auto",
        reader_mode: str = "pedagogical",
        max_workers: int = 1,
        reconcile: bool = True,
        server_parse_config: Any | None = None,
        scout_submissions_dir: Path | None = None,
        run_scout: bool | None = None,
        candidate_finder: Any = None,
    ) -> None:
        self.vault_root = vault_root
        self.normalized_root = normalized_root
        self.config = config or SchedulerConfig.from_environment(_environ())
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="research-pulse-daily")

        self.store = FileTopicStore(vault_root / ".topics")
        self.store.initialize()
        if reconcile:
            self.store.reconcile_active_runs()

        # Candidate source: the scout agent's daily submissions feed by default.
        # Scout runs rank/emit at most ``daily_limit`` candidates themselves, so
        # the scheduler consumes those instead of re-fetching arXiv directly.
        if candidate_finder is None:
            from research_pulse.topics.scout_finder import ScoutSubmissionFinder

            candidate_finder = ScoutSubmissionFinder(
                scout_submissions_dir or _default_submissions_dir(),
                scout_runner=_run_scout_if_requested(
                    _environment_bool("RESEARCH_PULSE_RUN_SCOUT", True)
                    if run_scout is None else run_scout
                ),
            )

        self.service = TopicRunService(
            self.store,
            runner=ReadingBatchRunner(
                candidate_finder=candidate_finder,
                reader_service=TraceableCandidateReader(
                    cache_root=normalized_root,
                    vault_root=vault_root,
                ),
                vault_root=vault_root,
            ),
        )
        self.coordinator = ScheduleCoordinator(
            self.service,
            submitter=lambda run_id: self._executor.submit(self.service.execute, run_id),
        )
        self.scheduler = DailyScheduler(self.config, self.coordinator)

    def start(self) -> None:
        self.scheduler.start()

    def status(self):
        return self.scheduler.status()

    def lifespan(self):
        @asynccontextmanager
        async def _lifespan(_app):
            self.start()
            try:
                yield
            finally:
                self.scheduler.shutdown()
                self._executor.shutdown(wait=True, cancel_futures=False)

        return _lifespan


def _environ():
    """Expose ``os.environ`` for SchedulerConfig without importing os at module top."""
    import os

    return os.environ


def _default_submissions_dir() -> Path:
    # scout/submissions lives under the repo root (sibling of research_pulse/).
    return Path(__file__).resolve().parents[2] / "scout" / "submissions"


def _run_scout_if_requested(enabled: bool):
    """Return a scout runner for ScoutSubmissionFinder, or None.

    When ``enabled`` the daily schedule invokes the scout agent (RSS + DeepSeek
    judgment + Top-K) to (re)produce today's submissions file before reading it.
    Disabled by default so the scheduler never spends API budget silently.
    """
    if not enabled:
        return None

    def _run() -> int:
        from research_pulse import scout

        return scout.main()

    return _run


def _environment_bool(name: str, default: bool) -> bool:
    value = _environ().get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")
