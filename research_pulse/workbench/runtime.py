"""Single-process lifespan owner for the workbench SQLite repository."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from research_pulse.workbench.sessions import ResearchSession, SessionLifecycle
from research_pulse.workbench.sqlite import (
    SQLiteWorkbenchRepository,
    open_workbench_database,
    recover_interrupted_work,
)


class WorkbenchRuntime:
    """Repository proxy whose connection exists only during the app lifespan."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        material_is_complete: Callable[[str], bool] = lambda _: False,
    ) -> None:
        self.database_path = Path(database_path)
        self.material_is_complete = material_is_complete
        self.connection = None
        self.repository: SQLiteWorkbenchRepository | None = None

    def start(self) -> None:
        if self.connection is not None:
            raise RuntimeError("workbench runtime already started; use one Uvicorn worker")
        self.connection = open_workbench_database(self.database_path)
        self.repository = SQLiteWorkbenchRepository(self.connection)
        recover_interrupted_work(
            self.connection,
            material_is_complete=self.material_is_complete,
        )

    def stop(self) -> None:
        if self.connection is not None:
            self.connection.close()
        self.connection = None
        self.repository = None

    def lifespan(self, upstream: Any = None):
        @asynccontextmanager
        async def combined(app):
            self.start()
            try:
                if upstream is None:
                    yield
                else:
                    async with upstream(app):
                        yield
            finally:
                self.stop()

        return combined

    def _repo(self) -> SQLiteWorkbenchRepository:
        if self.repository is None:
            raise RuntimeError("workbench runtime is outside the FastAPI lifespan")
        return self.repository

    def create(self, session: ResearchSession) -> None:
        self._repo().create(session)

    def get(self, session_id: str) -> ResearchSession | None:
        return self._repo().get(session_id)

    def list(self, lifecycle: SessionLifecycle | None = None) -> tuple[ResearchSession, ...]:
        return self._repo().list(lifecycle)

    def save(self, session: ResearchSession) -> None:
        self._repo().save(session)

    def delete(self, session_id: str) -> None:
        self._repo().delete(session_id)

    def link_paper(self, session_id: str, paper_id: str) -> None:
        self._repo().link_paper(session_id, paper_id)

    def list_paper_ids(self, session_id: str) -> tuple[str, ...]:
        return self._repo().list_paper_ids(session_id)
