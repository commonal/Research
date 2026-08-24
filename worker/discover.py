"""Fetch and normalize paper candidates from the arXiv Atom API."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import re
import xml.etree.ElementTree as ET


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class Subscription:
    """The small, explicit contract for one research direction."""

    name: str
    query: str
    exclude_terms: tuple[str, ...]
    daily_limit: int
    max_results: int
    source: str

    @classmethod
    def from_file(cls, path: Path) -> "Subscription":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("source") != "arxiv":
            raise ValueError("This first worker only supports source='arxiv'.")
        if not payload.get("name") or not payload.get("query"):
            raise ValueError("A subscription needs non-empty 'name' and 'query'.")

        return cls(
            name=str(payload["name"]),
            query=str(payload["query"]),
            exclude_terms=tuple(str(item).lower() for item in payload.get("exclude_terms", [])),
            daily_limit=_positive_int(payload.get("daily_limit", 3), "daily_limit"),
            max_results=_positive_int(payload.get("max_results", 10), "max_results"),
            source="arxiv",
        )


@dataclass(frozen=True)
class PaperCandidate:
    """A source-backed metadata record. It intentionally excludes PDF bytes."""

    source: str
    source_id: str
    title: str
    authors: list[str]
    published_at: str
    updated_at: str
    abstract: str
    source_url: str
    categories: list[str]


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def build_query_url(subscription: Subscription) -> str:
    """Build a deterministic newest-first arXiv query URL."""

    parameters = {
        "search_query": f'all:"{subscription.query}"',
        "start": 0,
        "max_results": subscription.max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return f"{ARXIV_API_URL}?{urlencode(parameters)}"


def fetch_candidates(subscription: Subscription, timeout_seconds: int = 30) -> list[PaperCandidate]:
    """Download one Atom feed, parse it, and apply transparent local exclusions."""

    request = Request(
        build_query_url(subscription),
        headers={"User-Agent": "ResearchPulse/0.1 (personal research feed)"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()

    candidates = parse_arxiv_feed(payload)
    return [
        candidate
        for candidate in candidates
        if not _contains_excluded_term(candidate, subscription.exclude_terms)
    ]


def parse_arxiv_feed(payload: bytes) -> list[PaperCandidate]:
    """Convert arXiv Atom XML to source-backed, JSON-safe candidates."""

    root = ET.fromstring(payload)
    candidates: list[PaperCandidate] = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = _required_text(entry, f"{ATOM}id")
        source_id = raw_id.rsplit("/", maxsplit=1)[-1]
        source_url = _canonical_url(entry, raw_id)
        candidates.append(
            PaperCandidate(
                source="arxiv",
                source_id=source_id,
                title=_normalized_text(_required_text(entry, f"{ATOM}title")),
                authors=[
                    _normalized_text(_required_text(author, f"{ATOM}name"))
                    for author in entry.findall(f"{ATOM}author")
                ],
                published_at=_required_text(entry, f"{ATOM}published"),
                updated_at=_required_text(entry, f"{ATOM}updated"),
                abstract=_normalized_text(_required_text(entry, f"{ATOM}summary")),
                source_url=source_url,
                categories=[category.attrib["term"] for category in entry.findall(f"{ATOM}category")],
            )
        )
    return candidates


def write_candidates(subscription: Subscription, candidates: list[PaperCandidate], output_path: Path) -> None:
    """Persist a reviewable run receipt; knowledge Markdown is not created here."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subscription": asdict(subscription),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _canonical_url(entry: ET.Element, fallback: str) -> str:
    for link in entry.findall(f"{ATOM}link"):
        if link.attrib.get("rel", "alternate") == "alternate":
            return link.attrib["href"]
    return fallback


def _contains_excluded_term(candidate: PaperCandidate, exclude_terms: tuple[str, ...]) -> bool:
    haystack = f"{candidate.title}\n{candidate.abstract}".lower()
    return any(term in haystack for term in exclude_terms)


def _required_text(element: ET.Element, path: str) -> str:
    child = element.find(path)
    if child is None or child.text is None:
        raise ValueError(f"Malformed arXiv feed: missing {path}.")
    return child.text.strip()


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
