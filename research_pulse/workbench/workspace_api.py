"""HTTP boundary for the research-workspace state (M4).

Exposes a session's workspace canonical state and its human-readable Markdown
projections. The conversation only links to these documents; it does not grow a
permanent side panel for the full Research Map / subquestions / evidence / plan. UI
mutations still go through the domain pipeline rather than editing Markdown.

Canonical state is read-only by construction here except for resolving a pending
``DecisionPoint`` or explicitly setting the user-selected focus. Document GETs
may regenerate Markdown projections, but never mutate the canonical research
state.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from research_pulse.workbench.hitl import DecisionPointError, HITLService
from research_pulse.workbench.workspace import (
    ResearchPlan,
    ResearchArtifactStage,
    Researchability,
    SubQuestionStatus,
    WorkspaceNotFoundError,
    WorkspaceService,
)


class ResolveDecisionRequest(BaseModel):
    approved: bool
    decision: str | None = None
    operation_id: str | None = None
    resolved_run_id: str | None = None
    question_id: str | None = None


class SetFocusRequest(BaseModel):
    question_id: str


def _iteration_status_label(status: str) -> str:
    return {
        "in_progress": "进行中",
        "waiting_for_user": "等待你确认",
        "completed": "已完成",
        "abandoned": "已放弃",
    }.get(status, status)


_ARTIFACT_STAGE_LABELS = {
    ResearchArtifactStage.OVERVIEW.value: "研究概览",
    ResearchArtifactStage.STAGE_NOTE.value: "阶段性研究笔记",
    ResearchArtifactStage.REPORT_DRAFT.value: "研究报告草稿",
    ResearchArtifactStage.REPORT.value: "研究报告",
}
_ARTIFACT_STAGE_DESCRIPTIONS = {
    ResearchArtifactStage.OVERVIEW.value: "刚完成初步理解，证据仍在积累。",
    ResearchArtifactStage.STAGE_NOTE.value: "已有可追溯证据和研究迭代，但还不是最终报告。",
    ResearchArtifactStage.REPORT_DRAFT.value: "已整理成报告草稿，仍需检查证据覆盖和结论边界。",
    ResearchArtifactStage.REPORT.value: "已完成用户确认，可作为当前研究报告。",
}


def _effective_artifact_stage(plan: ResearchPlan, *, evidence_count: int = 0) -> ResearchArtifactStage:
    """Return the conservative user-facing maturity of the research output.

    Legacy workspaces may have iterations/evidence but no ``artifact_stage``;
    expose them as a stage note without mutating canonical state.  Promotion to
    a report remains an explicit action and is never inferred from one run.
    """
    if plan.artifact_stage is ResearchArtifactStage.OVERVIEW and (
        plan.iterations or evidence_count > 0
    ):
        return ResearchArtifactStage.STAGE_NOTE
    return plan.artifact_stage


def _artifact_projection(plan: ResearchPlan, *, evidence_count: int = 0) -> dict[str, object]:
    stage = _effective_artifact_stage(plan, evidence_count=evidence_count)
    return {
        "stage": stage.value,
        "label": _ARTIFACT_STAGE_LABELS[stage.value],
        "description": _ARTIFACT_STAGE_DESCRIPTIONS[stage.value],
        "can_generate_report": stage in {
            ResearchArtifactStage.STAGE_NOTE,
            ResearchArtifactStage.REPORT_DRAFT,
        },
        "is_final": stage is ResearchArtifactStage.REPORT,
    }


def project_workspace_state(
    service: WorkspaceService,
    workspace_id: str,
    *,
    decision_service: HITLService | None = None,
) -> dict[str, Any]:
    """Deterministic read projection of one workspace's canonical state."""
    workspace = service.get(workspace_id)
    result: dict[str, Any] = {
        "workspace_id": workspace.workspace_id,
        "research_question": workspace.research_question,
        "research_intent": workspace.research_intent,
        "active_focus_id": workspace.active_focus_id,
        "anchor_paper_id": workspace.anchor_paper_id,
        "status": workspace.status.value,
        "workspace_revision": workspace.workspace_revision,
        "research_map": [
            {
                "node_id": node.node_id,
                "label": node.label,
                "related_question_ids": list(node.related_question_ids),
                "evidence_ids": list(node.evidence_ids),
            }
            for node in service.list_research_map(workspace_id)
        ],
        "subquestions": [
            {
                "question_id": question.question_id,
                "text": question.text,
                "status": question.status.value,
                "answer": question.answer,
                "researchability": question.researchability.value,
            }
            for question in service.list_subquestions(workspace_id)
        ],
        "evidence": [
            {
                "evidence_id": evidence.evidence_id,
                "source_id": evidence.source_id,
                "block_ids": list(evidence.block_ids),
                "supports_question_ids": list(evidence.supports_question_ids),
                "evidence_role": evidence.evidence_role,
                "claim": evidence.claim,
                "research_interpretation": evidence.research_interpretation,
                "confidence": evidence.confidence,
            }
            for evidence in service.list_evidence(workspace_id)
        ],
        "research_plan": _project_research_plan(
            workspace.research_plan,
            evidence_count=len(service.list_evidence(workspace_id)),
        ),
    }
    if decision_service is not None:
        result["decision_points"] = [
            asdict(decision)
            for decision in decision_service.list_for_workspace(workspace.workspace_id)
        ]
    return result


def _project_research_plan(plan: ResearchPlan, *, evidence_count: int = 0) -> dict[str, Any]:
    """Return a JSON-safe projection of the structured research plan."""
    artifact = _artifact_projection(plan, evidence_count=evidence_count)
    return {
        "stage": plan.stage.value,
        "artifact_stage": artifact["stage"],
        "artifact": artifact,
        "iterations": [
            {
                "iteration_id": item.iteration_id,
                "sequence": item.sequence,
                "title": item.title,
                "status": item.status.value,
                "focus_question_id": item.focus_question_id,
                "run_id": item.run_id,
                "summary": item.summary,
                "evidence_ids": list(item.evidence_ids),
                "candidate_question_ids": list(item.candidate_question_ids),
                "decision": item.decision,
                "next_step": item.next_step,
            }
            for item in plan.iterations
        ],
        "method_map": (
            {
                "map_id": plan.method_map.map_id,
                "status": plan.method_map.status.value,
                "entries": [
                    {
                        "method_id": entry.method_id,
                        "name": entry.name,
                        "mechanism": entry.mechanism,
                        "assumptions": entry.assumptions,
                        "evidence_ids": list(entry.evidence_ids),
                        "limitations": entry.limitations,
                    }
                    for entry in plan.method_map.entries
                ],
            }
            if plan.method_map is not None
            else None
        ),
        "hypotheses": [
            {
                "hypothesis_id": item.hypothesis_id,
                "text": item.text,
                "question_id": item.question_id,
                "evidence_ids": list(item.evidence_ids),
                "rationale": item.rationale,
                "falsifiers": list(item.falsifiers),
                "status": item.status.value,
            }
            for item in plan.hypotheses
        ],
        "critiques": [
            {
                "critique_id": item.critique_id,
                "hypothesis_id": item.hypothesis_id,
                "strengths": list(item.strengths),
                "risks": list(item.risks),
                "alternatives": list(item.alternatives),
                "evidence_ids": list(item.evidence_ids),
                "confidence": item.confidence,
                "status": item.status.value,
            }
            for item in plan.critiques
        ],
        "experiment_plans": [
            {
                "plan_id": item.plan_id,
                "hypothesis_id": item.hypothesis_id,
                "status": item.status.value,
                "intervention": item.intervention,
                "baselines": list(item.baselines),
                "datasets": list(item.datasets),
                "metrics": list(item.metrics),
                "ablations": list(item.ablations),
                "expected_outcomes": item.expected_outcomes,
                "decision_criteria": item.decision_criteria,
                "resource_estimate": item.resource_estimate,
                "risks": list(item.risks),
            }
            for item in plan.experiment_plans
        ],
    }


def _workspace_documents(service: WorkspaceService, workspace_id: str) -> list[dict[str, Any]]:
    """Render small, user-facing Markdown projections from canonical state.

    The JSON workspace remains the source of truth. These files are deliberately
    simple reading artifacts: they are regenerated on demand and never used as a
    write path for research state.
    """
    workspace = service.get(workspace_id)
    questions = service.list_subquestions(workspace_id)
    evidence = service.list_evidence(workspace_id)
    nodes = service.list_research_map(workspace_id)
    intent = workspace.research_intent or workspace.research_question or "尚未确定研究议题"
    active = next((q for q in questions if q.question_id == workspace.active_focus_id), None)
    iterations = sorted(workspace.research_plan.iterations, key=lambda item: item.sequence)
    candidates = [q for q in questions if q.researchability.value == "candidate" and q.status.value != "deprioritized"]
    boundaries = [q for q in questions if q.researchability.value == "boundary"]

    iteration_brief_lines: list[str] = []
    iteration_progress_lines: list[str] = []
    for item in iterations:
        iteration_brief_lines.extend([
            f"### 第 {item.sequence} 轮 · {item.title}",
            f"- 状态：{_iteration_status_label(item.status.value)}",
            f"- 阶段结论：{item.summary or '暂无'}",
            f"- 本轮决策：{item.decision or '暂无'}",
            f"- 下一步：{item.next_step or '暂无'}",
            "",
        ])
        iteration_progress_lines.extend([
            f"- 第 {item.sequence} 轮：{item.title}（{_iteration_status_label(item.status.value)}）",
            f"  - {item.summary or '本轮尚未记录阶段结论'}",
            f"  - 下一步：{item.next_step or '待确定'}",
        ])

    def bullet(items: list[str], empty: str = "暂无") -> str:
        return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"

    artifact = _artifact_projection(workspace.research_plan, evidence_count=len(evidence))
    brief = "\n".join([
        "# 研究概览",
        "",
        f"**当前产物：** {artifact['label']}",
        f"**状态说明：** {artifact['description']}",
        "**报告状态：** 尚未单独生成最终研究报告；需要更多独立证据和多轮分析。"
        if artifact["stage"] != ResearchArtifactStage.REPORT.value
        else "**报告状态：** 已生成并确认。",
        "",
        "## 研究议题",
        intent,
        "",
        "## 锚点论文",
        workspace.anchor_paper_id or "尚未绑定论文",
        "",
        "## 当前研究焦点",
        active.text if active else "尚未选择当前研究焦点",
        "",
        "## 研究迭代",
        *iteration_brief_lines,
        "尚未开始多轮研究。" if not iterations else "",
        "## 说明",
        "研究议题是稳定的研究背景；候选线索需要用户确认后才会成为当前研究焦点。",
        "",
    ])
    status_labels = {
        "created": "未开始",
        "initial_research": "初步研究中",
        "waiting_for_user_action": "等待你确认",
        "investigating": "深入研究中",
        "archived": "已归档",
    }
    progress_sections = [
        "# 当前研究进展",
        "",
        f"**状态：** {status_labels.get(workspace.status.value, workspace.status.value)}",
        "",
        "## 当前焦点",
        active.text if active else "尚未选择当前研究焦点。",
        "",
        "## 研究迭代",
        *iteration_progress_lines,
        "- 尚未开始多轮研究。" if not iterations else "",
        "## 研究结构",
        bullet([node.label or node.node_id for node in nodes], "尚未形成研究结构"),
        "",
        "## 候选研究线索",
        bullet([q.text for q in candidates]),
        "",
        "## 论文边界",
        bullet([q.text for q in boundaries]),
        "",
        "## 下一步",
        "用户可以从候选线索中选择一个方向并开始核查。",
        "",
    ]
    evidence_lines = [
        "# 证据清单",
        "",
        "说明：每条证据都保留来源和证据块定位，可回到论文原文核验。",
        "",
    ]
    if evidence:
        for item in evidence:
            evidence_lines.extend([
                f"## {item.evidence_id}",
                f"- 角色：{item.evidence_role}",
                f"- 可信度：{item.confidence}",
                f"- 来源：{item.source_id}",
                f"- 证据块：{', '.join(item.block_ids)}",
                f"- 原文判断：{item.claim or '暂无'}",
                f"- 研究解释：{item.research_interpretation or '暂无'}",
                "",
            ])
    else:
        evidence_lines.append("- 尚未记录证据\n")

    stage_labels = {
        "not_started": "尚未开始",
        "method_mapping": "方法路线整理",
        "hypothesis_review": "候选假设评审",
        "experiment_planning": "实验计划设计",
        "ready": "可执行",
    }
    artifact_status_labels = {
        "draft": "草稿",
        "approved": "已确认",
        "rejected": "已否决",
    }
    hypothesis_status_labels = {
        "candidate": "候选",
        "selected": "已选中",
        "rejected": "已否决",
    }
    plan = workspace.research_plan
    plan_lines = [
        "# 研究计划",
        "",
        "> 这份文档记录从论文理解到可执行研究方案的中间产物。它是只读投影，修改必须通过工作区的受控操作完成。",
        "",
        f"**阶段（Stage）：** {stage_labels.get(plan.stage.value, plan.stage.value)}",
        f"**产物成熟度：** {artifact['label']}（{artifact['description']}）",
        "",
        "## 研究迭代记录",
    ]
    if not iterations:
        plan_lines.extend(["尚未记录研究迭代。", ""])
    else:
        for item in iterations:
            plan_lines.extend([
                f"### 第 {item.sequence} 轮 · {item.title}",
                f"- 状态：{_iteration_status_label(item.status.value)}",
                f"- 当前焦点：{item.focus_question_id or '研究主线'}",
                f"- 阶段结论：{item.summary or '暂无'}",
                f"- 关联证据：{', '.join(item.evidence_ids) if item.evidence_ids else '暂无'}",
                f"- 产生候选问题：{', '.join(item.candidate_question_ids) if item.candidate_question_ids else '暂无'}",
                f"- 本轮决策：{item.decision or '暂无'}",
                f"- 下一步：{item.next_step or '暂无'}",
                "",
            ])
    plan_lines.extend([
        "## 方法路线图（Method Map）",
    ])
    if plan.method_map is None or not plan.method_map.entries:
        plan_lines.extend([
            "尚未形成方法路线图。先完成锚点论文的机制、假设和限制整理。",
            "",
        ])
    else:
        plan_lines.extend([
            f"**状态：** {artifact_status_labels.get(plan.method_map.status.value, plan.method_map.status.value)}",
            "",
        ])
        for entry in plan.method_map.entries:
            plan_lines.extend([
                f"### {entry.name}（{entry.method_id}）",
                f"- 机制：{entry.mechanism or '暂无'}",
                f"- 前提：{entry.assumptions or '暂无'}",
                f"- 局限：{entry.limitations or '暂无'}",
                f"- 关联证据：{', '.join(entry.evidence_ids) if entry.evidence_ids else '暂无'}",
                "",
            ])
    plan_lines.append("## 候选研究假设（Candidate Hypotheses）")
    if not plan.hypotheses:
        plan_lines.extend(["尚未形成可检验的候选假设。", ""])
    else:
        for hypothesis in plan.hypotheses:
            plan_lines.extend([
                f"### {hypothesis.hypothesis_id} · {hypothesis_status_labels.get(hypothesis.status.value, hypothesis.status.value)}",
                hypothesis.text,
                f"- 对应线索：{hypothesis.question_id or '暂无'}",
                f"- 提出理由：{hypothesis.rationale or '暂无'}",
                f"- 关联证据：{', '.join(hypothesis.evidence_ids) if hypothesis.evidence_ids else '暂无'}",
                f"- 可能证伪：{'; '.join(hypothesis.falsifiers) if hypothesis.falsifiers else '暂无'}",
                "",
            ])
    plan_lines.append("## 批判与风险（Critique）")
    if not plan.critiques:
        plan_lines.extend(["尚未形成批判记录。", ""])
    else:
        for critique in plan.critiques:
            plan_lines.extend([
                f"### {critique.critique_id} · 针对 {critique.hypothesis_id}",
                f"- 状态：{artifact_status_labels.get(critique.status.value, critique.status.value)}",
                f"- 优势：{'; '.join(critique.strengths) if critique.strengths else '暂无'}",
                f"- 风险：{'; '.join(critique.risks) if critique.risks else '暂无'}",
                f"- 替代解释：{'; '.join(critique.alternatives) if critique.alternatives else '暂无'}",
                f"- 可信度：{critique.confidence}",
                "",
            ])
    plan_lines.append("## 实验计划（Experiment Plan）")
    if not plan.experiment_plans:
        plan_lines.extend([
            "尚未形成实验计划。只有候选假设经过批判后，才进入干预、基线和判定标准设计。",
            "",
        ])
    else:
        for experiment in plan.experiment_plans:
            plan_lines.extend([
                f"### {experiment.plan_id} · {artifact_status_labels.get(experiment.status.value, experiment.status.value)}",
                f"- 对应假设：{experiment.hypothesis_id or '暂无'}",
                f"- 干预：{experiment.intervention or '暂无'}",
                f"- 基线：{', '.join(experiment.baselines) if experiment.baselines else '暂无'}",
                f"- 数据集：{', '.join(experiment.datasets) if experiment.datasets else '暂无'}",
                f"- 指标：{', '.join(experiment.metrics) if experiment.metrics else '暂无'}",
                f"- 消融：{', '.join(experiment.ablations) if experiment.ablations else '暂无'}",
                f"- 预期结果：{experiment.expected_outcomes or '暂无'}",
                f"- 判定标准：{experiment.decision_criteria or '暂无'}",
                f"- 资源估计：{experiment.resource_estimate or '暂无'}",
                f"- 风险：{'; '.join(experiment.risks) if experiment.risks else '暂无'}",
                "",
            ])

    docs = [
        {"document_id": "research-brief", "title": "研究概览", "filename": "research-brief.md", "markdown": brief},
        {"document_id": "current-progress", "title": "当前研究进展", "filename": "current-progress.md", "markdown": "\n".join(progress_sections)},
        {"document_id": "evidence-index", "title": "证据清单", "filename": "evidence-index.md", "markdown": "\n".join(evidence_lines)},
        {"document_id": "research-plan", "title": "研究计划", "filename": "research-plan.md", "markdown": "\n".join(plan_lines)},
    ]
    repository = getattr(service, "repository", None)
    root = getattr(repository, "root", None)
    if root is not None:
        artifact_dir = Path(root) / "artifacts"
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # The API can still serve the projection when a deployment mounts
            # the state directory read-only; file materialization is best effort.
            return docs
        for item in docs:
            try:
                (artifact_dir / item["filename"]).write_text(item["markdown"], encoding="utf-8")
            except OSError:
                continue
            item["path"] = str(artifact_dir / item["filename"])
            item["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return docs


def build_workspace_router(
    service_provider: Callable[[str], WorkspaceService | None],
    *,
    decision_provider: Callable[[str], HITLService | None] | None = None,
    session_workspace_resolver: Callable[[str], str | None] | None = None,
) -> APIRouter:
    """Router for session workspace state (M4).

    ``service_provider(session_id)`` resolves the session's workspace service (or
    ``None`` when no workspace is bound yet). ``decision_provider`` optionally
    resolves the HITL service for the same session.
    ``session_workspace_resolver`` returns the session's real RESOURCE workspace
    id; without it the router falls back to the legacy ``ws-{session_id}`` key so
    isolated router tests keep working.
    """
    router = APIRouter(prefix="/api/workbench/workspaces", tags=["workbench"])

    def workspace_id_for(session_id: str) -> str:
        if session_workspace_resolver is not None:
            resolved = session_workspace_resolver(session_id)
            if resolved:
                return resolved
        return f"ws-{session_id}"

    @router.get("/{session_id}")
    def get_workspace(session_id: str) -> dict[str, Any]:
        service = service_provider(session_id)
        if service is None:
            raise HTTPException(404, "workspace not bound to this session")
        try:
            decisions = (
                decision_provider(session_id) if decision_provider else None
            )
            return project_workspace_state(
                service, workspace_id_for(session_id), decision_service=decisions
            )
        except WorkspaceNotFoundError as error:
            raise HTTPException(404, "workspace not found") from error

    @router.get("/{session_id}/documents")
    def get_workspace_documents(session_id: str) -> dict[str, Any]:
        service = service_provider(session_id)
        if service is None:
            raise HTTPException(404, "workspace not bound to this session")
        try:
            workspace_id = workspace_id_for(session_id)
            return {"items": _workspace_documents(service, workspace_id)}
        except WorkspaceNotFoundError as error:
            raise HTTPException(404, "workspace not found") from error

    @router.post("/{session_id}/decisions/{decision_id}/resolve")
    def resolve_decision(
        session_id: str, decision_id: str, request: ResolveDecisionRequest
    ) -> dict[str, Any]:
        if decision_provider is None:
            raise HTTPException(503, "HITL is unavailable")
        service = service_provider(session_id)
        if service is None:
            raise HTTPException(404, "workspace not bound to this session")
        decisions = decision_provider(session_id)
        if decisions is None:
            raise HTTPException(503, "HITL is unavailable")
        focus_question_id: str | None = None
        try:
            pending = decisions.get(decision_id)
            if pending is None:
                raise DecisionPointError(f"decision point {decision_id} does not exist")
            workspace_id = workspace_id_for(session_id)
            if pending.workspace_id != workspace_id:
                raise DecisionPointError("decision point does not belong to this workspace")
            if pending.kind.value == "research_direction" and request.approved:
                focus_question_id = request.question_id or (
                    pending.candidate_question_ids[0]
                    if pending.candidate_question_ids
                    else None
                )
                if focus_question_id:
                    workspace = service.get(workspace_id)
                    question = service.repository.get_subquestion(workspace_id, focus_question_id)
                    if workspace.status.value == "archived":
                        raise ValueError("archived workspaces cannot change the active focus")
                    if question is None:
                        raise ValueError("workspace or question not found")
                    if question.researchability is not Researchability.CANDIDATE:
                        raise ValueError("only candidate questions can become the active focus")
                    if question.status is SubQuestionStatus.DEPRIORITIZED:
                        raise ValueError("deprioritized questions cannot become the active focus")
        except (DecisionPointError, WorkspaceNotFoundError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            decision = decisions.resolve(
                decision_id,
                decision=request.decision,
                approved=request.approved,
                resolved_run_id=request.resolved_run_id,
                operation_id=request.operation_id,
            )
        except DecisionPointError as error:
            raise HTTPException(409, str(error)) from error
        if decision.kind.value == "research_direction" and request.approved:
            if focus_question_id:
                try:
                    service.set_active_focus(workspace_id_for(session_id), focus_question_id)
                except (WorkspaceNotFoundError, ValueError) as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
        return {"decision": asdict(decision)}

    @router.post("/{session_id}/focus")
    def set_workspace_focus(session_id: str, request: SetFocusRequest) -> dict[str, Any]:
        service = service_provider(session_id)
        if service is None:
            raise HTTPException(404, "workspace not bound to this session")
        try:
            workspace_id = workspace_id_for(session_id)
            service.set_active_focus(workspace_id, request.question_id)
            return project_workspace_state(
                service,
                workspace_id,
                decision_service=decision_provider(session_id) if decision_provider else None,
            )
        except WorkspaceNotFoundError as error:
            raise HTTPException(404, "workspace or question not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return router
