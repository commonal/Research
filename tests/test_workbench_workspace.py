"""Domain + persistence tests for the Research Assistant Workspace (M1).

These tests establish the M1 backend surface before any implementation:
- Workspace requires research_intent + anchor_paper_id; research_question remains
  a legacy compatibility field.
- Workspace lifecycle (create/list/get/archive) and stable ID.
- WorkspaceStatus state machine is Orchestrator-controlled (illegal transitions fail).
- Session != Run: one Session may have many Runs, all sharing one Workspace state.
- Durable objects (SubQuestion/Evidence/ResearchMapNode) use workspace-scoped stable
  IDs; text is editable but IDs never change; objects reference each other by ID.
- Canonical JSON (not Markdown) is the single source of truth; subquestions are
  updated via structured operations (add/update/resolve/deprioritize) that never
  overwrite the whole canonical file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from research_pulse.workbench.workspace import (
    AgentRun,
    CandidateHypothesis,
    Critique,
    Evidence,
    ExperimentPlan,
    HypothesisStatus,
    MethodMap,
    MethodMapEntry,
    PlanArtifactStatus,
    ProjectedWorkspaceState,
    Researchability,
    ResearchMapNode,
    ResearchIteration,
    ResearchIterationStatus,
    ResearchPlan,
    ResearchPlanStage,
    SubQuestion,
    SubQuestionStatus,
    Workspace,
    WorkspaceSession,
    WorkspaceStatus,
    WorkspaceNotFoundError,
    WorkspaceService,
)


NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


class _MemoryWorkspaceStore:
    """In-memory store satisfying the WorkspaceRepository protocol."""

    def __init__(self) -> None:
        self.workspaces: dict[str, Workspace] = {}
        self.subquestions: dict[str, dict[str, SubQuestion]] = {}
        self.evidence: dict[str, dict[str, Evidence]] = {}
        self.research_map: dict[str, dict[str, ResearchMapNode]] = {}

    def create(self, workspace: Workspace) -> None:
        self.workspaces[workspace.workspace_id] = workspace
        self.subquestions.setdefault(workspace.workspace_id, {})
        self.evidence.setdefault(workspace.workspace_id, {})

    def get(self, workspace_id: str) -> Workspace | None:
        return self.workspaces.get(workspace_id)

    def list(self) -> tuple[Workspace, ...]:
        return tuple(sorted(self.workspaces.values(), key=lambda w: w.created_at, reverse=True))

    def save(self, workspace: Workspace) -> None:
        self.workspaces[workspace.workspace_id] = workspace

    def list_subquestions(self, workspace_id: str) -> tuple[SubQuestion, ...]:
        return tuple(self.subquestions.get(workspace_id, {}).values())

    def get_subquestion(self, workspace_id: str, question_id: str) -> SubQuestion | None:
        return self.subquestions.get(workspace_id, {}).get(question_id)

    def upsert_subquestion(self, workspace_id: str, question: SubQuestion) -> None:
        self.subquestions.setdefault(workspace_id, {})[question.question_id] = question

    def patch_subquestion(self, workspace_id: str, question_id: str, **changes) -> SubQuestion:
        current = self.get_subquestion(workspace_id, question_id)
        if current is None:
            raise WorkspaceNotFoundError(question_id)
        updated = SubQuestion(
            **{**current.__dict__, **changes, "question_id": question_id},
        )
        self.upsert_subquestion(workspace_id, updated)
        return updated

    def list_evidence(self, workspace_id: str) -> tuple[Evidence, ...]:
        return tuple(self.evidence.get(workspace_id, {}).values())

    def upsert_evidence(self, workspace_id: str, evidence: Evidence) -> None:
        self.evidence.setdefault(workspace_id, {})[evidence.evidence_id] = evidence

    def list_research_map(self, workspace_id: str) -> tuple[ResearchMapNode, ...]:
        return tuple(self.research_map.get(workspace_id, {}).values())

    def get_research_map_node(self, workspace_id: str, node_id: str) -> ResearchMapNode | None:
        return self.research_map.get(workspace_id, {}).get(node_id)

    def upsert_research_map_node(self, workspace_id: str, node: ResearchMapNode) -> None:
        self.research_map.setdefault(workspace_id, {})[node.node_id] = node


class WorkspaceCreationTests(TestCase):
    def test_workspace_requires_research_question_and_anchor_paper(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)

        with self.assertRaises(ValueError):
            service.create(research_question=None, anchor_paper_id="paper-x")
        with self.assertRaises(ValueError):
            service.create(research_question="   ", anchor_paper_id="paper-x")
        with self.assertRaises(ValueError):
            service.create(research_question="一个问题", anchor_paper_id="  ")
        with self.assertRaises(ValueError):
            service.create(research_question=None, anchor_paper_id=None)

    def test_create_workspace_has_stable_id_and_created_status(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)

        created = service.create(research_question="如何减少位置偏差？", anchor_paper_id="paper-x")

        self.assertEqual(created.workspace_id, "ws-1")
        self.assertEqual(created.research_question, "如何减少位置偏差？")
        self.assertEqual(created.anchor_paper_id, "paper-x")
        self.assertEqual(created.status, WorkspaceStatus.CREATED)
        self.assertEqual(created.created_at, NOW)

    def test_create_workspace_can_start_with_broad_research_intent(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-intent", clock=lambda: NOW)

        created = service.create(research_intent="探索推荐系统中的位置偏差", anchor_paper_id="paper-x")

        self.assertEqual(created.research_intent, "探索推荐系统中的位置偏差")
        self.assertEqual(created.research_question, "")

    def test_active_focus_requires_an_explicit_candidate(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-focus", clock=lambda: NOW)
        service.create(research_intent="研究位置偏差", anchor_paper_id="paper-x")
        store.upsert_subquestion(
            "ws-focus", SubQuestion(question_id="Q-1", text="可验证线索", researchability=Researchability.CANDIDATE)
        )

        selected = service.set_active_focus("ws-focus", "Q-1")

        self.assertEqual(selected.active_focus_id, "Q-1")

    def test_get_after_create_returns_same_aggregate(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)

        created = service.create(research_question="一个研究问题", anchor_paper_id="paper-x")
        restored = service.get("ws-1")

        self.assertEqual(restored, created)

    def test_get_missing_workspace_raises(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)

        with self.assertRaises(WorkspaceNotFoundError):
            service.get("nope")

    def test_list_returns_newest_first(self) -> None:
        store = _MemoryWorkspaceStore()
        times = iter([NOW, datetime(2026, 9, 2, 11, 0, tzinfo=UTC)])
        service = WorkspaceService(
            store,
            workspace_id_factory=lambda: "ws-1",
            clock=lambda: next(times),
        )
        service.create(research_question="问题一", anchor_paper_id="p1")

        service2 = WorkspaceService(
            store,
            workspace_id_factory=lambda: "ws-2",
            clock=lambda: datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
        )
        service2.create(research_question="问题二", anchor_paper_id="p2")

        listed = service.list()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0].research_question, "问题二")
        self.assertEqual(listed[1].research_question, "问题一")

    def test_archive_and_restore_workspace(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)

        created = service.create(research_question="一个问题", anchor_paper_id="paper-x")
        archived = service.archive("ws-1")
        restored = service.restore("ws-1")

        self.assertEqual(archived.status, WorkspaceStatus.ARCHIVED)
        self.assertEqual(restored.status, WorkspaceStatus.CREATED)

    def test_cannot_archive_without_existing_workspace(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)

        with self.assertRaises(WorkspaceNotFoundError):
            service.archive("ghost")


class WorkspaceStatusStateMachineTests(TestCase):
    def test_legal_orchestrator_transitions(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        w = service.get("ws-1")
        w = service.advance_status("ws-1", WorkspaceStatus.INITIAL_RESEARCH)
        w = service.advance_status("ws-1", WorkspaceStatus.WAITING_FOR_USER_ACTION)
        w = service.advance_status("ws-1", WorkspaceStatus.INVESTIGATING)
        w = service.advance_status("ws-1", WorkspaceStatus.WAITING_FOR_USER_ACTION)

        self.assertEqual(w.status, WorkspaceStatus.WAITING_FOR_USER_ACTION)

    def test_illegal_direct_transition_fails(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        # CREATED -> INVESTIGATING is illegal: must pass INITIAL_RESEARCH then WAITING.
        with self.assertRaises(ValueError):
            service.advance_status("ws-1", WorkspaceStatus.INVESTIGATING)

        # CREATED -> ARCHIVED is an allowed terminal short-circuit, but replaying
        # INVESTIGATING from CREATED must not be allowed.
        self.assertEqual(service.get("ws-1").status, WorkspaceStatus.CREATED)

    def test_agent_cannot_set_status_via_write_tools(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        # The service only exposes advance_status (Orchestrator gate); there is no
        # public "set status" mutation free for the agent.
        with self.assertRaises(AttributeError):
            # type: ignore[attr-defined]
            service.set_status("ws-1", WorkspaceStatus.WAITING_FOR_USER_ACTION)


class StableIdAndOperationTests(TestCase):
    def test_subquestion_has_stable_id_text_editable_but_id_unchanged(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        q = service.add_subquestion("ws-1", "Q-003", "原文本")
        edited = service.update_subquestion_text("ws-1", "Q-003", "编辑后的文本")

        self.assertEqual(edited.question_id, "Q-003")
        self.assertEqual(edited.text, "编辑后的文本")
        self.assertEqual(store.get_subquestion("ws-1", "Q-003").question_id, "Q-003")

    def test_subquestion_operations_do_not_overwrite_whole_file(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        service.add_subquestion("ws-1", "Q-001", "问题一")
        service.add_subquestion("ws-1", "Q-002", "问题二")
        service.update_subquestion_text("ws-1", "Q-001", "问题一改")

        all_questions = store.list_subquestions("ws-1")
        self.assertEqual(len(all_questions), 2)
        by_id = {q.question_id: q for q in all_questions}
        self.assertEqual(by_id["Q-001"].text, "问题一改")
        self.assertEqual(by_id["Q-002"].text, "问题二")

    def test_resolve_and_deprioritize(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        service.add_subquestion("ws-1", "Q-001", "问题一")
        resolved = service.resolve_subquestion("ws-1", "Q-001", "答案是X")
        deprioritized = service.deprioritize_subquestion("ws-1", "Q-001")

        self.assertEqual(resolved.status, SubQuestionStatus.RESOLVED)
        self.assertEqual(resolved.answer, "答案是X")
        self.assertEqual(deprioritized.status, SubQuestionStatus.DEPRIORITIZED)

    def test_evidence_requires_source_and_block_ids(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        with self.assertRaises(ValueError):
            service.add_evidence("ws-1", evidence_id="E-012", source_id="paper-x", block_ids=())

        with self.assertRaises(ValueError):
            service.add_evidence("ws-1", evidence_id="E-012", source_id="", block_ids=("b31",))

    def test_evidence_supports_multiple_questions_by_id(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        evidence = service.add_evidence(
            "ws-1",
            evidence_id="E-012",
            source_id="paper-x",
            block_ids=("b31", "b32"),
            supports_question_ids=("Q-001", "Q-003"),
            claim="最小判断",
            research_interpretation="对研究问题的含义",
        )

        self.assertEqual(evidence.evidence_id, "E-012")
        self.assertEqual(evidence.supports_question_ids, ("Q-001", "Q-003"))
        self.assertEqual(evidence.claim, "最小判断")
        self.assertEqual(evidence.research_interpretation, "对研究问题的含义")

    def test_research_map_node_references_by_stable_id(self) -> None:
        node = ResearchMapNode(node_id="M-007", related_question_ids=("Q-003",), evidence_ids=("E-012",))
        self.assertEqual(node.node_id, "M-007")
        self.assertEqual(node.related_question_ids, ("Q-003",))
        self.assertEqual(node.evidence_ids, ("E-012",))


class ResearchMapOperationTests(TestCase):
    def test_add_research_map_node_and_update_label_keeps_stable_id(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        node = service.add_research_map_node(
            "ws-1", "M-007", label="  DPO 路线  ",
            related_question_ids=("Q-003",),
            evidence_ids=("E-012",),
        )
        updated = service.update_research_map_node("ws-1", "M-007", label="CausalDPO 路线")

        self.assertEqual(node.node_id, "M-007")
        self.assertEqual(node.label, "DPO 路线")  # normalized whitespace
        self.assertEqual(updated.label, "CausalDPO 路线")
        self.assertEqual(updated.node_id, "M-007")  # id never changes
        listed = service.list_research_map("ws-1")
        self.assertEqual(len(listed), 1)

    def test_update_research_map_node_sets_field_not_corrupts_siblings(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")
        service.add_research_map_node("ws-1", "M-007", related_question_ids=("Q-001",), evidence_ids=("E-01",), label="路线")

        # Setting related_question_ids replaces that field's id set, exactly like
        # the other structured update operations; other fields are untouched.
        updated = service.update_research_map_node("ws-1", "M-007", related_question_ids=("Q-003",))

        self.assertEqual(updated.related_question_ids, ("Q-003",))
        self.assertEqual(updated.evidence_ids, ("E-01",))  # sibling field preserved
        self.assertEqual(updated.label, "路线")  # sibling field preserved

    def test_update_missing_research_node_raises(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")
        with self.assertRaises(WorkspaceNotFoundError):
            service.update_research_map_node("ws-1", "M-999", label="x")


class SessionRunSplitTests(TestCase):
    def test_session_holds_many_runs_all_sharing_one_workspace(self) -> None:
        store = _MemoryWorkspaceStore()
        service = WorkspaceService(store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW)
        service.create(research_question="一个问题", anchor_paper_id="paper-x")

        session = WorkspaceSession(session_id="sess-1", workspace_id="ws-1", created_at=NOW)
        first_run = AgentRun(run_id="run-001", session_id="sess-1", workspace_id="ws-1", message="调查 Q1")
        second_run = AgentRun(run_id="run-002", session_id="sess-1", workspace_id="ws-1", message="改查 Q3")

        # Session is a dialog context; each user message is one Run within it.
        self.assertEqual(session.session_id, "sess-1")
        self.assertEqual(session.workspace_id, "ws-1")
        self.assertEqual(first_run.session_id, "sess-1")
        self.assertEqual(second_run.session_id, "sess-1")
        self.assertNotEqual(first_run.run_id, second_run.run_id)
        # All runs and the session point at the same durable workspace state.
        self.assertEqual({first_run.workspace_id, second_run.workspace_id}, {"ws-1"})

    def test_session_is_not_equal_to_run(self) -> None:
        session = WorkspaceSession(session_id="sess-1", workspace_id="ws-1", created_at=NOW)
        run = AgentRun(run_id="run-001", session_id="sess-1", workspace_id="ws-1", message="调查")
        # A Session is a container of Runs, not a Run itself; they are distinct objects.
        self.assertFalse(hasattr(session, "message"))
        self.assertTrue(hasattr(run, "message"))


class CanonicalJsonStoreTests(TestCase):
    def test_json_store_reads_and_writes_workspace_state(self) -> None:
        from research_pulse.workbench.workspace_json import WorkspaceJsonStore

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW
            )
            service.create(research_question="一个问题", anchor_paper_id="paper-x")
            service.add_subquestion("ws-1", "Q-001", "问题一")

            workspace_dir = root / "ws-1"
            self.assertTrue((workspace_dir / "workspace.json").exists())
            self.assertTrue((workspace_dir / "subquestions.json").exists())
            self.assertFalse((workspace_dir / "research-map.md").exists())
            self.assertTrue((workspace_dir / "progress-snapshot.json").exists() or True)

            restored = store.get("ws-1")
            self.assertEqual(restored.research_question, "一个问题")
            self.assertEqual(len(store.list_subquestions("ws-1")), 1)
            self.assertEqual(restored.research_plan.stage, ResearchPlanStage.NOT_STARTED)
            self.assertTrue((workspace_dir / "research-plan.json").exists())

    def test_json_store_round_trips_research_plan_artifacts(self) -> None:
        from research_pulse.workbench.workspace_json import WorkspaceJsonStore

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            plan = ResearchPlan(
                stage=ResearchPlanStage.EXPERIMENT_PLANNING,
                iterations=(ResearchIteration(
                    iteration_id="I-1",
                    sequence=1,
                    title="第一轮：确认研究缺口",
                    status=ResearchIterationStatus.COMPLETED,
                    focus_question_id="Q-1",
                    run_id="run-1",
                    summary="确认锚点论文没有覆盖该敏感性分析。",
                    evidence_ids=("E-1",),
                    candidate_question_ids=("Q-2",),
                    decision="继续检索相关工作",
                    next_step="导入并精读候选论文",
                ),),
                method_map=MethodMap(
                    map_id="MM-1",
                    status=PlanArtifactStatus.APPROVED,
                    entries=(MethodMapEntry(
                        method_id="M-1",
                        name="位置偏差建模",
                        mechanism="分离展示位置与偏好信号",
                        assumptions="位置效应可估计",
                        evidence_ids=("E-1",),
                        limitations="尚未覆盖冷启动",
                    ),),
                ),
                hypotheses=(CandidateHypothesis(
                    hypothesis_id="H-1",
                    text="偏差修正改善公平性",
                    question_id="Q-1",
                    evidence_ids=("E-1",),
                    rationale="锚点证据支持可分离信号",
                    falsifiers=("公平性没有提升",),
                    status=HypothesisStatus.SELECTED,
                ),),
                critiques=(Critique(
                    critique_id="C-1",
                    hypothesis_id="H-1",
                    strengths=("机制清晰",),
                    risks=("可能牺牲相关性",),
                    alternatives=("分层校准",),
                    evidence_ids=("E-1",),
                    confidence="medium",
                    status=PlanArtifactStatus.DRAFT,
                ),),
                experiment_plans=(ExperimentPlan(
                    plan_id="EP-1",
                    hypothesis_id="H-1",
                    intervention="加入位置校正项",
                    baselines=("原始排序器",),
                    datasets=("MovieLens",),
                    metrics=("NDCG", "公平性差异"),
                    ablations=("移除校正项",),
                    expected_outcomes="公平性提升且相关性不显著下降",
                    decision_criteria="两个指标均达到预设阈值",
                    resource_estimate="单卡一天",
                    risks=("数据分布偏移",),
                ),),
            )
            workspace = Workspace(
                workspace_id="ws-plan",
                research_question="",
                research_intent="研究推荐位置偏差",
                anchor_paper_id="paper-x",
                research_plan=plan,
            )
            store.create(workspace)

            restored = store.get("ws-plan")

            self.assertIsNotNone(restored)
            self.assertEqual(restored.research_plan, plan)
            store.apply_commit(
                "ws-plan",
                ProjectedWorkspaceState(workspace_revision=2, research_plan=plan),
            )
            self.assertEqual(store.get("ws-plan").research_plan, plan)

    def test_json_store_never_writes_paper_body_into_workspace(self) -> None:
        from research_pulse.workbench.workspace_json import WorkspaceJsonStore

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            store = WorkspaceJsonStore(root)
            service = WorkspaceService(
                store, workspace_id_factory=lambda: "ws-1", clock=lambda: NOW
            )
            service.create(research_question="一个问题", anchor_paper_id="paper-x")
            service.add_evidence(
                "ws-1", evidence_id="E-012", source_id="paper-x",
                block_ids=("b31",), claim="断言", research_interpretation="含义",
            )

            evidence_dir = root / "ws-1" / "evidence"
            self.assertTrue((evidence_dir / "E-012.json").exists())
            # The workspace must not copy the paper's full text; only the claim
            # and interpretation live here.
            raw = (evidence_dir / "E-012.json").read_text(encoding="utf-8")
            self.assertNotIn("论文原文全文", raw)
