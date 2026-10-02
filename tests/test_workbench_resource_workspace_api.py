from __future__ import annotations

import sqlite3
from typing import Callable
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class _Knowledge:
    def recent(self, *, limit: int): return ()
    def get_current(self, knowledge_id: str): return None


def _build_client(
    *,
    workspace_cleanup: Callable[[str], None] | None = None,
    session_cleanup: Callable[[str, str | None], None] | None = None,
    session_ids: tuple[str, ...] = ("session-a",),
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repository = SQLiteWorkbenchRepository(connection)
    ids = iter(session_ids)
    sessions = SessionService(repository, session_id_factory=lambda: next(ids))
    client = TestClient(
        create_app(
            knowledge_reader=_Knowledge(),
            workbench_session_service=sessions,
            workbench_workspace_cleanup=workspace_cleanup,
            workbench_session_workspace_cleanup=session_cleanup,
        )
    )
    return client, connection


class ResourceWorkspaceApiTests(TestCase):
    def setUp(self) -> None:
        self.client, self.connection = _build_client(session_ids=("session-a", "session-b"))

    def tearDown(self) -> None:
        self.connection.close()

    def test_sessions_carry_workspace_title_and_rename_propagates(self) -> None:
        a = self.client.post("/api/workbench/sessions", json={"workspace_id": "ws-proj"}).json()
        b = self.client.post("/api/workbench/sessions", json={"workspace_id": "ws-proj"}).json()
        self.assertEqual(a["workspace_id"], "ws-proj")
        self.assertEqual(b["workspace_id"], "ws-proj")
        # default title from ensure_workspace
        self.assertEqual(a["workspace_title"], "研究项目")

        renamed = self.client.put("/api/workbench/workspace/ws-proj", json={"title": "位置偏差研究"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "位置偏差研究")

        # both sessions in the workspace now carry the updated title
        self.assertEqual(
            self.client.get("/api/workbench/sessions/session-a").json()["workspace_title"],
            "位置偏差研究",
        )
        self.assertEqual(
            self.client.get("/api/workbench/sessions/session-b").json()["workspace_title"],
            "位置偏差研究",
        )

    def test_delete_workspace_removes_its_sessions_and_then_404(self) -> None:
        self.client.post("/api/workbench/sessions", json={"workspace_id": "ws-proj"})
        self.client.post("/api/workbench/sessions", json={"workspace_id": "ws-proj"})
        # sessions exist
        self.assertEqual(self.client.get("/api/workbench/sessions/session-a").status_code, 200)

        deleted = self.client.delete("/api/workbench/workspace/ws-proj")
        self.assertEqual(deleted.status_code, 204)

        # the workspace row is gone, so renaming it 404s
        self.assertEqual(
            self.client.put("/api/workbench/workspace/ws-proj", json={"title": "x"}).status_code,
            404,
        )
        # its sessions are gone too
        self.assertEqual(self.client.get("/api/workbench/sessions/session-a").status_code, 404)
        self.assertEqual(len(self.client.get("/api/workbench/sessions").json()["items"]), 0)

    def test_rename_missing_workspace_404(self) -> None:
        self.assertEqual(
            self.client.put("/api/workbench/workspace/nope", json={"title": "x"}).status_code,
            404,
        )

    def test_delete_workspace_runs_cleanup_only_after_db_rows_are_gone(self) -> None:
        calls: list[str] = []
        client, connection = _build_client(
            workspace_cleanup=calls.append,
            session_ids=("session-a", "session-b"),
        )
        client.post("/api/workbench/sessions", json={"workspace_id": "ws-proj"})
        client.post("/api/workbench/sessions", json={"workspace_id": "ws-proj"})

        deleted = client.delete("/api/workbench/workspace/ws-proj")
        self.assertEqual(deleted.status_code, 204)
        # the filesystem cleanup fired exactly once, and only after the DB rows
        # (sessions + workspace) were already gone -> rmtree never orphans rows.
        self.assertEqual(calls, ["ws-proj"])
        self.assertEqual(client.get("/api/workbench/sessions/session-a").status_code, 404)
        self.assertEqual(len(client.get("/api/workbench/sessions").json()["items"]), 0)
        connection.close()

    def test_delete_missing_workspace_skips_cleanup(self) -> None:
        calls: list[str] = []
        client, connection = _build_client(workspace_cleanup=calls.append)
        self.assertEqual(client.delete("/api/workbench/workspace/nope").status_code, 404)
        self.assertEqual(calls, [])
        connection.close()

    def test_delete_session_passes_workspace_id_to_cleanup(self) -> None:
        calls: list[tuple[str, str | None]] = []
        client, connection = _build_client(
            session_cleanup=lambda session_id, workspace_id: calls.append((session_id, workspace_id)),
        )
        created = client.post("/api/workbench/sessions", json={}).json()

        self.assertEqual(client.delete(f"/api/workbench/sessions/{created['session_id']}").status_code, 204)
        # cleanup receives the workspace_id captured BEFORE the row was deleted,
        # so it can remove the session dir without a (now-missing) DB lookup.
        self.assertEqual(calls, [(created["session_id"], created["workspace_id"])])
        self.assertEqual(client.get(f"/api/workbench/sessions/{created['session_id']}").status_code, 404)
        connection.close()
