from __future__ import annotations

from datetime import UTC, datetime
from unittest import TestCase

from research_pulse.workbench.sessions import SessionService
from research_pulse.workbench.sessions import SessionLifecycle, SessionNotFoundError
from research_pulse.workbench.models import (
    CandidateFinding,
    ExplorationRun,
    ExplorationStatus,
    Message,
    MessageStatus,
    NoteRun,
    NoteRunStatus,
    Paper,
    ParseStatus,
    PdfStatus,
    SessionPaperLink,
    StateTransitionError,
)


NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


class _MemorySessionRepository:
    def __init__(self) -> None:
        self.sessions = {}
        self.paper_links: dict[str, list[str]] = {}
        self.papers = {"paper-1": {"paper_id": "paper-1"}}

    def create(self, session):
        self.sessions[session.session_id] = session

    def get(self, session_id: str):
        return self.sessions.get(session_id)

    def list(self, lifecycle=None):
        sessions = tuple(self.sessions.values())
        if lifecycle is not None:
            sessions = tuple(item for item in sessions if item.lifecycle == lifecycle)
        return sessions

    def save(self, session):
        self.sessions[session.session_id] = session

    def link_paper(self, session_id: str, paper_id: str) -> None:
        self.paper_links.setdefault(session_id, []).append(paper_id)

    def list_paper_ids(self, session_id: str) -> tuple[str, ...]:
        return tuple(self.paper_links.get(session_id, ()))

    def delete(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        self.paper_links.pop(session_id, None)


class SessionServiceTests(TestCase):
    def test_question_first_session_has_stable_id_question_and_title(self) -> None:
        repository = _MemorySessionRepository()
        service = SessionService(
            repository,
            session_id_factory=lambda: "session-123",
            clock=lambda: NOW,
        )

        created = service.create_from_question("  如何减少推荐系统的位置偏差？  ")
        restored = service.get("session-123")

        self.assertEqual(created.session_id, "session-123")
        self.assertEqual(restored, created)
        self.assertEqual(created.research_question, "如何减少推荐系统的位置偏差？")
        self.assertEqual(created.title, "如何减少推荐系统的位置偏差？")
        self.assertEqual(created.created_at, NOW)

    def test_paper_first_session_links_reusable_paper_without_a_question(self) -> None:
        repository = _MemorySessionRepository()
        service = SessionService(
            repository,
            session_id_factory=lambda: "session-paper",
            clock=lambda: NOW,
        )

        created = service.create_from_paper("paper-1")

        self.assertEqual(created.session_id, "session-paper")
        self.assertIsNone(created.research_question)
        self.assertEqual(created.title, "新会话")
        self.assertEqual(repository.paper_links[created.session_id], ["paper-1"])
        self.assertIn("paper-1", repository.papers)

    def test_find_active_for_paper_returns_newest_matching_session(self) -> None:
        repository = _MemorySessionRepository()
        ids = iter(("session-old", "session-new", "session-other"))
        service = SessionService(
            repository,
            session_id_factory=lambda: next(ids),
            clock=lambda: NOW,
        )

        old = service.create_from_paper("paper-1")
        service.clock = lambda: NOW.replace(minute=1)
        new = service.create_from_paper("paper-1")
        other = service.create_empty()
        service.attach_paper(other.session_id, "paper-1")
        service.archive(other.session_id)

        found = service.find_active_for_paper("paper-1")

        self.assertEqual(found, new)
        self.assertNotEqual(found, old)
        self.assertIsNone(service.find_active_for_paper("paper-1", "missing-workspace"))

    def test_workspace_holds_multiple_shared_papers(self) -> None:
        repository = _MemorySessionRepository()
        repository.papers["paper-2"] = {"paper_id": "paper-2"}
        service = SessionService(
            repository,
            session_id_factory=lambda: "session-one-paper",
            clock=lambda: NOW,
        )
        created = service.create_from_paper("paper-1")

        # With the workspace model, a research project (workspace) holds several
        # shared papers; the "one active paper per session" V1 cap is removed.
        service.attach_paper(created.session_id, "paper-2")
        self.assertEqual(repository.paper_links[created.session_id], ["paper-1", "paper-2"])

    def test_opening_paper_panel_defaults_to_first_attached_paper(self) -> None:
        repository = _MemorySessionRepository()
        service = SessionService(
            repository,
            session_id_factory=lambda: "session-paper-panel",
            clock=lambda: NOW,
        )
        created = service.create_empty()
        service.attach_paper(created.session_id, "paper-1")

        opened = service.update_layout(created.session_id, paper_panel_open=True)

        self.assertTrue(opened.paper_panel_open)
        self.assertEqual("paper-1", opened.active_paper_id)


class WorkbenchDomainModelTests(TestCase):
    def test_all_run_and_material_states_reject_illegal_transitions(self) -> None:
        paper = Paper(
            paper_id="paper-1",
            source_identity="sha256:abc",
            pdf_status=PdfStatus.ABSENT,
            parse_status=ParseStatus.IDLE,
        )
        message = Message(
            message_id="message-1",
            session_id="session-1",
            role="assistant",
            text="",
            status=MessageStatus.QUEUED,
        )
        exploration = ExplorationRun(
            run_id="run-1",
            session_id="session-1",
            question_snapshot="如何比较方法？",
            attempt=1,
            status=ExplorationStatus.QUEUED,
        )
        note = NoteRun(
            note_run_id="note-1",
            paper_id="paper-1",
            triggering_session_id="session-1",
            status=NoteRunStatus.QUEUED,
        )

        with self.assertRaises(StateTransitionError):
            paper.transition_pdf(PdfStatus.READY)
        with self.assertRaises(StateTransitionError):
            message.transition(MessageStatus.COMPLETED)
        with self.assertRaises(StateTransitionError):
            exploration.transition(ExplorationStatus.COMPLETED)
        with self.assertRaises(StateTransitionError):
            note.transition(NoteRunStatus.PUBLISHED)

    def test_domain_objects_keep_identity_and_candidate_is_not_knowledge(self) -> None:
        link = SessionPaperLink(session_id="session-1", paper_id="paper-1")
        finding = CandidateFinding(
            finding_id="finding-1",
            run_id="run-1",
            claim="候选结论",
            source_ids=("paper-1",),
            read_block_ids=("B-1",),
        )

        self.assertEqual(link.paper_id, "paper-1")
        self.assertFalse(finding.is_formal_knowledge)

    def test_session_can_be_renamed_archived_restored_and_deleted_without_deleting_paper(self) -> None:
        repository = _MemorySessionRepository()
        service = SessionService(
            repository,
            session_id_factory=lambda: "session-lifecycle",
            clock=lambda: NOW,
        )
        created = service.create_from_paper("paper-1")

        renamed = service.rename(created.session_id, "  DPO 去偏研究  ")
        archived = service.archive(created.session_id)
        restored = service.restore(created.session_id)
        service.delete(created.session_id)

        self.assertEqual(renamed.title, "DPO 去偏研究")
        self.assertEqual(archived.lifecycle, SessionLifecycle.ARCHIVED)
        self.assertEqual(restored.lifecycle, SessionLifecycle.ACTIVE)
        with self.assertRaises(SessionNotFoundError):
            service.get(created.session_id)
        self.assertIn("paper-1", repository.papers)
