from __future__ import annotations

from io import BytesIO
import json
import sqlite3
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.chat import ChatService
from research_pulse.workbench.context_resolution import ContextResolver
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.paper_access import PaperAccessService
from research_pulse.workbench.paper_ingest import PdfIngestStore
from research_pulse.workbench.preparation import PaperPreparationService, PreparationQueue
from research_pulse.workbench.prompt_budget import PromptBudgeter
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class _Knowledge:
    def recent(self, *, limit: int): return ()
    def get_current(self, knowledge_id: str): return None


class _Parser:
    def __init__(self): self.calls = 0
    def parse_pdf(self, pdf_path, *, source_id, source_url, output_dir):
        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        block = {"block_id": "block:shared", "text": "shared evidence", "section_path": ["Method"], "page_start": 1}
        (output_dir / "blocks.jsonl").write_text(json.dumps(block) + "\n", encoding="utf-8")
        (output_dir / "manifest.json").write_text(json.dumps({"complete": True}), encoding="utf-8")
        return output_dir


class _Model:
    def complete(self, prompt: str) -> str: return "answer"


class _Codec:
    def count(self, text: str) -> int: return len(text.split())
    def truncate(self, text: str, max_tokens: int) -> str: return " ".join(text.split()[:max_tokens])


class _Runtime:
    def cancel(self, run_id: str) -> None: pass


class WorkbenchTwoSessionApiTests(TestCase):
    def test_shared_paper_prepares_once_while_session_state_is_isolated(self) -> None:
        root = Path("tests/.workbench-two-session")
        layer = root / "layer-c"
        material = root / "material"
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            repository = SQLiteWorkbenchRepository(connection)
            session_ids = iter(("session-a", "session-b"))
            sessions = SessionService(repository, session_id_factory=lambda: next(session_ids))
            parser = _Parser()
            queue = PreparationQueue(parser, repository)
            preparation = PaperPreparationService(repository, queue, material_cache_root=material)
            store = PdfIngestStore(layer)
            content = b"%PDF-1.7\nshared\n%%EOF\n"
            first = store.ingest(BytesIO(content))
            second = store.ingest(BytesIO(content))
            paper = preparation.register_pdf(first)
            preparation.register_pdf(second)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(queue.pending_count, 1)
            queue.run_next()
            self.assertEqual(parser.calls, 1)

            access = PaperAccessService(repository, sessions, queue, layer_c_root=layer, material_cache_root=material)
            message_ids = iter(f"message-{index}" for index in range(8))
            chat = ChatService(repository, sessions, access, ContextResolver(max_blocks=20), PromptBudgeter(_Codec(), context_window_tokens=200, reserved_output_tokens=30), _Model(), message_id_factory=lambda: next(message_ids), clock=lambda: sessions.clock())
            run_ids = iter(("run-a", "run-b"))
            exploration = ExplorationService(repository, _Runtime(), run_id_factory=lambda: next(run_ids))
            client = TestClient(create_app(knowledge_reader=_Knowledge(), workbench_session_service=sessions, workbench_paper_service=access, workbench_chat_service=chat, workbench_exploration_service=exploration))

            a = client.post("/api/workbench/sessions", json={}).json()["session_id"]
            b = client.post("/api/workbench/sessions", json={}).json()["session_id"]
            self.assertEqual(client.post(f"/api/workbench/sessions/{a}/papers/{paper.paper_id}").status_code, 200)
            self.assertEqual(client.post(f"/api/workbench/sessions/{b}/papers/{paper.paper_id}").status_code, 200)
            client.patch(f"/api/workbench/sessions/{a}", json={"paper_panel_open": True, "active_paper_id": paper.paper_id})
            client.post(f"/api/workbench/sessions/{a}/messages", json={"query": "only a", "scope": "none"})
            client.post(f"/api/workbench/sessions/{a}/messages", json={"query": "explore a", "scope": "explore"})

            self.assertTrue(client.get(f"/api/workbench/sessions/{a}").json()["paper_panel_open"])
            self.assertFalse(client.get(f"/api/workbench/sessions/{b}").json()["paper_panel_open"])
            self.assertEqual(len(client.get(f"/api/workbench/sessions/{a}/messages").json()["items"]), 2)
            self.assertEqual(client.get(f"/api/workbench/sessions/{b}/messages").json()["items"], [])
            self.assertEqual(len(client.get(f"/api/workbench/sessions/{a}/explorations").json()["items"]), 1)
            self.assertEqual(client.get(f"/api/workbench/sessions/{b}/explorations").json()["items"], [])
            self.assertEqual(repository.connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
        finally:
            connection.close()
            if root.exists():
                for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                    if path.is_file(): path.unlink()
                    elif path.is_dir(): path.rmdir()
                root.rmdir()
