"""Note-only API entrypoint.

Serves the knowledge timeline and single-note detail straight from the schema-v1
file vault (``knowledge/papers``).  No DATABASE_URL, Postgres, RAG, LangGraph,
DeepSeek, or the legacy production chain is required.

Optionally wires the daily paper scheduler (see ``topics.daily_scheduler``)
into the app lifecycle.  Run uvicorn with ``--workers 1``: this process is the
single scheduler owner and must not be duplicated.

Backed endpoints:
  GET /api/knowledge            -> recently published note summaries
  GET /api/knowledge/{id}       -> one published note (markdown + provenance)

Unbacked features (chat, research-topics write paths, review) answer 503; the
frontend already degrades those responses gracefully.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from research_pulse.api.app import create_app
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.workbench.runtime import WorkbenchRuntime
from research_pulse.workbench.sessions import SessionService


def create_note_only_app(
    vault_root: Path | None = None,
    normalized_root: Path | None = None,
    *,
    enable_scheduler: bool = True,
    profile_path: Path | None = None,
    reader_mode: str = "pedagogical",
    workbench_database_path: Path | None = None,
):
    """Build a minimal FastAPI app over the note vault.

    ``vault_root`` defaults to ``<project_root>/knowledge`` and holds the note
    feed plus (with the scheduler enabled) the ``.topics/`` control state.
    ``normalized_root`` is retained as a compatibility parameter and now points
    to the MinerU material cache; it defaults to ``<project_root>/data/mineru``.
    ``profile_path`` defaults to ``<project_root>/scout/profile.yaml`` and, when
    a research topic is created from the UI, is kept in sync so the scout agent
    and the frontend see the same interests.
    """
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    if vault_root is None:
        vault_root = project_root / "knowledge"
    if normalized_root is None:
        normalized_root = project_root / "data" / "mineru"
    if profile_path is None:
        profile_path = project_root / "scout" / "profile.yaml"
    if workbench_database_path is None:
        workbench_database_path = project_root / "data" / "workbench" / "workbench.sqlite3"

    schedule = None
    lifespan = None
    scheduler_status = None
    topic_service = None
    on_topic_created: Callable[[str, str], None] | None = None
    if enable_scheduler:
        from research_pulse.topics.daily_scheduler import DailyPaperScheduler

        schedule = DailyPaperScheduler(
            vault_root=vault_root,
            normalized_root=normalized_root,
            reader_mode=reader_mode,
        )
        # The scheduler only starts inside the FastAPI lifespan (below), so
        # building the app object never spawns scheduling threads — that stays
        # tied to the server process actually serving requests.
        lifespan = schedule.lifespan()
        scheduler_status = schedule.status
        topic_service = schedule.service
        on_topic_created = _scout_profile_sync(str(profile_path))

    workbench_runtime = WorkbenchRuntime(workbench_database_path)
    lifespan = workbench_runtime.lifespan(lifespan)

    return create_app(
        knowledge_reader=FilesystemKnowledgeReader(vault_root),
        topic_service=topic_service,
        scheduler_status=scheduler_status,
        lifespan=lifespan,
        on_topic_created=on_topic_created,
        workbench_session_service=SessionService(workbench_runtime),
    )


def _scout_profile_sync(profile_path: str) -> Callable[[str, str], None]:
    """Return a ``(name, query) -> None`` hook that appends to scout interests.

    Each frontend-created research topic becomes (or updates) one scout interest
    with the topic name as label and the query as its keywords.  Existing
    entries are de-duplicated by label; ``dislikes`` are left untouched.
    """
    def _sync(name: str, query: str) -> None:
        _append_interest_to_profile(Path(profile_path), name, query)

    return _sync


def _append_interest_to_profile(path: Path, name: str, query: str) -> None:
    import yaml

    data = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    interests = [i for i in data.get("interests") or [] if isinstance(i, dict)]
    interests = [i for i in interests if str(i.get("topic", "")) != name]
    interests.append({"topic": name, "keywords": [query], "note": "由前端研究方向自动同步"})
    data["interests"] = interests
    data.setdefault("dislikes", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
