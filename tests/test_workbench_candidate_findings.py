from __future__ import annotations

import sqlite3
from unittest import TestCase

from research_pulse.workbench.candidate_findings import CandidateFindingService
from research_pulse.workbench.exploration import ExplorationService
from research_pulse.workbench.run_events import AttemptEventStore, UnsequencedEvent
from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sqlite import SQLiteWorkbenchRepository


class _Runtime:
    def cancel(self, run_id: str) -> None:
        pass


class WorkbenchCandidateFindingTests(TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.repository = SQLiteWorkbenchRepository(self.connection)
        sessions = SessionService(self.repository, session_id_factory=lambda: "s-1")
        sessions.create_empty()
        run = ExplorationService(
            self.repository, _Runtime(), run_id_factory=lambda: "run-1"
        ).create("s-1", "question", config_snapshot={}, budgets={})
        attempt = self.repository.list_coordinated_attempts(run.run_id)[-1]
        AttemptEventStore(self.repository).append(
            attempt.attempt_id,
            attempt.generation,
            UnsequencedEvent(
                "context_read",
                "读取受管证据块",
                {"stable_ids": {"source_id": "paper-1", "block_id": "block:read"}},
            ),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_unread_block_cannot_become_a_resolved_finding_citation(self) -> None:
        finding = CandidateFindingService(self.repository).create(
            finding_id="finding-1",
            run_id="run-1",
            claim="已读取 [block:read]，未读取 [block:unread]",
            source_authority={"paper-1": "original_research"},
        )

        self.assertEqual(finding.read_block_ids, ("block:read",))
        self.assertEqual(finding.citation_statuses, {
            "block:read": "resolved",
            "block:unread": "unresolved",
        })
        self.assertFalse(finding.is_formal_knowledge)
        self.assertEqual(self.repository.list_candidate_findings("run-1"), (finding,))

    def test_authority_must_match_sources_actually_read(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly"):
            CandidateFindingService(self.repository).create(
                finding_id="finding-2",
                run_id="run-1",
                claim="claim",
                source_authority={"paper-2": "secondary_discussion"},
            )
