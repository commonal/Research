from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient

from research_pulse.api.app import create_app
from research_pulse.workbench.chat import ChatService
from research_pulse.workbench.context_resolution import ContextResolver
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.models import Paper, ParseStatus, PdfStatus
from research_pulse.workbench.paper_access import PaperAccessService
from research_pulse.workbench.preparation import PreparationQueue
from research_pulse.workbench.prompt_budget import PromptBudgeter
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class _EmptyKnowledgeReader:
    def recent(self, *, limit: int):
        return ()

    def get_current(self, knowledge_id: str):
        return None


class _WordCodec:
    def count(self, text: str) -> int:
        return len(text.split())

    def truncate(self, text: str, max_tokens: int) -> str:
        return " ".join(text.split()[:max_tokens])


class _ScriptedModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.fail = False
        self.answer = "草稿回答 [normalized:paper-1:text:method]"

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("api_key=secret C:/private")
        return self.answer


class _UnusedParser:
    def parse_pdf(self, pdf_path, *, source_id, source_url, output_dir):
        raise AssertionError("chat must not start parsing")


class _ExplorationRuntime:
    def cancel(self, run_id: str) -> None:
        pass


class WorkbenchChatApiTests(TestCase):
    def setUp(self) -> None:
        self.root = Path("tests/.workbench-chat")
        self.layer_c = self.root / "layer-c"
        self.material_cache = self.root / "mineru"
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)
        self.session_service = SessionService(
            self.repository,
            session_id_factory=lambda: "session-chat",
            clock=lambda: datetime(2026, 8, 31, 16, 0, tzinfo=UTC),
        )
        self.session_service.create_empty()
        queue = PreparationQueue(_UnusedParser(), self.repository)
        self.paper_access = PaperAccessService(
            self.repository,
            self.session_service,
            queue,
            layer_c_root=self.layer_c,
            material_cache_root=self.material_cache,
        )
        self.model = _ScriptedModel()
        ids = iter(("user-1", "assistant-1", "user-2", "assistant-2", "user-3", "assistant-3"))
        chat = ChatService(
            self.repository,
            self.session_service,
            self.paper_access,
            ContextResolver(max_blocks=20),
            PromptBudgeter(
                _WordCodec(), context_window_tokens=200, reserved_output_tokens=30
            ),
            self.model,
            message_id_factory=lambda: next(ids),
            clock=lambda: datetime(2026, 8, 31, 16, 0, tzinfo=UTC),
        )
        run_ids = iter(("explore-1", "explore-2", "explore-3", "explore-4"))
        self.exploration = ExplorationService(
            self.repository,
            _ExplorationRuntime(),
            run_id_factory=lambda: next(run_ids),
        )
        self.assistant_exploration = ExplorationService(
            self.repository,
            _ExplorationRuntime(),
            run_id_factory=lambda: "assistant-heavy-run",
        )
        self.client = TestClient(
            create_app(
                knowledge_reader=_EmptyKnowledgeReader(),
                workbench_session_service=self.session_service,
                workbench_chat_service=chat,
                workbench_exploration_service=self.exploration,
                workbench_assistant_exploration_service=self.assistant_exploration,
            )
        )

    def tearDown(self) -> None:
        self.connection.close()
        if self.root.exists():
            for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.root.rmdir()

    def _add_ready_paper(self) -> None:
        pdf = self.layer_c / "paper-1" / "source.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF-1.7\nchat\n%%EOF\n")
        material = self.material_cache / "paper-1" / "material"
        material.mkdir(parents=True)
        blocks = (
            {
                "block_id": "normalized:paper-1:text:method",
                "kind": "text",
                "text": "The method uses a causal adjustment.",
                "section_path": ["Method"],
                "page_start": 2,
                "sources": [{"parser": "mineru_api", "locator": "#/1"}],
                "alignment": "mineru_only",
                "parse_status": "available",
                "confidence": 0.8,
            },
            {
                "block_id": "normalized:paper-1:text:result",
                "kind": "text",
                "text": "The result improves recall.",
                "section_path": ["Results"],
                "page_start": 3,
                "sources": [{"parser": "mineru_api", "locator": "#/2"}],
                "alignment": "mineru_only",
                "parse_status": "available",
                "confidence": 0.8,
            },
        )
        (material / "blocks.jsonl").write_text(
            "".join(json.dumps(block) + "\n" for block in blocks), encoding="utf-8"
        )
        (material / "manifest.json").write_text(
            json.dumps({"complete": True, "block_count": 2}), encoding="utf-8"
        )
        self.repository.upsert_paper(
            Paper(
                paper_id="paper-1",
                source_identity="sha256:paper-1",
                pdf_path=str(pdf),
                pdf_status=PdfStatus.READY,
                parse_status=ParseStatus.READY,
                material_root=str(material),
            )
        )
        self.session_service.attach_paper("session-chat", "paper-1")

    def test_none_scope_calls_model_once_and_persists_no_paper_marker(self) -> None:
        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={"query": "先讨论问题", "scope": "none"},
        )
        history = self.client.get(
            "/api/workbench/sessions/session-chat/messages"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.model.prompts), 1)
        self.assertIn("当前没有论文上下文", self.model.prompts[0])
        self.assertFalse(response.json()["metadata"]["paper_context_used"])
        self.assertEqual(
            [item["generation_status"] for item in history.json()["items"]],
            ["completed", "completed"],
        )
        self.assertEqual(self.repository.list_exploration_runs("session-chat"), ())

    def test_explore_scope_always_creates_an_independent_run_without_calling_chat_model(self) -> None:
        first = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={"query": "比较两条方法路线", "scope": "explore"},
        )
        second = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={"query": "比较两条方法路线", "scope": "explore"},
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["kind"], "exploration_run")
        self.assertEqual(first.json()["run"]["run_id"], "explore-1")
        self.assertEqual(second.json()["run"]["run_id"], "explore-2")
        self.assertEqual(first.json()["run"]["attempt"], 1)
        self.assertEqual(second.json()["run"]["attempt"], 2)
        self.assertEqual(
            "research_exploration",
            first.json()["run"]["config_snapshot"]["resolved"]["retrieval_plan"],
        )
        direct = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={"query": "RLHF 是什么？", "scope": "explore"},
        )
        self.assertEqual(
            "direct",
            direct.json()["run"]["config_snapshot"]["resolved"]["retrieval_plan"],
        )
        web = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={"query": "查网页上的 RLHF 最新消息", "scope": "explore"},
        )
        self.assertEqual(
            "web_lookup",
            web.json()["run"]["config_snapshot"]["resolved"]["retrieval_plan"],
        )
        self.assertEqual(len(self.model.prompts), 0)
        self.assertEqual(
            self.client.get("/api/workbench/sessions/session-chat/messages").json()["items"],
            [],
        )

    def test_assistant_web_lookup_uses_lightweight_exploration_budget(self) -> None:
        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={
                "query": "查网页上的 2026 年最新 LLM 推荐系统研究进展",
                "scope": "explore",
                "profile": "assistant",
            },
        )

        self.assertEqual(response.status_code, 202)
        run = response.json()["run"]
        self.assertEqual(run["run_id"], "explore-1")
        self.assertEqual(run["config_snapshot"]["resolved"]["retrieval_plan"], "web_lookup")
        self.assertEqual(run["config_snapshot"]["profile"], "literature")
        self.assertEqual(run["config_snapshot"]["requested_profile"], "assistant")
        self.assertEqual(run["budgets"], {
            "model_rounds": 4,
            "tool_calls": 8,
            "block_reads": 0,
            "wall_seconds": 180,
            "input_tokens": 30000,
            "output_tokens": 6000,
        })

    def test_explicit_research_action_does_not_fall_through_to_chat(self) -> None:
        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={
                "query": "请围绕当前研究焦点继续深入研究：beta 敏感性",
                "scope": "none",
                "action": "research_run",
            },
        )

        self.assertEqual(response.status_code, 202)
        run = response.json()["run"]
        self.assertEqual(run["config_snapshot"]["profile"], "assistant")
        self.assertEqual(
            run["config_snapshot"]["resolved"]["retrieval_plan"],
            "research_exploration",
        )
        self.assertEqual(self.client.get("/api/workbench/sessions/session-chat/messages").json()["items"], [])

    def test_selection_scope_sends_only_exact_selected_block(self) -> None:
        self._add_ready_paper()

        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={
                "query": "方法是什么？",
                "scope": "selection",
                "paper_id": "paper-1",
                "block_id": "normalized:paper-1:text:method",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.model.prompts), 1)
        self.assertIn("normalized:paper-1:text:method", self.model.prompts[0])
        self.assertIn("causal adjustment", self.model.prompts[0])
        self.assertNotIn("improves recall", self.model.prompts[0])
        self.assertEqual(response.json()["metadata"]["scope"], "selection")
        self.assertEqual(
            response.json()["metadata"]["actual_block_ids"],
            ["normalized:paper-1:text:method"],
        )
        self.assertEqual(
            response.json()["metadata"]["context_chips"],
            [{
                "paper_id": "paper-1",
                "block_id": "normalized:paper-1:text:method",
                "section_path": ["Method"],
                "page": 2,
                "truncated": False,
            }],
        )

    def test_selection_scope_preserves_exact_text_and_multiple_block_ids(self) -> None:
        self._add_ready_paper()

        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={
                "query": "请解释选区",
                "scope": "selection",
                "paper_id": "paper-1",
                "block_ids": [
                    "normalized:paper-1:text:method",
                    "normalized:paper-1:text:result",
                ],
                "selected_text": "The method uses a causal adjustment. The result improves recall.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("用户精确选区", self.model.prompts[-1])
        self.assertIn("The method uses a causal adjustment. The result improves recall.", self.model.prompts[-1])
        metadata = response.json()["metadata"]
        self.assertEqual(metadata["selection_block_ids"], [
            "normalized:paper-1:text:method",
            "normalized:paper-1:text:result",
        ])
        self.assertEqual(metadata["selected_text"], "The method uses a causal adjustment. The result improves recall.")

    def test_model_failure_is_safe_and_refreshable(self) -> None:
        self.model.fail = True

        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={"query": "会失败", "scope": "none"},
        )
        history = self.client.get(
            "/api/workbench/sessions/session-chat/messages"
        ).json()["items"]

        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.model.prompts), 1)
        self.assertEqual(history[-1]["generation_status"], "failed")
        self.assertNotIn("secret", json.dumps(history, ensure_ascii=False))
        self.assertNotIn("C:/private", json.dumps(history, ensure_ascii=False))

    def test_citations_only_resolve_exact_blocks_sent_this_turn_and_detach(self) -> None:
        self._add_ready_paper()
        self.model.answer = (
            "方法证据 [normalized:paper-1:text:method]，"
            "缩写 [method]，未发送 [normalized:paper-1:text:result]，"
            "无效 [normalized:paper-1:text:missing]。"
        )

        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={
                "query": "方法是什么？",
                "scope": "selection",
                "paper_id": "paper-1",
                "block_id": "normalized:paper-1:text:method",
            },
        )

        self.assertEqual(response.status_code, 200)
        citations = response.json()["citations"]
        self.assertEqual(
            [(item["block_id"], item["status"]) for item in citations],
            [
                ("normalized:paper-1:text:method", "resolved"),
                ("normalized:paper-1:text:result", "unresolved"),
                ("normalized:paper-1:text:missing", "unresolved"),
            ],
        )
        self.assertEqual(citations[0]["locator"]["page"], 2)

        self.paper_access.remove("session-chat", "paper-1")
        refreshed = self.client.get(
            "/api/workbench/sessions/session-chat/messages"
        ).json()["items"][-1]

        self.assertEqual(refreshed["citations"][0]["status"], "detached")
        self.assertTrue(all(
            item["status"] == "unresolved" for item in refreshed["citations"][1:]
        ))

    def test_reference_numbers_are_not_workbench_citations(self) -> None:
        self.model.answer = "翻译结果见参考文献 [6]、[16]。"
        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={"query": "翻译当前选区", "scope": "none"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["citations"], [])

    def test_citations_accept_parentheses_and_backticks_from_model_output(self) -> None:
        self._add_ready_paper()
        self.model.answer = (
            "方法依据（`normalized:paper-1:text:method`、"
            "normalized:paper-1:text:result`）。"
        )

        response = self.client.post(
            "/api/workbench/sessions/session-chat/messages",
            json={
                "query": "方法是什么？",
                "scope": "selection",
                "paper_id": "paper-1",
                "block_id": "normalized:paper-1:text:method",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(item["block_id"], item["status"]) for item in response.json()["citations"]],
            [
                ("normalized:paper-1:text:method", "resolved"),
                ("normalized:paper-1:text:result", "unresolved"),
            ],
        )
