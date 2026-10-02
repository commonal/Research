from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from unittest import TestCase

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from research_pulse.workbench.agent_runtime import AgentRunInput, RunBudgets
from research_pulse.workbench.deepagents_v0 import (
    ALLOWED_TOOLS,
    ALLOWED_TOOLS_ASSISTANT,
    DeepAgentsV0Runtime,
)
from research_pulse.workbench.research_tools import ReadOnlyResearchTools
from research_pulse.workbench.workspace import WorkspaceService
from research_pulse.workbench.workspace_json import WorkspaceJsonStore
from research_pulse.workbench.workspace_pipeline import build_workspace_pipeline
from research_pulse.workbench.workspace_tools import WorkspaceResearchTools


class _FakeImporter:
    """Satisfies the SupportingPaperImporter protocol without touching the network."""

    def import_paper(self, candidate):
        from research_pulse.workbench.supporting_paper import SupportingPaperImportResult

        return SupportingPaperImportResult(
            source_id="paper-1",
            sample_block_id="normalized:paper-1:text:abs",
            source_identity="arxiv:fake",
            source_url="",
            pdf_status="ready",
            parse_status="ready",
            safe_error=None,
        )


MANIFEST = "experiments/workbench-harness-v0/manifest.json"
BLOCK_ID = "normalized:2608.21867:text:a2f7b3c37549"


def tools_factory(run_id, status_reader):
    return ReadOnlyResearchTools.from_fixture(MANIFEST, run_id=run_id, status_reader=status_reader)


class _FakeWebProvider:
    def search(self, query, *, max_results=5):
        return [{"title": "Official page", "url": "https://example.com/rlhf", "snippet": "bounded result"}]


def web_tools_factory(run_id, status_reader):
    return ReadOnlyResearchTools.from_fixture(
        MANIFEST,
        run_id=run_id,
        status_reader=status_reader,
        # The runtime provider marker and the facade provider are intentionally
        # separate seams, matching production dependency injection.
        web_search_provider=_FakeWebProvider(),
    )


class ScriptedToolModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    script: Iterator[AIMessage]
    bound_names: list[str] = Field(default_factory=list)
    before_response: object | None = None
    received_messages: list[BaseMessage] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "scripted-tool-model"

    def bind_tools(self, tools: Sequence[object], **kwargs):
        self.bound_names[:] = [tool.name for tool in tools]
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
        self.received_messages.extend(messages)
        if callable(self.before_response):
            self.before_response()
            self.before_response = None
        return ChatResult(generations=[ChatGeneration(message=next(self.script))])


class BrokenToolModel(ScriptedToolModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("api_key=secret private prompt")


class WorkbenchDeepAgentsV0Tests(TestCase):
    def input(self, run_id="run-v0", budgets=None):
        return AgentRunInput(
            run_id=run_id,
            question="Compare memory governance and reranking.",
            allowed_tools=ALLOWED_TOOLS,
            budgets=budgets or RunBudgets(8, 16, 24, 300, 40000, 8000),
        )

    @staticmethod
    def run_with_events(runtime, run_input):
        events = []
        result = runtime.start(replace(run_input, on_event=events.append))
        return result, events

    def test_effective_inventory_is_exact_and_static_builtin_tools_are_not_model_visible(self):
        facade = tools_factory("inventory", lambda _: {})
        actual = DeepAgentsV0Runtime._preflight_effective_tools(facade, ALLOWED_TOOLS)
        self.assertEqual(actual, ALLOWED_TOOLS)
        self.assertTrue({"write_file", "edit_file", "execute", "task"}.isdisjoint(actual))

    def test_system_prompt_does_not_own_tool_serialization_safety(self):
        model = ScriptedToolModel(script=iter([
            AIMessage(content="探索草稿/待验证：无工具调用。"),
        ]))
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: model,
            tools_factory=tools_factory,
        )

        result = runtime.start(self.input("prompt-safety"))

        prompt_text = "\n".join(str(message.content) for message in model.received_messages)
        self.assertEqual("completed", result.stop_reason)
        self.assertNotIn("avoid parallel tool calls", prompt_text.lower())

    def test_research_run_records_a_round_receipt_even_without_agent_plan_write(self):
        """The runtime keeps the multi-round timeline durable when the model only answers."""
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store,
                workspace_id_factory=lambda: "ws-iteration-runtime",
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )
            service.create(research_question="研究 DPO 对齐", anchor_paper_id="paper-x")
            pipeline = build_workspace_pipeline(store, workspace_root=root)

            def workspace_tools_factory(run_id):
                return WorkspaceResearchTools(
                    service,
                    "ws-iteration-runtime",
                    gate=pipeline.gate,
                    risk_classifier=pipeline.risk_classifier,
                    commit_service=pipeline.commit_service,
                    hitl_service=pipeline.hitl_service,
                    run_id=run_id,
                )

            model = ScriptedToolModel(script=iter([AIMessage(content="本轮完成了一个有界研究回答。")]))
            runtime = DeepAgentsV0Runtime(
                model_factory=lambda guard: model,
                tools_factory=tools_factory,
                assistant=True,
                workspace_resolver=lambda _run_id: str(Path(tmp) / "session"),
                workspace_tools_factory=workspace_tools_factory,
            )
            result = runtime.start(AgentRunInput(
                run_id="iteration-runtime",
                question="请完成本轮研究。",
                allowed_tools=ALLOWED_TOOLS_ASSISTANT,
                budgets=RunBudgets(4, 8, 16, 300, 40000, 8000),
                resolved={
                    "retrieval_plan": "research_exploration",
                    "logical_run_id": "logical-exploration-1",
                },
            ))

            self.assertEqual(result.stop_reason, "completed")
            iterations = store.get("ws-iteration-runtime").research_plan.iterations
            self.assertEqual(len(iterations), 1)
            self.assertEqual(iterations[0].run_id, "logical-exploration-1")
            self.assertEqual(iterations[0].status.value, "completed")

            # A budget continuation has a different physical Attempt id but
            # must remain the same logical research round.
            model2 = ScriptedToolModel(script=iter([AIMessage(content="恢复后继续完成本轮研究。")]))
            runtime2 = DeepAgentsV0Runtime(
                model_factory=lambda guard: model2,
                tools_factory=tools_factory,
                assistant=True,
                workspace_resolver=lambda _run_id: str(Path(tmp) / "session"),
                workspace_tools_factory=workspace_tools_factory,
            )
            result2 = runtime2.start(AgentRunInput(
                run_id="iteration-runtime-attempt-2",
                question="请继续完成本轮研究。",
                allowed_tools=ALLOWED_TOOLS_ASSISTANT,
                budgets=RunBudgets(4, 8, 16, 300, 40000, 8000),
                resolved={
                    "retrieval_plan": "research_exploration",
                    "logical_run_id": "logical-exploration-1",
                },
            ))
            self.assertEqual(result2.stop_reason, "completed")
            iterations = store.get("ws-iteration-runtime").research_plan.iterations
            self.assertEqual(len(iterations), 1)

    def test_direct_retrieval_plan_does_not_bind_search_tools(self):
        model = ScriptedToolModel(script=iter([
            AIMessage(content="这是一个不需要检索的常识性回答。"),
        ]))
        runtime = DeepAgentsV0Runtime(model_factory=lambda guard: model, tools_factory=tools_factory)
        run_input = replace(
            self.input("direct-answer"),
            resolved={"retrieval_plan": "direct", "mode": "direct"},
        )

        result, _events = self.run_with_events(runtime, run_input)

        self.assertEqual("completed", result.stop_reason)
        self.assertNotIn("search_sources", model.bound_names)
        self.assertNotIn("search_arxiv", model.bound_names)

    def test_paper_evidence_plan_reads_anchor_without_external_search(self):
        model = ScriptedToolModel(script=iter([
            AIMessage(content="基于当前论文的回答。"),
        ]))
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: model,
            tools_factory=tools_factory,
            assistant=True,
        )
        run_input = replace(
            self.input("paper-evidence"),
            allowed_tools=ALLOWED_TOOLS_ASSISTANT,
            resolved={
                "mode": "paper_in_research_context",
                "retrieval_plan": "paper_evidence",
            },
        )

        result, _events = self.run_with_events(runtime, run_input)

        self.assertEqual("completed", result.stop_reason)
        self.assertNotIn("search_sources", model.bound_names)
        self.assertNotIn("search_arxiv", model.bound_names)

    def test_web_lookup_plan_binds_only_web_search_and_read_tools(self):
        model = ScriptedToolModel(script=iter([
            AIMessage(content="网页结果见 https://example.com/rlhf。"),
        ]))
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: model,
            tools_factory=web_tools_factory,
            web_search_provider=object(),
        )
        run_input = replace(
            self.input("web-lookup"),
            allowed_tools=(*ALLOWED_TOOLS, "search_web"),
            resolved={"mode": "research_synthesis", "retrieval_plan": "web_lookup"},
        )

        result, _events = self.run_with_events(runtime, run_input)

        self.assertEqual("completed", result.stop_reason)
        self.assertIn("search_web", model.bound_names)
        self.assertNotIn("search_sources", model.bound_names)
        self.assertNotIn("search_arxiv", model.bound_names)
        self.assertNotIn("read_paper_metadata", model.bound_names)
        self.assertNotIn("read_managed_blocks", model.bound_names)
        self.assertNotIn("read_run_status", model.bound_names)

    def test_web_lookup_without_provider_fails_with_explainable_event(self):
        model = ScriptedToolModel(script=iter([AIMessage(content="不会被调用")]))
        runtime = DeepAgentsV0Runtime(model_factory=lambda guard: model, tools_factory=tools_factory)
        run_input = replace(
            self.input("web-unavailable"),
            resolved={"mode": "research_synthesis", "retrieval_plan": "web_lookup"},
        )
        result, events = self.run_with_events(runtime, run_input)
        self.assertEqual("failed", result.stop_reason)
        self.assertTrue(any("网页检索未配置" in event.summary for event in events))

    def test_web_lookup_stops_after_consecutive_empty_results(self):
        class _EmptyWeb:
            def __init__(self):
                self.calls = []

            def search(self, query, *, max_results=5):
                self.calls.append(query)
                return []

        provider = _EmptyWeb()
        model = ScriptedToolModel(script=iter([
            AIMessage(content="", tool_calls=[{
                "name": "search_web", "args": {"query": "LLM recommender 2026"},
                "id": "call-web-empty-1", "type": "tool_call",
            }]),
            AIMessage(content="", tool_calls=[{
                "name": "search_web", "args": {"query": "latest LLM recommendation progress"},
                "id": "call-web-empty-2", "type": "tool_call",
            }]),
            AIMessage(content="", tool_calls=[{
                "name": "search_web", "args": {"query": "generative recommendation 2026"},
                "id": "call-web-empty-3", "type": "tool_call",
            }]),
            AIMessage(content="网页检索没有返回可核验结果。"),
        ]))

        def empty_web_tools_factory(run_id, status_reader):
            return ReadOnlyResearchTools.from_fixture(
                MANIFEST,
                run_id=run_id,
                status_reader=status_reader,
                web_search_provider=provider,
            )

        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: model,
            tools_factory=empty_web_tools_factory,
            web_search_provider=object(),
        )
        run_input = replace(
            self.input("web-empty-results"),
            allowed_tools=(*ALLOWED_TOOLS, "search_web"),
            resolved={"mode": "research_synthesis", "retrieval_plan": "web_lookup"},
        )

        result, events = self.run_with_events(runtime, run_input)

        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(provider.calls, ["LLM recommender 2026", "latest LLM recommendation progress"])
        self.assertTrue(any("连续空结果" in event.summary for event in events))

    def test_real_deepagents_loop_reads_block_emits_events_and_closes_citation(self):
        model = ScriptedToolModel(script=iter([
            AIMessage(content="", tool_calls=[{
                "name": "read_managed_blocks",
                "args": {"source_id": "2608.21867", "block_ids": [BLOCK_ID]},
                "id": "call-read-1",
                "type": "tool_call",
            }]),
            AIMessage(content=f"探索草稿/待验证：结论 [{BLOCK_ID}]"),
        ]))
        runtime = DeepAgentsV0Runtime(model_factory=lambda guard: model, tools_factory=tools_factory)
        result, events = self.run_with_events(runtime, self.input())
        read_ids = {e.stable_ids.get("block_id") for e in events if e.event_type == "context_read"}
        self.assertEqual(result.stop_reason, "completed")
        self.assertIn(BLOCK_ID, read_ids)
        self.assertIn(f"[{BLOCK_ID}]", result.final_draft)
        self.assertEqual(model.bound_names, list(ALLOWED_TOOLS))

    def test_large_block_request_is_capped_to_remaining_budget_and_can_synthesize(self):
        facade = tools_factory("bounded-read", lambda _: {})
        block_ids = [
            str(item["block_id"])
            for item in facade.read_paper_metadata("2608.21867")["block_index"][:20]
        ]
        model = ScriptedToolModel(script=iter([
            AIMessage(content="", tool_calls=[{
                "name": "read_managed_blocks",
                "args": {"source_id": "2608.21867", "block_ids": block_ids},
                "id": "call-read-over-budget",
                "type": "tool_call",
            }]),
            AIMessage(content="基于已读取证据生成总结。"),
        ]))
        runtime = DeepAgentsV0Runtime(model_factory=lambda guard: model, tools_factory=tools_factory)
        result, events = self.run_with_events(
            runtime,
            replace(self.input("bounded-read"), budgets=RunBudgets(8, 16, 10, 300, 40000, 8000)),
        )

        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(
            len([event for event in events if event.event_type == "context_read"]),
            10,
        )
        self.assertTrue(any("按剩余预算读取 10/20" in event.summary for event in events))
        self.assertNotIn("budget_exhausted", [event.summary for event in events])

    def test_budget_recovery_switches_to_evidence_synthesis_surface(self):
        model = ScriptedToolModel(script=iter([
            AIMessage(content="基于已读取论文证据生成的有限结论。"),
        ]))
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: model,
            tools_factory=tools_factory,
            assistant=True,
        )
        run_input = replace(
            self.input("synthesis-recovery"),
            allowed_tools=ALLOWED_TOOLS_ASSISTANT,
            resolved={
                "recovery_strategy": "synthesize_from_persisted_evidence",
                "evidence": [{
                    "stable_id": BLOCK_ID,
                    "source_id": "2608.21867",
                    "read": True,
                    "resolvable": True,
                }],
                "search_candidates": [{
                    "source_id": "2401.00001",
                    "title": "Preference Optimization Survey",
                    "abstract_preview": "A bounded candidate preview.",
                    "relevance": 0.91,
                }],
            },
        )

        result, _events = self.run_with_events(runtime, run_input)

        self.assertEqual("completed", result.stop_reason)
        self.assertNotIn("search_sources", model.bound_names)
        self.assertNotIn("search_arxiv", model.bound_names)
        for workspace_tool in (
            "read_workspace_state",
            "update_subquestions",
            "add_evidence",
            "update_research_map",
            "update_research_plan",
            "import_supporting_paper",
        ):
            self.assertNotIn(workspace_tool, model.bound_names)
        prompt_text = "\n".join(str(message.content) for message in model.received_messages)
        self.assertIn(BLOCK_ID, prompt_text)
        self.assertIn("Do not search", prompt_text)
        self.assertIn("Preference Optimization Survey", prompt_text)
        self.assertIn("未读取", prompt_text)

    def test_budget_exhaustion_fails_before_provider_and_retry_can_complete(self):
        tiny = RunBudgets(1, 1, 1, 300, 1, 1)
        first_runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: ScriptedToolModel(script=iter([AIMessage(content="unused")])),
            tools_factory=tools_factory,
        )
        first = first_runtime.start(self.input("attempt-1", tiny))
        second_runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: ScriptedToolModel(script=iter([AIMessage(content="探索草稿/待验证")])),
            tools_factory=tools_factory,
        )
        second = second_runtime.start(self.input("attempt-2"))
        self.assertEqual(first.stop_reason, "budget_exhausted")
        self.assertEqual(second.stop_reason, "completed")

    def test_invalid_tool_arguments_are_recoverable(self):
        model = ScriptedToolModel(script=iter([
            AIMessage(content="", tool_calls=[{
                "name": "read_run_status",
                "args": {},
                "id": "call-status-invalid",
                "type": "tool_call",
            }]),
            AIMessage(content="", tool_calls=[{
                "name": "read_run_status",
                "args": {"run_id": "invalid-tool-args"},
                "id": "call-status-corrected",
                "type": "tool_call",
            }]),
            AIMessage(content="探索草稿/待验证"),
        ]))
        runtime = DeepAgentsV0Runtime(model_factory=lambda guard: model, tools_factory=tools_factory)

        result, events = self.run_with_events(runtime, self.input("invalid-tool-args"))

        self.assertEqual(result.stop_reason, "completed")
        self.assertIn("final_draft", [event.event_type for event in events])
        completed = [
            event for event in events
            if event.event_type == "tool_completed"
        ]
        self.assertEqual(1, len(completed))

    def test_failed_search_can_switch_tool_and_finish_with_an_honest_boundary(self):
        calls: list[str] = []

        class SearchFailureFacade:
            def __init__(self):
                self.inner = tools_factory("fallback", lambda _run_id: {})
                self.capability_names = self.inner.capability_names

            def invoke(self, name, args):
                calls.append(name)
                if name == "search_sources":
                    raise ConnectionError("private upstream address")
                return self.inner.invoke(name, args)

        model = ScriptedToolModel(script=iter([
            AIMessage(content="", tool_calls=[{
                "name": "search_sources", "args": {"query": "memory"},
                "id": "call-search-failed", "type": "tool_call",
            }]),
            AIMessage(content="", tool_calls=[{
                "name": "read_paper_metadata", "args": {"source_id": "2608.21867"},
                "id": "call-metadata-fallback", "type": "tool_call",
            }]),
            AIMessage(content="探索草稿/待验证：补充搜索失败，以下仅基于已有受管材料。"),
        ]))
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: model,
            tools_factory=lambda _run_id, _status: SearchFailureFacade(),
        )

        result, events = self.run_with_events(runtime, self.input("fallback"))

        self.assertEqual("completed", result.stop_reason)
        self.assertEqual(["search_sources", "read_paper_metadata"], calls)
        self.assertIn("补充搜索失败", result.final_draft or "")
        self.assertNotIn("private upstream", repr(events))

    def test_external_failure_is_sanitized(self):
        runtime = DeepAgentsV0Runtime(
            model_factory=lambda guard: BrokenToolModel(script=iter([])), tools_factory=tools_factory
        )
        result, events = self.run_with_events(runtime, self.input("failure"))
        self.assertEqual(result.stop_reason, "failed")
        self.assertNotIn("secret", repr(events))
        self.assertNotIn("private prompt", repr(events).lower())

        retry = DeepAgentsV0Runtime(
            model_factory=lambda guard: ScriptedToolModel(script=iter([AIMessage(content="探索草稿/待验证")])),
            tools_factory=tools_factory,
        ).start(self.input("failure-retry"))
        self.assertEqual(retry.stop_reason, "completed")

    def test_on_event_receives_events_before_run_completes(self):
        observed: list[str] = []

        def before_final_response():
            # Runs while the loop is still in-flight: the live sink must
            # already have seen the run_started event produced so far,
            # before the runtime returned anything.
            observed.append("mid:" + ",".join(e.event_type for e in live_events))

        live_events: list = []
        model = ScriptedToolModel(script=iter([
            AIMessage(content="", tool_calls=[{
                "name": "read_managed_blocks",
                "args": {"source_id": "2608.21867", "block_ids": [BLOCK_ID]},
                "id": "call-live-1",
                "type": "tool_call",
            }]),
            AIMessage(content=f"探索草稿/待验证：结论 [{BLOCK_ID}]"),
        ]))
        model.before_response = before_final_response
        runtime = DeepAgentsV0Runtime(model_factory=lambda guard: model, tools_factory=tools_factory)
        result = runtime.start(self.input("live-events", ), on_event=live_events.append) if False else runtime.start(
            AgentRunInput(
                run_id="live-events",
                question="Compare memory governance and reranking.",
                allowed_tools=ALLOWED_TOOLS,
                budgets=RunBudgets(8, 16, 24, 300, 40000, 8000),
                on_event=live_events.append,
            )
        )
        self.assertEqual(result.stop_reason, "completed")
        # Live sink saw events while the run was still executing (before any
        # result was returned), and ends with the complete sequence.
        self.assertTrue(observed and observed[0].startswith("mid:run_started"))
        self.assertTrue(all(not hasattr(event, "sequence_no") for event in live_events))
        self.assertEqual(live_events[-1].event_type, "final_draft")
        self.assertIn("context_read", [e.event_type for e in live_events])

    def test_assistant_profile_exposes_file_tools_and_writes_into_session_workspace(self):
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            workbench_root = Path(tmp) / "workbench"
            workspaces = {}
            workspace_root = workbench_root / "workspaces"

            def assistant_tools(run_id, status_reader):
                return tools_factory(run_id, status_reader)

            def workspace_tools_factory(run_id):
                # Bind a real workspace so the controlled write tools appear.
                store = WorkspaceJsonStore(workspace_root)
                service = WorkspaceService(
                    store,
                    workspace_id_factory=lambda: "ws-1",
                    clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
                )
                service.create(research_question="如何降低位置偏差？", anchor_paper_id="paper-1")
                pipeline = build_workspace_pipeline(
                    store, workspace_root=workspace_root,
                    clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
                )
                return WorkspaceResearchTools(
                    service, "ws-1",
                    gate=pipeline.gate,
                    risk_classifier=pipeline.risk_classifier,
                    commit_service=pipeline.commit_service,
                    hitl_service=pipeline.hitl_service,
                    run_id=run_id,
                    importer=_FakeImporter(),
                )

            model = ScriptedToolModel(script=iter([
                AIMessage(content="", tool_calls=[{
                    "name": "write_file",
                    "args": {"file_path": "notes/idea.md", "content": "# 草稿"},
                    "id": "call-write-1",
                    "type": "tool_call",
                }]),
                AIMessage(content="", tool_calls=[{
                    "name": "update_subquestions",
                    "args": {"operation": "add", "question_id": "Q-001", "text": "是否有偏置？"},
                    "id": "call-subq-1",
                    "type": "tool_call",
                }]),
                AIMessage(content="探索草稿/待验证：已写入工作区"),
            ]))
            runtime = DeepAgentsV0Runtime(
                model_factory=lambda guard: model,
                tools_factory=assistant_tools,
                assistant=True,
                workspace_resolver=lambda run_id: workspaces.get(run_id),
                workspace_tools_factory=workspace_tools_factory,
            )
            from research_pulse.workbench.session_workspace import SessionWorkspaceManager
            manager = SessionWorkspaceManager(tmp)
            workspaces["ws-run"] = manager.resolve("session-ws")
            live_events = []
            result = runtime.start(AgentRunInput(
                run_id="ws-run",
                question="设计一个实验",
                allowed_tools=ALLOWED_TOOLS_ASSISTANT,
                budgets=RunBudgets(8, 16, 24, 300, 40000, 8000),
                on_event=live_events.append,
            ))
            self.assertEqual(result.stop_reason, "completed")
            written = Path(manager.resolve("session-ws")) / "notes" / "idea.md"
            self.assertTrue(written.exists())
            self.assertIn("# 草稿", written.read_text(encoding="utf-8"))
            # The controlled workspace write tool ran and persisted canonical state.
            state = WorkspaceJsonStore(workspace_root).list_subquestions("ws-1")
            self.assertEqual(len(state), 1)
            self.assertEqual(state[0].question_id, "Q-001")
            file_events = [e for e in live_events if e.event_type == "file_written"]
            self.assertTrue(file_events)
            # Backend normalizes to a virtual absolute path; it must stay
            # workspace-relative (no host path leakage).
            self.assertEqual(file_events[0].stable_ids["file_path"].lstrip("/"), "notes/idea.md")
            self.assertNotIn(str(Path(tmp)), repr(live_events))

    def test_parallel_workspace_writes_are_retried_after_references_commit(self):
        """A model may emit evidence/map/question writes in one parallel batch.

        The gate must not turn the arrival order into data loss: map/evidence
        writes that temporarily reference a not-yet-committed question are
        retried after the dependent write succeeds, and the initial run pauses
        for the direction decision once the graph is complete.
        """
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store,
                workspace_id_factory=lambda: "ws-parallel",
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )
            service.create(research_question="研究记忆重排序", anchor_paper_id="2608.21867")
            pipeline = build_workspace_pipeline(
                store, workspace_root=root,
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )

            def workspace_tools_factory(run_id):
                return WorkspaceResearchTools(
                    service, "ws-parallel",
                    gate=pipeline.gate,
                    risk_classifier=pipeline.risk_classifier,
                    commit_service=pipeline.commit_service,
                    hitl_service=pipeline.hitl_service,
                    run_id=run_id,
                )

            model = ScriptedToolModel(script=iter([
                AIMessage(content="", tool_calls=[
                    {
                        "name": "update_research_map",
                        "args": {
                            "node_id": "node-1",
                            "label": "记忆变化下的低秩补全",
                            "related_question_ids": ["q-1"],
                            "evidence_ids": ["ev-1"],
                        },
                        "id": "call-map-first",
                        "type": "tool_call",
                    },
                    {
                        "name": "add_evidence",
                        "args": {
                            "evidence_id": "ev-1",
                            "source_id": "2608.21867",
                            "block_ids": [BLOCK_ID],
                            "supports_question_ids": ["q-1"],
                            "claim": "低秩结构是关键前提。",
                        },
                        "id": "call-evidence-second",
                        "type": "tool_call",
                    },
                    {
                        "name": "update_subquestions",
                        "args": {
                            "operation": "add",
                            "question_id": "q-1",
                            "text": "记忆发生变化时低秩补全是否仍然有效？",
                            "researchability": "candidate",
                        },
                        "id": "call-question-third",
                        "type": "tool_call",
                    },
                ]),
                AIMessage(content="初步研究结果已形成。"),
            ]))
            runtime = DeepAgentsV0Runtime(
                model_factory=lambda guard: model,
                tools_factory=tools_factory,
                assistant=True,
                workspace_resolver=lambda _run_id: str(Path(tmp) / "session"),
                workspace_tools_factory=workspace_tools_factory,
            )
            events: list = []
            result = runtime.start(AgentRunInput(
                run_id="parallel-writes",
                question="请从论文边界提出一个可验证问题。",
                allowed_tools=ALLOWED_TOOLS_ASSISTANT,
                budgets=RunBudgets(8, 16, 24, 300, 40000, 8000),
                on_event=events.append,
            ))

            self.assertEqual("awaiting_decision", result.stop_reason)
            self.assertEqual(1, len(store.list_research_map("ws-parallel")))
            self.assertEqual("node-1", store.list_research_map("ws-parallel")[0].node_id)
            self.assertEqual(1, len(store.list_evidence("ws-parallel")))
            self.assertEqual(1, len(store.list_subquestions("ws-parallel")))
            self.assertTrue(any(
                "延迟" in event.summary or "已保留已知关联" in event.summary
                for event in events
            ))

    def test_experimental_mode_does_not_hit_hidden_workspace_write_cap(self):
        """The test-mode upper-bound run must not stop at the production write cap."""
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store,
                workspace_id_factory=lambda: "ws-write-cap",
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )
            service.create(research_question="研究写入上限", anchor_paper_id="2608.21867")
            pipeline = build_workspace_pipeline(
                store, workspace_root=root,
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )

            def workspace_tools_factory(run_id):
                return WorkspaceResearchTools(
                    service, "ws-write-cap",
                    gate=pipeline.gate,
                    risk_classifier=pipeline.risk_classifier,
                    commit_service=pipeline.commit_service,
                    hitl_service=pipeline.hitl_service,
                    run_id=run_id,
                )

            writes = [
                {
                    "name": "update_subquestions",
                    "args": {
                        "operation": "add",
                        "question_id": f"q-cap-{index}",
                        "text": f"第 {index} 个可验证问题？",
                        "researchability": "candidate",
                    },
                    "id": f"call-cap-{index}",
                    "type": "tool_call",
                }
                for index in range(21)
            ]
            model = ScriptedToolModel(script=iter([
                AIMessage(content="", tool_calls=writes),
                AIMessage(content="阶段结果已形成。"),
            ]))
            runtime = DeepAgentsV0Runtime(
                model_factory=lambda guard: model,
                tools_factory=tools_factory,
                assistant=True,
                workspace_resolver=lambda _run_id: str(Path(tmp) / "session"),
                workspace_tools_factory=workspace_tools_factory,
            )
            events: list = []
            result = runtime.start(AgentRunInput(
                run_id="write-cap",
                question="请记录多个候选问题。",
                allowed_tools=ALLOWED_TOOLS_ASSISTANT,
                budgets=RunBudgets(8, 64, 0, 300, 40000, 8000, experimental_unbounded=True),
                resolved={"retrieval_plan": "research_exploration"},
                on_event=events.append,
            ))

            self.assertNotEqual("budget_exhausted", result.stop_reason)
            self.assertEqual(21, len(store.list_subquestions("ws-write-cap")))
            self.assertFalse(any("工作区写入已达到本轮上限" in event.summary for event in events))

    def test_budget_stop_after_workspace_writes_keeps_direction_checkpoint(self):
        """A valid initial graph must survive a model-round budget stop."""
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store,
                workspace_id_factory=lambda: "ws-budget-checkpoint",
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )
            service.create(research_question="研究记忆重排序", anchor_paper_id="2608.21867")
            pipeline = build_workspace_pipeline(
                store, workspace_root=root,
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )

            def workspace_tools_factory(run_id):
                return WorkspaceResearchTools(
                    service, "ws-budget-checkpoint",
                    gate=pipeline.gate,
                    risk_classifier=pipeline.risk_classifier,
                    commit_service=pipeline.commit_service,
                    hitl_service=pipeline.hitl_service,
                    run_id=run_id,
                )

            model = ScriptedToolModel(script=iter([
                AIMessage(content="", tool_calls=[
                    {
                        "name": "update_subquestions",
                        "args": {
                            "operation": "add",
                            "question_id": "q-budget",
                            "text": "记忆变化时低秩补全是否仍然有效？",
                            "researchability": "candidate",
                        },
                        "id": "call-budget-question",
                        "type": "tool_call",
                    },
                    {
                        "name": "update_research_map",
                        "args": {
                            "node_id": "node-budget",
                            "label": "记忆变化下的低秩补全",
                            "related_question_ids": ["q-budget"],
                        },
                        "id": "call-budget-map",
                        "type": "tool_call",
                    },
                ]),
            ]))
            runtime = DeepAgentsV0Runtime(
                model_factory=lambda guard: model,
                tools_factory=tools_factory,
                assistant=True,
                workspace_resolver=lambda _run_id: str(Path(tmp) / "session"),
                workspace_tools_factory=workspace_tools_factory,
            )
            result = runtime.start(AgentRunInput(
                run_id="budget-checkpoint",
                question="请从论文边界提出一个可验证问题。",
                allowed_tools=ALLOWED_TOOLS_ASSISTANT,
                budgets=RunBudgets(1, 16, 24, 300, 40000, 8000),
                resolved={"retrieval_plan": "research_exploration"},
            ))

            self.assertEqual("awaiting_decision", result.stop_reason)
            self.assertEqual(1, len(store.list_research_map("ws-budget-checkpoint")))
            self.assertEqual(1, len(store.list_subquestions("ws-budget-checkpoint")))
            self.assertIn("记忆变化时低秩补全是否仍然有效", result.final_draft or "")

    def test_provider_failure_after_initial_graph_keeps_direction_checkpoint(self):
        """A provider 400 after durable writes must remain recoverable."""
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        class FailingAfterGraphModel(ScriptedToolModel):
            calls: int = 0

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "update_subquestions",
                                "args": {
                                    "operation": "add",
                                    "question_id": "q-provider",
                                    "text": "提供方失败后是否仍可从持久化证据继续？",
                                    "researchability": "candidate",
                                },
                                "id": "call-provider-question",
                                "type": "tool_call",
                            },
                            {
                                "name": "update_research_map",
                                "args": {
                                    "node_id": "node-provider",
                                    "label": "提供方失败后的恢复检查点",
                                    "related_question_ids": ["q-provider"],
                                },
                                "id": "call-provider-map",
                                "type": "tool_call",
                            },
                        ],
                    ))])
                raise ValueError("OpenAIInvalidRequestError: provider rejected transcript")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store,
                workspace_id_factory=lambda: "ws-provider-failure",
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )
            service.create(research_question="研究恢复", anchor_paper_id="2608.21867")
            pipeline = build_workspace_pipeline(
                store,
                workspace_root=root,
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )

            def workspace_tools_factory(run_id):
                return WorkspaceResearchTools(
                    service,
                    "ws-provider-failure",
                    gate=pipeline.gate,
                    risk_classifier=pipeline.risk_classifier,
                    commit_service=pipeline.commit_service,
                    hitl_service=pipeline.hitl_service,
                    run_id=run_id,
                )

            model = FailingAfterGraphModel(script=iter(()))
            runtime = DeepAgentsV0Runtime(
                model_factory=lambda guard: model,
                tools_factory=tools_factory,
                assistant=True,
                workspace_resolver=lambda _run_id: str(Path(tmp) / "session"),
                workspace_tools_factory=workspace_tools_factory,
            )
            result = runtime.start(AgentRunInput(
                run_id="provider-failure",
                question="请从论文边界提出一个可验证问题。",
                allowed_tools=ALLOWED_TOOLS_ASSISTANT,
                budgets=RunBudgets(8, 16, 24, 300, 40000, 8000),
                resolved={"retrieval_plan": "research_exploration"},
            ))

            self.assertEqual("awaiting_decision", result.stop_reason)
            self.assertEqual(1, len(store.list_research_map("ws-provider-failure")))
            self.assertEqual(1, len(store.list_subquestions("ws-provider-failure")))
            self.assertIn("提供方失败后是否仍可从持久化证据继续", result.final_draft or "")

    def test_later_round_checkpoint_does_not_claim_initial_paper_read(self):
        class FakeWorkspaceTools:
            def read_workspace_state(self):
                return {
                    "active_focus_id": "q-focus",
                    "subquestions": [
                        {"question_id": "q-focus", "text": "当前焦点", "researchability": "candidate", "status": "open"},
                        {"question_id": "q-next", "text": "下一轮可验证问题", "researchability": "candidate", "status": "open"},
                    ],
                    "evidence": [{"evidence_id": "E-1"}],
                    "research_plan": {"iterations": [{"sequence": 1}, {"sequence": 2}]},
                }

        draft = DeepAgentsV0Runtime._direction_checkpoint_final(FakeWorkspaceTools(), True)
        self.assertIn("第 2 轮研究已保存阶段性结果", draft or "")
        self.assertIn("当前焦点：当前焦点", draft or "")
        self.assertNotIn("已完成论文初读", draft or "")

    def test_fallback_initial_map_materializes_when_agent_only_writes_candidate(self):
        """The deterministic checkpoint fallback must work without a model map call."""
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store,
                workspace_id_factory=lambda: "ws-fallback-map",
                clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            )
            service.create(research_question="研究问题", anchor_paper_id="2608.21867")
            pipeline = build_workspace_pipeline(store, workspace_root=root)
            tools = WorkspaceResearchTools(
                service,
                "ws-fallback-map",
                gate=pipeline.gate,
                risk_classifier=pipeline.risk_classifier,
                commit_service=pipeline.commit_service,
                hitl_service=pipeline.hitl_service,
                run_id="fallback-map",
            )
            tools.update_subquestions(
                "add",
                "q-fallback",
                text="主题漂移下的检索补全是否仍然有效？",
                researchability="candidate",
            )

            self.assertTrue(tools.ensure_initial_map())
            nodes = store.list_research_map("ws-fallback-map")
            self.assertEqual(1, len(nodes))
            self.assertEqual(("q-fallback",), nodes[0].related_question_ids)

    def test_session_workspace_lifecycle_create_and_remove(self):
        import tempfile
        from pathlib import Path
        from research_pulse.workbench.session_workspace import SessionWorkspaceManager

        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionWorkspaceManager(tmp)
            # Resource workspace folder keyed by workspace_id, with per-session
            # M3 state isolated in a subfolder (Session ≈ ResearchRun).
            workspace_root = Path(manager.resolve("workspace-1"))
            self.assertTrue((workspace_root / "sessions").is_dir())
            self.assertTrue((workspace_root / "materials").is_dir())
            state_dir = Path(manager.session_state_dir("workspace-1", "session-lc"))
            self.assertTrue((state_dir / "notes").is_dir())
            self.assertTrue((state_dir / "evidence").is_dir())
            self.assertTrue((state_dir / "findings").is_dir())
            # resolve is idempotent
            self.assertEqual(manager.resolve("workspace-1"), str(workspace_root))
            manager.remove_session("workspace-1", "session-lc")
            self.assertFalse(state_dir.exists())
            manager.remove_workspace("workspace-1")
            self.assertFalse(workspace_root.exists())
            manager.remove_session("workspace-1", "session-lc")  # no-op on missing
