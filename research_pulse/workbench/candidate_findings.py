"""Candidate findings projected only from blocks actually read by one run."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from research_pulse.workbench.agent_runtime import SafeAgentEvent
from research_pulse.workbench.models import CandidateFinding


_REFERENCE = re.compile(r"\[([^\[\]\r\n]+)\]")


class FindingRepository(Protocol):
    def list_exploration_events(self, run_id: str) -> tuple[SafeAgentEvent, ...]: ...
    def insert_candidate_finding(self, finding: CandidateFinding) -> None: ...


class CandidateFindingService:
    def __init__(self, repository: FindingRepository) -> None:
        self.repository = repository

    def create(
        self,
        *,
        finding_id: str,
        run_id: str,
        claim: str,
        source_authority: Mapping[str, str],
    ) -> CandidateFinding:
        events = self.repository.list_exploration_events(run_id)
        reads = tuple(event for event in events if event.event_type == "context_read")
        read_block_ids = tuple(dict.fromkeys(
            event.stable_ids["block_id"] for event in reads if "block_id" in event.stable_ids
        ))
        source_ids = tuple(dict.fromkeys(
            event.stable_ids["source_id"] for event in reads if "source_id" in event.stable_ids
        ))
        if set(source_authority) != set(source_ids):
            raise ValueError("source authority must cover exactly the sources actually read")
        allowed = frozenset(read_block_ids)
        citation_statuses = {
            match.group(1): "resolved" if match.group(1) in allowed else "unresolved"
            for match in _REFERENCE.finditer(claim)
        }
        finding = CandidateFinding(
            finding_id=finding_id,
            run_id=run_id,
            claim=claim,
            source_ids=source_ids,
            read_block_ids=read_block_ids,
            source_authority=dict(source_authority),
            citation_statuses=citation_statuses,
        )
        self.repository.insert_candidate_finding(finding)
        return finding
