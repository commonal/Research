from __future__ import annotations

from unittest import TestCase
import sqlite3
from datetime import UTC, datetime, timedelta

from research_pulse.workbench.continuation import (
    ContinuationBundle,
    ContinuationAssembler,
    OperationReference,
    StableEvidenceReference,
)
from research_pulse.workbench.run_coordinator import CreateRunCommand, ResearchRunCoordinator
from research_pulse.workbench.run_events import AttemptEventStore, UnsequencedEvent
from research_pulse.workbench.run_models import AttemptStatus
from research_pulse.workbench.sessions import ResearchSession
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository
from research_pulse.workbench.tool_execution import (
    Retryability, SideEffectState, ToolOutcome, ToolOutcomeStatus,
)


class WorkbenchContinuationContractTests(TestCase):
    def test_checkpoint_resume_requires_revision_settled_operations_and_resolvable_evidence(self) -> None:
        complete = ContinuationBundle(
            prior_attempt_id="attempt-1",
            persisted_event_ids=("event-1",),
            evidence=(StableEvidenceReference("block-1", read=True, resolvable=True),),
            workspace_revision="revision-7",
            operations=(OperationReference("operation-1", settled=True),),
        )
        self.assertTrue(complete.checkpoint_resume_allowed)

        missing_revision = complete.with_workspace_revision(None)
        unsettled = complete.with_operations((OperationReference("operation-1", settled=False),))
        missing_evidence = complete.with_evidence((
            StableEvidenceReference("block-1", read=True, resolvable=False),
        ))

        self.assertFalse(missing_revision.checkpoint_resume_allowed)
        self.assertFalse(unsettled.checkpoint_resume_allowed)
        self.assertFalse(missing_evidence.checkpoint_resume_allowed)

    def test_unread_candidate_is_inherited_but_never_enters_citation_allowlist(self) -> None:
        bundle = ContinuationBundle(
            prior_attempt_id="attempt-1",
            persisted_event_ids=("event-1",),
            evidence=(
                StableEvidenceReference("candidate-1", read=False, resolvable=True),
                StableEvidenceReference("block-1", read=True, resolvable=True),
            ),
            workspace_revision="revision-7",
            operations=(),
        )

        self.assertEqual(("candidate-1", "block-1"), bundle.inherited_evidence_ids)
        self.assertEqual(("block-1",), bundle.citation_allowlist)

    def test_coordinator_builds_reference_only_bundle_for_next_attempt(self) -> None:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(connection.close)
        repository = SQLiteWorkbenchRepository(connection)
        repository.create(ResearchSession(
            "session-1", "问题", "研究", datetime(2026, 9, 5, tzinfo=UTC)
        ))
        ids = iter(("attempt-1", "attempt-2"))
        coordinator = ResearchRunCoordinator(
            repository, run_id_factory=lambda: "run-1",
            attempt_id_factory=lambda: next(ids),
        )
        coordinator.create(CreateRunCommand(
            "session-1", "问题", {"profile": "literature"},
            {"model_rounds": 2, "tool_calls": 2, "block_reads": 2,
             "wall_seconds": 30, "input_tokens": 100, "output_tokens": 100},
        ))
        lease = repository.claim_next_attempt(
            "worker", now=datetime.now(UTC), lease_duration=timedelta(seconds=30)
        )
        events = AttemptEventStore(repository)
        events.append(lease.attempt_id, lease.generation, UnsequencedEvent(
            "source_discovered", "发现候选",
            {"stable_ids": {"source_id": "candidate-1"}},
        ))
        events.append(lease.attempt_id, lease.generation, UnsequencedEvent(
            "context_read", "读取证据",
            {"stable_ids": {"block_id": "block-1", "source_id": "paper-1"}},
        ))
        events.append(lease.attempt_id, lease.generation, UnsequencedEvent(
            "tool_completed", "只读工具 search_arxiv 已完成",
            {
                "stable_ids": {"tool_name": "search_arxiv"},
                "extras": [{
                    "arxiv_id": "2401.00001",
                    "source_id": "2401.00001",
                    "title": "Preference Optimization Survey",
                    "abstract_preview": "A bounded candidate preview.",
                    "relevance": 0.91,
                }],
            },
        ))
        now = datetime.now(UTC)
        repository.save_tool_outcome(ToolOutcome(
            "call-1", lease.attempt_id, "read_managed_blocks",
            ToolOutcomeStatus.SUCCEEDED, None, Retryability.NEVER,
            SideEffectState.NONE, now, now,
            bounded_result_reference="managed:block-1",
        ))
        repository.begin_coordinated_operation(
            "operation-1", lease.attempt_id,
            expected_generation=lease.generation, tool_call_id="call-write",
            resource_key="workspace:one", operation_kind="workspace_write",
        )
        repository.settle_coordinated_operation(
            "operation-1", lease.attempt_id,
            expected_generation=lease.generation, status="committed",
            receipt={"workspace_revision": 7, "patch_id": "patch-1"},
        )
        coordinator.record_attempt_outcome(
            lease.attempt_id, AttemptStatus.BUDGET_EXHAUSTED,
            expected_generation=lease.generation,
        )

        snapshot = coordinator.continue_run(
            "run-1", expected_attempt_id=lease.attempt_id,
            idempotency_key="continue-1",
        )
        bundle = snapshot.current_attempt.input_snapshot["continuation"]

        self.assertEqual("attempt-1", bundle["prior_attempt_id"])
        self.assertEqual(["candidate-1", "block-1"], [
            item["stable_id"] for item in bundle["evidence"]
        ])
        self.assertEqual("synthesize_from_persisted_evidence", bundle["recovery_strategy"])
        self.assertEqual("paper-1", bundle["evidence"][1]["source_id"])
        self.assertEqual("2401.00001", bundle["search_candidates"][0]["source_id"])
        self.assertEqual("A bounded candidate preview.", bundle["search_candidates"][0]["abstract_preview"])

        inherited_bundle = ContinuationAssembler(repository).assemble(
            "attempt-2", prior_status="budget_exhausted"
        ).to_recovery_facts()
        self.assertEqual(
            "2401.00001", inherited_bundle["search_candidates"][0]["source_id"]
        )
        self.assertEqual(["block-1"], bundle["citation_allowlist"])
        self.assertEqual("managed:block-1", bundle["tool_results"][0]["bounded_result_reference"])
        self.assertEqual("7", bundle["workspace_revision"])
        self.assertEqual("patch-1", bundle["operations"][0]["receipt"]["patch_id"])
        self.assertNotIn("ephemeral_value", repr(bundle))


if __name__ == "__main__":
    import unittest

    unittest.main()
