"""In-memory WorkspaceRepository for workbench gate/risk/commit tests.

Not collected by pytest (does not match ``test_*.py``); imported explicitly by
the M3 test modules.
"""

from __future__ import annotations

from research_pulse.workbench.workspace import (
    ProjectedWorkspaceState,
    ResearchMapNode,
    SubQuestion,
    Workspace,
)


class MemoryWorkspaceStore:
    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._subquestions: dict[str, dict[str, SubQuestion]] = {}
        self._evidence: dict[str, dict[str, object]] = {}
        self._research_map: dict[str, dict[str, ResearchMapNode]] = {}

    def create(self, workspace: Workspace) -> None:
        self._workspaces[workspace.workspace_id] = workspace
        self._subquestions.setdefault(workspace.workspace_id, {})
        self._evidence.setdefault(workspace.workspace_id, {})
        self._research_map.setdefault(workspace.workspace_id, {})

    def get(self, workspace_id: str) -> Workspace | None:
        return self._workspaces.get(workspace_id)

    def list(self) -> tuple[Workspace, ...]:
        return tuple(self._workspaces.values())

    def save(self, workspace: Workspace) -> None:
        self._workspaces[workspace.workspace_id] = workspace

    def apply_commit(self, workspace_id: str, projected: ProjectedWorkspaceState) -> None:
        current = self._workspaces[workspace_id]
        self._workspaces[workspace_id] = Workspace(
            workspace_id=current.workspace_id,
            research_question=current.research_question,
            anchor_paper_id=current.anchor_paper_id,
            research_intent=current.research_intent,
            active_focus_id=current.active_focus_id,
            status=current.status,
            workspace_revision=projected.workspace_revision,
            created_at=current.created_at,
            research_plan=(
                projected.research_plan
                if projected.research_plan is not None
                else current.research_plan
            ),
        )
        self._subquestions[workspace_id] = {q.question_id: q for q in projected.subquestions}
        self._evidence[workspace_id] = {e.evidence_id: e for e in projected.evidence}
        self._research_map[workspace_id] = {n.node_id: n for n in projected.research_map}

    def list_subquestions(self, workspace_id: str) -> tuple[SubQuestion, ...]:
        return tuple(self._subquestions.get(workspace_id, {}).values())

    def get_subquestion(self, workspace_id: str, question_id: str) -> SubQuestion | None:
        return self._subquestions.get(workspace_id, {}).get(question_id)

    def upsert_subquestion(self, workspace_id: str, question: SubQuestion) -> None:
        self._subquestions.setdefault(workspace_id, {})[question.question_id] = question

    def list_evidence(self, workspace_id: str) -> tuple:
        return tuple(self._evidence.get(workspace_id, {}).values())

    def upsert_evidence(self, workspace_id: str, evidence) -> None:
        self._evidence.setdefault(workspace_id, {})[evidence.evidence_id] = evidence

    def list_research_map(self, workspace_id: str) -> tuple[ResearchMapNode, ...]:
        return tuple(self._research_map.get(workspace_id, {}).values())

    def get_research_map_node(self, workspace_id: str, node_id: str) -> ResearchMapNode | None:
        return self._research_map.get(workspace_id, {}).get(node_id)

    def upsert_research_map_node(self, workspace_id: str, node: ResearchMapNode) -> None:
        self._research_map.setdefault(workspace_id, {})[node.node_id] = node
