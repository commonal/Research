"""M3.3 — global workspace_revision + CommitService atomic commit.

A commit validates every canonical JSON it touches, writes them all in one
atomic batch, and bumps the global ``workspace_revision`` exactly once. The
optimistic-lock test proves a stale patch is rejected and the on-disk state left
untouched; the cross-file test proves all files reflect the commit consistently.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import sleep
from unittest import TestCase

from tests._workspace_store import MemoryWorkspaceStore

from research_pulse.workbench.workspace import Workspace
from research_pulse.workbench.workspace_commit import CommitService
from research_pulse.workbench.workspace_gate import (
    EVIDENCE_ADD,
    RESEARCH_MAP_ADD,
    SUBQUESTION_ADD,
    WorkspaceGateError,
    WorkspacePatch,
    WorkspacePatchOperation,
)
from research_pulse.workbench.workspace_json import WorkspaceJsonStore
from research_pulse.workbench.agent_runtime import RunBudgets
from research_pulse.workbench.budget_enforcer import BudgetLedger
from research_pulse.workbench.tool_dispatcher import (
    Idempotency,
    ParallelPolicy,
    ToolDispatcher,
    ToolEffect,
    ToolPolicy,
    ToolPolicyRegistry,
)


def _patch(*operations: WorkspacePatchOperation, base: int = 1) -> WorkspacePatch:
    return WorkspacePatch(base_workspace_revision=base, operations=tuple(operations), provenance="agent:run-1")


def _seed(store, workspace_id: str = "w1") -> Workspace:
    workspace = Workspace(
        workspace_id=workspace_id,
        research_question="How should shadow variables address MNAR?",
        anchor_paper_id="paper-1",
    )
    store.create(workspace)
    return workspace


class CommitServiceMemoryTests(TestCase):
    def setUp(self) -> None:
        self.store = MemoryWorkspaceStore()
        self.workspace = _seed(self.store)
        self.commit = CommitService(self.store)

    def test_valid_commit_bumps_revision_once(self) -> None:
        patch = _patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="sub"),
            WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", source_id="p2", block_ids=("b1",)),
        )
        projected = self.commit.commit(self.workspace.workspace_id, patch)
        self.assertEqual(projected.workspace_revision, 2)
        stored = self.store.get(self.workspace.workspace_id)
        self.assertEqual(stored.workspace_revision, 2)

    def test_stale_base_is_rejected_and_not_applied(self) -> None:
        first = _patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="sub"),
        )
        self.commit.commit(self.workspace.workspace_id, first)
        # A second patch authored against the now-stale revision 1 must be refused.
        stale = _patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-003", text="other"),
            base=1,
        )
        with self.assertRaises(WorkspaceGateError):
            self.commit.commit(self.workspace.workspace_id, stale)
        # The store is unchanged by the rejected commit: still at revision 2 and no Q-003.
        stored = self.store.get(self.workspace.workspace_id)
        self.assertEqual(stored.workspace_revision, 2)
        self.assertNotIn("Q-003", {q.question_id for q in self.store.list_subquestions(self.workspace.workspace_id)})

    def test_concurrent_same_revision_writes_commit_once_without_lost_update(self) -> None:
        patches = (
            _patch(WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="first")),
            _patch(WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-003", text="second")),
        )

        def commit(patch):
            try:
                return ("committed", self.commit.commit(self.workspace.workspace_id, patch))
            except WorkspaceGateError:
                return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(commit, patches))

        self.assertEqual(["committed", "conflict"], sorted(item[0] for item in results))
        stored = self.store.get(self.workspace.workspace_id)
        self.assertEqual(2, stored.workspace_revision)
        subquestions = self.store.list_subquestions(self.workspace.workspace_id)
        self.assertEqual(1, len(subquestions))

    def test_dispatcher_serializes_same_resource_and_does_not_retry_stale_patch(self) -> None:
        patches = {
            "first": _patch(WorkspacePatchOperation(
                kind=SUBQUESTION_ADD, object_id="Q-002", text="first",
            )),
            "second": _patch(WorkspacePatchOperation(
                kind=SUBQUESTION_ADD, object_id="Q-003", text="second",
            )),
        }
        calls: list[str] = []
        active = 0
        maximum = 0
        guard = Lock()

        def commit_patch(*, patch_id: str):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
                calls.append(patch_id)
            try:
                sleep(0.02)
                projected = self.commit.commit(
                    self.workspace.workspace_id, patches[patch_id]
                )
                return {
                    "patch_id": patch_id,
                    "workspace_revision": projected.workspace_revision,
                }
            finally:
                with guard:
                    active -= 1

        registry = ToolPolicyRegistry()
        registry.register("update_subquestions", commit_patch, policy=ToolPolicy(
            ToolEffect.WRITE,
            Idempotency.NON_IDEMPOTENT,
            1,
            2,
            lambda _args: (f"workspace:{self.workspace.workspace_id}",),
            ParallelPolicy.SERIAL,
        ))
        dispatcher = ToolDispatcher(registry)

        def execute(patch_id: str):
            return dispatcher.dispatch_with_recovery(
                attempt_id="attempt-1",
                tool_call_id=f"call-{patch_id}",
                tool_name="update_subquestions",
                arguments={"patch_id": patch_id},
                budget_ledger=BudgetLedger(
                    RunBudgets(2, 4, 2, 10, 100, 20), recovery_limit=2,
                    attempt_id="attempt-1",
                ),
                sleeper=lambda _seconds: None,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(execute, patches))

        self.assertEqual(1, maximum)
        self.assertEqual(2, len(calls))
        succeeded = next(item for item in outcomes if item.status.value == "succeeded")
        conflict = next(item for item in outcomes if item.status.value == "rejected")
        self.assertIsNone(succeeded.error_code)
        self.assertEqual("revision_conflict", conflict.error_code.value)
        self.assertEqual("after_replan", conflict.retryability.value)
        self.assertEqual("not_applied", conflict.side_effect_state.value)
        self.assertEqual(2, self.store.get(self.workspace.workspace_id).workspace_revision)
        self.assertEqual(1, len(self.store.list_subquestions(self.workspace.workspace_id)))

    def test_integrity_violation_writes_nothing(self) -> None:
        # Evidence missing source_id must be refused before any write.
        patch = _patch(
            WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", block_ids=("b1",)),
        )
        with self.assertRaises(WorkspaceGateError):
            self.commit.commit(self.workspace.workspace_id, patch)
        self.assertEqual(self.store.get(self.workspace.workspace_id).workspace_revision, 1)
        self.assertEqual(self.store.list_evidence(self.workspace.workspace_id), ())


class CommitServiceCrossFileTests(TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.store = WorkspaceJsonStore(str(self.root))
        workspace = Workspace(
            workspace_id="ws-1",
            research_question="question?",
            anchor_paper_id="paper-1",
        )
        self.store.create(workspace)
        self.commit = CommitService(self.store)
        self.base = self.root / "ws-1"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_commit_writes_all_files_and_bumps_revision_consistently(self) -> None:
        patch = _patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="sub"),
            WorkspacePatchOperation(kind=EVIDENCE_ADD, object_id="E-001", source_id="p2", block_ids=("b1", "b2")),
            WorkspacePatchOperation(kind=RESEARCH_MAP_ADD, object_id="M-001", related_question_ids=("Q-002",), evidence_ids=("E-001",)),
        )
        projected = self.commit.commit("ws-1", patch)
        self.assertEqual(projected.workspace_revision, 2)

        workspace = json.loads((self.base / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(workspace["workspace_revision"], 2)

        subquestions = json.loads((self.base / "subquestions.json").read_text(encoding="utf-8"))
        ids = {item["question_id"] for item in subquestions["items"]}
        self.assertEqual(ids, {"Q-002"})

        research_map = json.loads((self.base / "research-map.json").read_text(encoding="utf-8"))
        self.assertEqual(research_map["nodes"][0]["node_id"], "M-001")

        evidence_files = list((self.base / "evidence").glob("*.json"))
        self.assertEqual({p.stem for p in evidence_files}, {"E-001"})
        evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
        self.assertEqual(evidence["block_ids"], ["b1", "b2"])

    def test_commit_preserves_workspace_meta(self) -> None:
        patch = _patch(WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="sub"))
        self.commit.commit("ws-1", patch)
        workspace = json.loads((self.base / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(workspace["research_question"], "question?")
        self.assertEqual(workspace["anchor_paper_id"], "paper-1")
        self.assertEqual(workspace["status"], "created")

    def test_stale_base_on_disk_is_rejected_and_untouched(self) -> None:
        first = _patch(WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-002", text="sub"))
        self.commit.commit("ws-1", first)
        snapshot_before = (self.base / "subquestions.json").read_text(encoding="utf-8")

        stale = _patch(
            WorkspacePatchOperation(kind=SUBQUESTION_ADD, object_id="Q-999", text="zombie"),
            base=1,
        )
        with self.assertRaises(WorkspaceGateError):
            self.commit.commit("ws-1", stale)

        self.assertEqual((self.base / "subquestions.json").read_text(encoding="utf-8"), snapshot_before)
        workspace = json.loads((self.base / "workspace.json").read_text(encoding="utf-8"))
        self.assertEqual(workspace["workspace_revision"], 2)
        sub_ids = {i["question_id"] for i in json.loads(
            (self.base / "subquestions.json").read_text(encoding="utf-8"))["items"]}
        self.assertNotIn("Q-999", sub_ids)


if __name__ == "__main__":
    import unittest

    unittest.main()
