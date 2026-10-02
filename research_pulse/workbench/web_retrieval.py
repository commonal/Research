"""Bounded web retrieval for the workbench.

The production adapter uses DeepSeek's server-side ``web_search`` tool through
the Responses API. Results are normalized into URL-backed web references; they
are never converted into managed paper blocks or canonical evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import os
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx


class WebRetrievalError(RuntimeError):
    """A web provider failed or returned an invalid response."""


class WebRetrievalUnavailable(WebRetrievalError):
    """No web provider is configured for this deployment."""


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    source: str = ""
    published_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class WebSearchResponse:
    """Provider response with bounded results and billable usage metadata."""

    results: tuple[Mapping[str, object], ...]
    usage: Mapping[str, int] = field(default_factory=dict)
    model: str = ""
    response_id: str = ""


class WebSearchProvider(Protocol):
    def search(self, query: str, *, max_results: int) -> Sequence[Mapping[str, object]]: ...


def _bounded_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _valid_http_url(value: object) -> str:
    url = _bounded_text(value, 2000)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def normalize_web_results(
    raw_results: Sequence[Mapping[str, object]] | Sequence[object],
    *,
    max_results: int,
) -> list[dict[str, object]]:
    """Normalize and bound an adapter response to a safe URL citation shape."""
    limit = max(1, min(int(max_results or 5), 10))
    normalized: list[dict[str, object]] = []
    seen_urls: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        url = _valid_http_url(raw.get("url") or raw.get("link") or raw.get("source_url"))
        title = _bounded_text(raw.get("title") or raw.get("name"), 240)
        snippet = _bounded_text(raw.get("snippet") or raw.get("description") or raw.get("text"), 800)
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        host = urlparse(url).netloc
        result = WebSearchResult(
            title=title,
            url=url,
            snippet=snippet,
            source=_bounded_text(raw.get("source") or host, 120),
            published_at=_bounded_text(raw.get("published_at") or raw.get("published"), 80) or None,
        )
        normalized.append(result.to_dict())
        if len(normalized) >= limit:
            break
    return normalized


class HttpJsonWebSearchProvider:
    """Generic JSON search gateway.

    The gateway receives ``q`` and ``max_results`` query parameters and may
    return either a JSON list or ``{"results": [...]}``.  This keeps the
    application independent of a vendor-specific SDK while still allowing a
    real provider to be configured with environment variables.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        endpoint = endpoint.strip()
        if not _valid_http_url(endpoint):
            raise ValueError("web search endpoint must be an http(s) URL")
        if timeout_seconds <= 0:
            raise ValueError("web search timeout must be positive")
        self.endpoint = endpoint
        self.api_key = api_key.strip() if api_key else None
        self.timeout_seconds = min(float(timeout_seconds), 60.0)
        self.transport = transport

    def search(self, query: str, *, max_results: int = 5) -> Sequence[Mapping[str, object]]:
        normalized_query = " ".join(query.split())[:500]
        if not normalized_query:
            raise WebRetrievalError("web search query must not be blank")
        limit = max(1, min(int(max_results or 5), 10))
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                response = client.get(
                    self.endpoint,
                    params={"q": normalized_query, "max_results": limit},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WebRetrievalError(f"web search provider unavailable: {type(exc).__name__}") from exc
        if isinstance(payload, Mapping):
            if "results" in payload:
                payload = payload["results"]
            elif "items" in payload:
                payload = payload["items"]
            else:
                raise WebRetrievalError("web search provider returned an invalid result shape")
        if not isinstance(payload, list):
            raise WebRetrievalError("web search provider returned an invalid result shape")
        return [item for item in payload if isinstance(item, Mapping)]


class TavilyWebSearchProvider:
    """Tavily Search API adapter.

    Tavily is intentionally kept behind the same small provider contract as
    DeepSeek native search. The API key stays server-side and the response is
    reduced to URL-backed snippets before it reaches the agent.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.tavily.com",
        search_depth: str = "basic",
        topic: str = "general",
        timeout_seconds: float = 20.0,
        empty_result_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Tavily API key must not be blank")
        if not _valid_http_url(base_url):
            raise ValueError("Tavily API base URL must be an http(s) URL")
        if timeout_seconds <= 0:
            raise ValueError("Tavily search timeout must be positive")
        if empty_result_retries < 0:
            raise ValueError("Tavily empty-result retries must not be negative")
        if search_depth not in {"basic", "fast", "ultra-fast", "advanced"}:
            raise ValueError("Tavily search depth is invalid")
        if topic not in {"general", "news", "finance"}:
            raise ValueError("Tavily topic is invalid")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.search_depth = search_depth
        self.topic = topic
        self.timeout_seconds = min(float(timeout_seconds), 60.0)
        self.empty_result_retries = min(int(empty_result_retries), 2)
        self.transport = transport

    def search_with_metadata(self, query: str, *, max_results: int = 5) -> WebSearchResponse:
        normalized_query = " ".join(query.split())[:500]
        if not normalized_query:
            raise WebRetrievalError("web search query must not be blank")
        limit = max(1, min(int(max_results or 5), 20))
        payload = {
            "query": normalized_query,
            "search_depth": self.search_depth,
            "topic": self.topic,
            "max_results": limit,
            "include_answer": False,
            "include_raw_content": False,
        }
        result: Mapping[str, object] | None = None
        raw_results: list[object] | None = None
        for attempt in range(self.empty_result_retries + 1):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(self.timeout_seconds),
                    follow_redirects=True,
                    transport=self.transport,
                ) as client:
                    response = client.post(
                        f"{self.base_url}/search",
                        json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    response.raise_for_status()
                    candidate = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise WebRetrievalError(f"Tavily web search unavailable: {type(exc).__name__}") from exc
            if not isinstance(candidate, Mapping):
                raise WebRetrievalError("Tavily web search returned an invalid response shape")
            candidate_results = candidate.get("results")
            if not isinstance(candidate_results, list):
                raise WebRetrievalError("Tavily web search returned an invalid result shape")
            result = candidate
            raw_results = candidate_results
            if raw_results or attempt >= self.empty_result_retries:
                break
        if result is None or raw_results is None:
            raise WebRetrievalError("Tavily web search returned no results")
        normalized_results: list[dict[str, object]] = []
        for item in raw_results:
            if not isinstance(item, Mapping):
                continue
            normalized_results.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content") or item.get("snippet") or item.get("raw_content"),
            })
        raw_usage = result.get("usage")
        usage: dict[str, int] = {}
        if isinstance(raw_usage, Mapping):
            credits = raw_usage.get("credits")
            try:
                credits_value = int(credits)
            except (TypeError, ValueError):
                credits_value = -1
            if credits_value >= 0:
                usage["credits"] = credits_value
        return WebSearchResponse(
            results=tuple(normalized_results),
            usage=usage,
            model="tavily",
            response_id=_bounded_text(result.get("request_id"), 200),
        )

    def search(self, query: str, *, max_results: int = 5) -> Sequence[Mapping[str, object]]:
        return self.search_with_metadata(query, max_results=max_results).results


def _response_output_items(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    output = payload.get("output", [])
    if not isinstance(output, list):
        raise WebRetrievalError("deepseek web search returned an invalid output shape")
    return [item for item in output if isinstance(item, Mapping)]


def _deepseek_web_sources(payload: Mapping[str, object]) -> list[dict[str, object]]:
    """Extract URL sources from web_search_call actions and URL annotations."""
    output_text = _bounded_text(payload.get("output_text"), 800)
    raw_results: list[dict[str, object]] = []
    for item in _response_output_items(payload):
        if item.get("type") == "web_search_call":
            action = item.get("action")
            if isinstance(action, Mapping):
                sources = action.get("sources", [])
                if isinstance(sources, list):
                    for source in sources:
                        if isinstance(source, Mapping):
                            raw_results.append({
                                "title": source.get("title") or source.get("name"),
                                "url": source.get("url") or source.get("link"),
                                "snippet": source.get("snippet") or output_text,
                                "published_at": source.get("published_at") or source.get("publishedDate"),
                            })
        if item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            annotations = part.get("annotations", [])
            if not isinstance(annotations, list):
                continue
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    continue
                url = annotation.get("url") or annotation.get("source_url")
                if url:
                    raw_results.append({
                        "title": annotation.get("title"),
                        "url": url,
                        "snippet": output_text,
                    })
    # Some compatible responses expose no title for a source. The normalizer
    # requires a title, so use the URL host as a bounded, non-fabricated label.
    for item in raw_results:
        if not _bounded_text(item.get("title"), 240):
            url = _valid_http_url(item.get("url"))
            item["title"] = urlparse(url).netloc if url else "网页来源"
    return raw_results


def _deepseek_usage(payload: Mapping[str, object]) -> dict[str, int]:
    """Keep only non-negative numeric usage fields from the provider response."""
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, Mapping):
        return {}
    usage: dict[str, int] = {}
    for name in (
        "input_tokens", "output_tokens", "total_tokens",
        "cached_tokens", "reasoning_tokens",
    ):
        value = raw_usage.get(name)
        if isinstance(value, bool):
            continue
        try:
            integer = int(value)
        except (TypeError, ValueError):
            continue
        if integer >= 0:
            usage[name] = integer
    input_details = raw_usage.get("input_tokens_details")
    if isinstance(input_details, Mapping):
        cached = input_details.get("cached_tokens")
        if "cached_tokens" not in usage and isinstance(cached, int) and cached >= 0:
            usage["cached_tokens"] = cached
    output_details = raw_usage.get("output_tokens_details")
    if isinstance(output_details, Mapping):
        reasoning = output_details.get("reasoning_tokens")
        if "reasoning_tokens" not in usage and isinstance(reasoning, int) and reasoning >= 0:
            usage["reasoning_tokens"] = reasoning
    return usage


class DeepSeekWebSearchProvider:
    """DeepSeek Responses API adapter for server-side web search."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DeepSeek API key must not be blank")
        if not _valid_http_url(base_url):
            raise ValueError("DeepSeek API base URL must be an http(s) URL")
        if timeout_seconds <= 0:
            raise ValueError("DeepSeek web search timeout must be positive")
        self.api_key = api_key.strip()
        self.model = model.strip() or "deepseek-v4-flash"
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = min(float(timeout_seconds), 60.0)
        self.transport = transport

    def search_with_metadata(self, query: str, *, max_results: int = 5) -> WebSearchResponse:
        normalized_query = " ".join(query.split())[:500]
        if not normalized_query:
            raise WebRetrievalError("web search query must not be blank")
        payload = {
            "model": self.model,
            "input": normalized_query,
            "tools": [{"type": "web_search"}],
            "tool_choice": {"type": "web_search"},
            # Search is a retrieval primitive, not a reasoning task.  Without
            # this explicit setting DeepSeek can spend the entire output
            # budget on reasoning and return no web_search_call at all.
            "reasoning": {"effort": "none"},
            "max_output_tokens": 512,
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                response = client.post(
                    f"{self.base_url}/responses",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WebRetrievalError(f"deepseek web search unavailable: {type(exc).__name__}") from exc
        if not isinstance(result, Mapping):
            raise WebRetrievalError("deepseek web search returned an invalid response shape")
        if result.get("status") == "failed":
            raise WebRetrievalError("deepseek web search failed")
        sources = _deepseek_web_sources(result)
        if result.get("status") not in {None, "completed"} and not sources:
            status = _bounded_text(result.get("status"), 40) or "unknown"
            raise WebRetrievalError(f"deepseek web search response incomplete: {status}")
        return WebSearchResponse(
            results=tuple(sources),
            usage=_deepseek_usage(result),
            model=_bounded_text(result.get("model") or self.model, 120),
            response_id=_bounded_text(result.get("id"), 200),
        )

    def search(self, query: str, *, max_results: int = 5) -> Sequence[Mapping[str, object]]:
        return self.search_with_metadata(query, max_results=max_results).results


def build_env_web_search_provider() -> WebSearchProvider | None:
    """Build the configured web adapter from server-only environment variables.

    Web search can use a dedicated key so provider-side usage can be
    attributed separately from model conversations.  The main key remains a
    backwards-compatible fallback for existing deployments that have not yet
    configured ``DEEPSEEK_WEB_SEARCH_API_KEY``.
    """
    provider_name = os.getenv("WEB_SEARCH_PROVIDER", "deepseek").strip().lower()
    if provider_name in {"none", "disabled", "off"}:
        return None
    if provider_name == "tavily":
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            return None
        raw_timeout = os.getenv("TAVILY_TIMEOUT_SECONDS", "20")
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 20.0
        return TavilyWebSearchProvider(
            api_key,
            base_url=os.getenv("TAVILY_API_BASE", "https://api.tavily.com"),
            search_depth=os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
            topic=os.getenv("TAVILY_TOPIC", "general"),
            timeout_seconds=timeout,
        )
    if provider_name not in {"deepseek", "native", ""}:
        raise ValueError(f"unsupported WEB_SEARCH_PROVIDER: {provider_name}")
    api_key = (
        os.getenv("DEEPSEEK_WEB_SEARCH_API_KEY", "").strip()
        or os.getenv("DEEPSEEK_API_KEY", "").strip()
    )
    if not api_key:
        return None
    raw_timeout = os.getenv("DEEPSEEK_WEB_SEARCH_TIMEOUT_SECONDS", "30")
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = 30.0
    return DeepSeekWebSearchProvider(
        api_key,
        model=os.getenv("DEEPSEEK_WEB_SEARCH_MODEL", "deepseek-v4-flash"),
        base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
        timeout_seconds=timeout,
    )
