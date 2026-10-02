"""Framework-free domain contracts for the session paper workbench."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum


class StateTransitionError(ValueError):
    """Raised when a workbench aggregate attempts an illegal state change."""


class PdfStatus(StrEnum):
    ABSENT = "absent"
    FETCHING = "fetching"
    READY = "ready"
    FAILED = "failed"


class ParseStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class ExplorationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    AWAITING_USER_DECISION = "awaiting_user_decision"


class NoteRunStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    AWAITING_APPROVAL = "awaiting_approval"
    PUBLISHED = "published"
    FAILED = "failed"


_PDF_TRANSITIONS = {
    PdfStatus.ABSENT: {PdfStatus.FETCHING},
    PdfStatus.FETCHING: {PdfStatus.READY, PdfStatus.FAILED},
    PdfStatus.FAILED: {PdfStatus.FETCHING},
    PdfStatus.READY: set(),
}
_PARSE_TRANSITIONS = {
    ParseStatus.IDLE: {ParseStatus.QUEUED},
    ParseStatus.QUEUED: {ParseStatus.PARSING, ParseStatus.FAILED},
    ParseStatus.PARSING: {ParseStatus.READY, ParseStatus.FAILED},
    ParseStatus.FAILED: {ParseStatus.QUEUED},
    ParseStatus.READY: set(),
}
_MESSAGE_TRANSITIONS = {
    MessageStatus.QUEUED: {MessageStatus.GENERATING, MessageStatus.FAILED},
    MessageStatus.GENERATING: {MessageStatus.COMPLETED, MessageStatus.FAILED},
    MessageStatus.COMPLETED: set(),
    MessageStatus.FAILED: set(),
}
_EXPLORATION_TRANSITIONS = {
    ExplorationStatus.QUEUED: {
        ExplorationStatus.RUNNING,
        ExplorationStatus.CANCELLED,
        ExplorationStatus.FAILED,
    },
    ExplorationStatus.RUNNING: {
        ExplorationStatus.COMPLETED,
        ExplorationStatus.FAILED,
        ExplorationStatus.CANCELLED,
        ExplorationStatus.BUDGET_EXHAUSTED,
        ExplorationStatus.AWAITING_USER_DECISION,
    },
    ExplorationStatus.COMPLETED: set(),
    ExplorationStatus.FAILED: set(),
    ExplorationStatus.CANCELLED: set(),
    ExplorationStatus.BUDGET_EXHAUSTED: set(),
}
_NOTE_TRANSITIONS = {
    NoteRunStatus.QUEUED: {NoteRunStatus.GENERATING, NoteRunStatus.FAILED},
    NoteRunStatus.GENERATING: {NoteRunStatus.AWAITING_APPROVAL, NoteRunStatus.FAILED},
    NoteRunStatus.AWAITING_APPROVAL: {NoteRunStatus.PUBLISHED, NoteRunStatus.FAILED},
    NoteRunStatus.PUBLISHED: set(),
    NoteRunStatus.FAILED: {NoteRunStatus.QUEUED},
}


def _ensure_transition(current: StrEnum, target: StrEnum, allowed: dict) -> None:
    if target not in allowed[current]:
        raise StateTransitionError(f"illegal transition: {current.value} -> {target.value}")


@dataclass(frozen=True)
class Paper:
    paper_id: str
    source_identity: str
    title: str | None = None
    pdf_status: PdfStatus = PdfStatus.ABSENT
    parse_status: ParseStatus = ParseStatus.IDLE
    source_url: str | None = None
    pdf_path: str | None = None
    material_root: str | None = None
    safe_error: str | None = None

    def transition_pdf(self, status: PdfStatus) -> "Paper":
        _ensure_transition(self.pdf_status, status, _PDF_TRANSITIONS)
        return replace(self, pdf_status=status)

    def transition_parse(self, status: ParseStatus) -> "Paper":
        _ensure_transition(self.parse_status, status, _PARSE_TRANSITIONS)
        return replace(self, parse_status=status)


@dataclass(frozen=True)
class SessionPaperLink:
    session_id: str
    paper_id: str


@dataclass(frozen=True)
class Message:
    message_id: str
    session_id: str
    role: str
    text: str
    status: MessageStatus
    scope: str = "none"

    def transition(self, status: MessageStatus) -> "Message":
        _ensure_transition(self.status, status, _MESSAGE_TRANSITIONS)
        return replace(self, status=status)


@dataclass(frozen=True)
class ExplorationRun:
    run_id: str
    session_id: str
    question_snapshot: str
    attempt: int
    status: ExplorationStatus
    config_snapshot: dict[str, object] = field(default_factory=dict)
    budgets: dict[str, int] = field(default_factory=dict)
    previous_run_id: str | None = None
    safe_error: str | None = None
    final_draft: str | None = None
    created_at: datetime | None = None
    # M3.6 HITL resume: a run resumed after a DecisionPoint carries the decision
    # it resolves; ``previous_run_id`` doubles as ``resumes_from``.
    decision_id: str | None = None

    def transition(self, status: ExplorationStatus) -> "ExplorationRun":
        _ensure_transition(self.status, status, _EXPLORATION_TRANSITIONS)
        return replace(self, status=status)


@dataclass(frozen=True)
class CandidateFinding:
    finding_id: str
    run_id: str
    claim: str
    source_ids: tuple[str, ...] = ()
    read_block_ids: tuple[str, ...] = ()
    source_authority: dict[str, str] = field(default_factory=dict)
    citation_statuses: dict[str, str] = field(default_factory=dict)
    is_formal_knowledge: bool = field(default=False, init=False)


METHOD_MAP_LABEL = "当前证据范围内的初始地图"


@dataclass(frozen=True)
class ExplorationArtifact:
    final_draft: str
    candidate_questions: tuple[str, ...] | None = None
    method_routes: tuple[str, ...] = ()
    draft_label: str = field(default="探索草稿/待验证", init=False)

    def __post_init__(self) -> None:
        if not self.final_draft.strip():
            raise ValueError("completed exploration requires a final draft")
        if self.candidate_questions is not None and any(
            not question.strip() for question in self.candidate_questions
        ):
            raise ValueError("candidate questions must not contain blanks")

    def public_dict(self) -> dict[str, object]:
        return {
            "draft_label": self.draft_label,
            "final_draft": self.final_draft,
            "candidate_questions": (
                list(self.candidate_questions) if self.candidate_questions is not None else None
            ),
            "method_map": {
                "label": METHOD_MAP_LABEL,
                "routes": list(self.method_routes),
            } if self.method_routes else None,
        }


@dataclass(frozen=True)
class NoteRun:
    note_run_id: str
    paper_id: str
    triggering_session_id: str
    status: NoteRunStatus
    attempt: int = 1
    previous_note_run_id: str | None = None
    receipt_path: str | None = None
    draft_path: str | None = None
    knowledge_id: str | None = None
    safe_error: str | None = None
    stage: str = "queued"
    created_at: str | None = None

    def transition(self, status: NoteRunStatus) -> "NoteRun":
        _ensure_transition(self.status, status, _NOTE_TRANSITIONS)
        return replace(self, status=status)
