"""Fail-closed read-only research tool facade over frozen managed materials."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from research_pulse.workbench.harness_v0_fixture import load_and_validate_fixture
from research_pulse.workbench.web_retrieval import (
    WebRetrievalError,
    WebRetrievalUnavailable,
    WebSearchResponse,
    WebSearchProvider,
    normalize_web_results,
)


class ToolPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedSource:
    source_id: str
    title: str
    source_url: str
    blocks_path: Path
    source_authority: str = "original_research"
    representation: str = "structured_blocks"


_AUTHORITY_RANK = {
    "original_research": 0,
    "official_data": 1,
    "official_documentation": 2,
    "secondary_discussion": 3,
    "unknown": 4,
}


def rank_sources(
    scored_sources: Sequence[tuple[int, ManagedSource]],
    *,
    representation_preference: Sequence[str] = ("structured_blocks", "html", "pdf", "summary"),
) -> list[ManagedSource]:
    """Rank relevance, authority, and representation as separate dimensions.

    Representation is deliberately only a tie-breaker after evidence authority,
    so an easier-to-read secondary page cannot outrank the original research.
    """
    preference = {name: index for index, name in enumerate(representation_preference)}
    return [
        source for _, source in sorted(
            scored_sources,
            key=lambda item: (
                -item[0],
                _AUTHORITY_RANK.get(item[1].source_authority, _AUTHORITY_RANK["unknown"]),
                preference.get(item[1].representation, len(preference)),
                item[1].source_id,
            ),
        )
    ]


_CAPABILITIES = (
    "search_sources", "read_paper_metadata", "read_managed_blocks", "read_run_status",
    "search_arxiv",
)
_FORBIDDEN_ARGUMENT_KEYS = frozenset({
    "path", "file", "filename", "directory", "command", "code", "payload",
    "todo", "task", "agent", "subagent", "destination",
})
_STATUS_FIELDS = frozenset({
    "run_id", "status", "model_rounds", "tool_calls", "block_reads",
    "elapsed_seconds", "input_tokens", "output_tokens", "stop_reason",
})


class ReadOnlyResearchTools:
    def __init__(
        self,
        sources: Sequence[ManagedSource],
        *,
        run_id: str,
        status_reader: Callable[[str], Mapping[str, object]],
        arxiv_client: Any = None,
        web_search_provider: WebSearchProvider | None = None,
    ) -> None:
        if not run_id.strip():
            raise ValueError("run id must not be blank")
        self._sources = {source.source_id: source for source in sources}
        self._run_id = run_id
        self._status_reader = status_reader
        self._arxiv = arxiv_client
        self._web_search = web_search_provider
        self._last_web_search_usage: dict[str, object] | None = None

    @property
    def last_web_search_usage(self) -> dict[str, object] | None:
        """Safe metadata for the most recent web search in this run."""
        return dict(self._last_web_search_usage) if self._last_web_search_usage else None

    @classmethod
    def from_fixture(
        cls,
        manifest_path: str | Path,
        *,
        run_id: str,
        status_reader: Callable[[str], Mapping[str, object]],
        project_root: str | Path | None = None,
        web_search_provider: WebSearchProvider | None = None,
    ) -> "ReadOnlyResearchTools":
        manifest = Path(manifest_path).resolve()
        root = Path(project_root).resolve() if project_root else manifest.parents[2]
        fixture = load_and_validate_fixture(manifest, project_root=root)
        sources = tuple(
            ManagedSource(
                source_id=item["source_id"],
                title=item["title"],
                source_url=item["source_url"],
                blocks_path=(root / item["blocks_path"]).resolve(),
                source_authority=str(item.get("source_authority", "original_research")),
                representation=str(item.get("representation", "structured_blocks")),
            )
            for item in fixture["materials"]
        )
        return cls(
            sources,
            run_id=run_id,
            status_reader=status_reader,
            web_search_provider=web_search_provider,
        )

    @property
    def capability_names(self) -> tuple[str, ...]:
        return _CAPABILITIES + (("search_web",) if self._web_search is not None else ())

    def invoke(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        # Keep the optional entry point diagnosable when called directly, while
        # omitting it from the declared capability surface when no provider is
        # configured (so preflight can fail before a model call).
        if tool_name not in self.capability_names and tool_name != "search_web":
            raise ToolPolicyError("tool is not allowed")
        lowered = {str(key).lower() for key in arguments}
        if lowered & _FORBIDDEN_ARGUMENT_KEYS:
            raise ToolPolicyError("unsafe tool argument")
        if tool_name == "search_sources":
            self._require_keys(arguments, {"query"})
            return self.search_sources(str(arguments["query"]))
        if tool_name == "read_paper_metadata":
            self._require_keys(arguments, {"source_id"})
            return self.read_paper_metadata(str(arguments["source_id"]))
        if tool_name == "read_managed_blocks":
            self._require_keys(arguments, {"source_id", "block_ids"})
            block_ids = arguments["block_ids"]
            if not isinstance(block_ids, list) or not all(isinstance(x, str) for x in block_ids):
                raise ToolPolicyError("block_ids must be a list of full ids")
            return self.read_managed_blocks(str(arguments["source_id"]), block_ids)
        if tool_name == "search_arxiv":
            if "query" not in arguments or not str(arguments["query"]).strip():
                raise ToolPolicyError("query is required for search_arxiv")
            categories = arguments.get("categories")
            max_results = arguments.get("max_results", 5)
            if not isinstance(categories, (list, tuple, type(None))):
                raise ToolPolicyError("categories must be a list")
            return self.search_arxiv(
                str(arguments["query"]),
                categories=categories,
                max_results=int(max_results or 5),
            )
        if tool_name == "search_web":
            if set(arguments) != {"query", "max_results"}:
                raise ToolPolicyError("tool arguments do not match the web search contract")
            return self.search_web(str(arguments["query"]), max_results=int(arguments["max_results"] or 5))
        self._require_keys(arguments, {"run_id"})
        return self.read_run_status(str(arguments["run_id"]))

    def search_sources(self, query: str) -> list[dict[str, object]]:
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        if not terms:
            raise ToolPolicyError("search query must not be blank")
        if not self._sources:
            # Fail closed, mirroring search_arxiv without a client: an empty
            # managed catalog is "unavailable", never "no results". Otherwise
            # the model burns its whole budget retrying searches that can
            # never return anything instead of switching to external search.
            raise ToolPolicyError(
                "no managed papers are attached to this session; "
                "switch to search_arxiv for external literature"
            )
        ranked: list[tuple[int, ManagedSource]] = []
        for source in self._sources.values():
            haystack = set(re.findall(
                r"[a-z0-9]+", f"{source.source_id} {source.title}".lower()
            ))
            score = len(terms & haystack)
            if score:
                ranked.append((score, source))
        return [self._source_summary(source) for source in rank_sources(ranked)]

    def search_arxiv(self, query: str, *, categories: Sequence[str] | None = None, max_results: int = 5) -> list[dict[str, object]]:
        """Read-only external literature discovery via the arXiv MCP server.

        Fail-closed: if no arXiv client is wired in, the tool refuses to run
        rather than silently returning nothing, so the explorer never mistakes
        "unavailable" for "no results".
        """
        if self._arxiv is None:
            raise ToolPolicyError("arxiv retrieval is not configured")
        try:
            papers = list(self._arxiv.search_papers(query, categories=categories, max_results=max_results))
        except Exception as exc:  # noqa: BLE001  (external retrieval is best-effort / fail-closed)
            raise ToolPolicyError(f"arxiv retrieval unavailable: {exc}") from exc
        # Lexical relevance to the query (title weighted by appearing twice),
        # so the model and the user's candidate list can rank results instead of
        # relying on the feed order.
        qterms = set(re.findall(r"[a-z0-9]+", query.lower()))
        for paper in papers:
            title_terms = set(re.findall(r"[a-z0-9]+", str(paper.get("title", "")).lower()))
            abstract_terms = set(re.findall(r"[a-z0-9]+", str(paper.get("abstract_preview", "")).lower()))
            matches = len(qterms & title_terms) * 2 + len(qterms & abstract_terms)
            paper["relevance"] = round(matches / max(1, len(qterms) * 2), 2)
        return sorted(papers, key=lambda paper: float(paper.get("relevance", 0.0)), reverse=True)

    def search_web(self, query: str, *, max_results: int = 5) -> list[dict[str, object]]:
        """Search configured web sources without entering the paper evidence chain."""
        if self._web_search is None:
            raise ToolPolicyError("web retrieval is not configured")
        normalized_query = " ".join(query.split())[:500]
        if not normalized_query:
            raise ToolPolicyError("web search query must not be blank")
        self._last_web_search_usage = None
        try:
            search_with_metadata = getattr(self._web_search, "search_with_metadata", None)
            if callable(search_with_metadata):
                response = search_with_metadata(normalized_query, max_results=max_results)
                if not isinstance(response, WebSearchResponse):
                    raise WebRetrievalError("web search provider returned invalid usage metadata")
                raw = response.results
                usage = {
                    key: value for key, value in response.usage.items()
                    if isinstance(key, str) and isinstance(value, int) and value >= 0
                }
                self._last_web_search_usage = {
                    "kind": "web_search_usage",
                    "model": response.model,
                    "response_id": response.response_id,
                    **usage,
                }
            else:
                raw = self._web_search.search(normalized_query, max_results=max_results)
            normalized = normalize_web_results(raw, max_results=max_results)
            if not normalized:
                # An empty result is not a successful retrieval.  Returning
                # [] makes the agent believe the provider answered normally
                # and invites an unbounded sequence of query rewrites.  Keep
                # this as a provider-boundary error so the runtime can apply
                # its retry/circuit-breaker policy and preserve the evidence
                # boundary honestly.
                raise WebRetrievalError("web search provider returned no results")
            return normalized
        except (WebRetrievalUnavailable, WebRetrievalError) as exc:
            raise ToolPolicyError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary is fail-closed
            raise ToolPolicyError(f"web retrieval unavailable: {type(exc).__name__}") from exc

    def read_paper_metadata(self, source_id: str) -> dict[str, object]:
        source = self._source(source_id)
        blocks = self._read_blocks(source)
        return {
            **self._source_summary(source),
            "block_count": len(blocks),
            "sample_block_ids": [item["block_id"] for item in blocks[:5]],
            # Full block index (id + section + page, no text) so the model can
            # enumerate every block of a paper and read exactly what it needs
            # (abstract / method / results / conclusion) instead of being limited
            # to the five cover-page sample ids.
            "block_index": [
                {
                    "block_id": item["block_id"],
                    "section_path": item.get("section_path", []),
                    "page": item.get("page_start"),
                }
                for item in blocks
            ],
        }

    def read_managed_blocks(
        self, source_id: str, block_ids: Sequence[str]
    ) -> list[dict[str, object]]:
        if not block_ids or len(block_ids) > 20 or len(set(block_ids)) != len(block_ids):
            raise ToolPolicyError("block request must contain 1 to 20 unique full ids")
        source = self._source(source_id)
        indexed = {item.get("block_id"): item for item in self._read_blocks(source)}
        if any(block_id not in indexed for block_id in block_ids):
            raise ToolPolicyError("managed block is unavailable")
        return [
            {
                "source_id": source_id,
                "block_id": block_id,
                "kind": indexed[block_id].get("kind"),
                "text": indexed[block_id].get("text", ""),
                "section_path": indexed[block_id].get("section_path", []),
                "page": indexed[block_id].get("page_start"),
            }
            for block_id in block_ids
        ]

    def read_run_status(self, run_id: str) -> dict[str, object]:
        if run_id != self._run_id:
            raise ToolPolicyError("run status is unavailable")
        raw = self._status_reader(run_id)
        return {key: value for key, value in raw.items() if key in _STATUS_FIELDS}

    def _source(self, source_id: str) -> _ManagedSource:
        source = self._sources.get(source_id)
        if source is None:
            raise ToolPolicyError("managed source is unavailable")
        return source

    @staticmethod
    def _read_blocks(source: ManagedSource) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        # Managed material may contain legacy bytes from older parsers. A bad
        # byte in one block must not abort metadata discovery for the whole run;
        # JSON structure remains parseable after replacement decoding.
        for line in source.blocks_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("block_id"), str):
                blocks.append(item)
        return blocks

    @staticmethod
    def _source_summary(source: ManagedSource) -> dict[str, object]:
        return {
            "source_id": source.source_id,
            "title": source.title,
            "source_url": source.source_url,
            "source_authority": source.source_authority,
            "representation": source.representation,
        }

    @staticmethod
    def _require_keys(arguments: Mapping[str, Any], expected: set[str]) -> None:
        if set(arguments) != expected:
            raise ToolPolicyError("tool arguments do not match the read-only contract")
