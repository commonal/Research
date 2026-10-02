"""Strict citation extraction and projection over a per-turn block allowlist."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Sequence

from research_pulse.workbench.paper_access import PaperAccessService


_BRACKETED_REFERENCE = re.compile(r"[\[［(（]([^\[\]\r\n]+)[\]］)）]")
_MANAGED_REFERENCE = re.compile(
    r"^(?:normalized:[^:\s\]]+:(?:text|figure|formula|table):[^\s\]]+|(?:text|figure|formula|table):[^\s\]]+)$"
)
_MANAGED_REFERENCE_SEARCH = re.compile(
    r"(?<![A-Za-z0-9_])`?((?:normalized:[^:\s\[\]\(\)（）,，、;；]+:(?:text|figure|formula|table):[^\s\[\]\(\)（）,，、;；`]+|(?:text|figure|formula|table):[^\s\[\]\(\)（）,，、;；`]+))`?(?![A-Za-z0-9_])"
)


@dataclass(frozen=True)
class CitationRecord:
    citation_id: str
    message_id: str
    paper_id: str | None
    block_id: str
    status: str = "unresolved"
    locator: dict[str, Any] | None = None


def extract_citations(
    text: str, *, message_id: str, paper_id: str | None
) -> tuple[CitationRecord, ...]:
    """Extract managed block ids from common model citation wrappers.

    Models use square brackets in prompts but often emit Markdown code ticks or
    Chinese/ASCII parentheses in the final answer. We accept those wrappers
    without fuzzy matching: only an exact managed block-id shape is extracted,
    and projection still enforces the per-turn evidence allowlist.
    """
    matches = list(
        match for match in _BRACKETED_REFERENCE.finditer(text)
        if _MANAGED_REFERENCE.fullmatch(match.group(1).strip())
    )
    # A parenthesized group may contain several ids separated by Chinese
    # punctuation. The group above deliberately keeps mixed prose untouched;
    # this scanner covers each exact id in such groups and in standalone
    # backtick-wrapped references.
    seen_spans = {(match.start(1), match.end(1)) for match in matches}
    for match in _MANAGED_REFERENCE_SEARCH.finditer(text):
        if (match.start(1), match.end(1)) not in seen_spans:
            matches.append(match)
    matches.sort(key=lambda match: match.start(1))
    return tuple(
        CitationRecord(
            citation_id=f"citation:{message_id}:{index}",
            message_id=message_id,
            paper_id=paper_id,
            block_id=match.group(1),
        )
        for index, match in enumerate(matches)
    )


def project_citations(
    citations: Sequence[CitationRecord], *, allowlist: Sequence[str],
    session_id: str, paper_access: PaperAccessService,
) -> tuple[CitationRecord, ...]:
    allowed = frozenset(allowlist)
    projected: list[CitationRecord] = []
    for citation in citations:
        if citation.paper_id is None or citation.block_id not in allowed:
            projected.append(replace(citation, status="unresolved", locator=None))
            continue
        locator = paper_access.block_locator(
            session_id, citation.paper_id, citation.block_id
        )
        if locator.status != "resolved":
            projected.append(replace(citation, status=locator.status, locator=None))
            continue
        projected.append(replace(
            citation,
            status="resolved",
            locator={
                "page": locator.page,
                "bbox": list(locator.bbox) if locator.bbox is not None else None,
                "section_path": list(locator.section_path),
                "bbox_space": locator.bbox_space,
                **({"bbox_dimensions": list(locator.bbox_dimensions)} if locator.bbox_dimensions is not None else {}),
            },
        ))
    return tuple(projected)
