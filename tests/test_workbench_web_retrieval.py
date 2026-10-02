from __future__ import annotations

import httpx
import pytest

from research_pulse.workbench.web_retrieval import (
    DeepSeekWebSearchProvider,
    build_env_web_search_provider,
    HttpJsonWebSearchProvider,
    TavilyWebSearchProvider,
    WebRetrievalError,
    normalize_web_results,
)


def test_normalize_web_results_deduplicates_and_caps_urls() -> None:
    results = normalize_web_results(
        [
            {"title": "one", "url": "https://example.com/1", "snippet": "a"},
            {"title": "duplicate", "url": "https://example.com/1", "snippet": "b"},
            {"title": "two", "url": "https://example.com/2", "snippet": "c"},
        ],
        max_results=1,
    )
    assert [item["url"] for item in results] == ["https://example.com/1"]


def test_http_json_provider_accepts_results_envelope_and_bounds_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [
            {"title": "Official", "url": "https://example.com", "description": "summary"},
        ]})

    provider = HttpJsonWebSearchProvider(
        "https://search.example/api",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    raw = provider.search("  latest  ", max_results=99)
    assert raw[0]["title"] == "Official"
    assert seen[0].url.params["max_results"] == "10"
    assert seen[0].headers["authorization"] == "Bearer secret"


def test_http_json_provider_rejects_bad_response_shape() -> None:
    provider = HttpJsonWebSearchProvider(
        "https://search.example/api",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
    )
    with pytest.raises(WebRetrievalError, match="invalid result shape"):
        provider.search("query")


def test_deepseek_provider_calls_responses_web_search_and_extracts_sources() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "status": "completed",
            "output_text": "A bounded answer.",
            "id": "resp-search-1",
            "model": "deepseek-v4-flash",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
                "input_tokens_details": {"cached_tokens": 10},
                "output_tokens_details": {"reasoning_tokens": 5},
            },
            "output": [{
                "type": "web_search_call",
                "action": {"type": "search", "sources": [
                    {"url": "https://example.com/one", "title": "Official one"},
                    {"url": "https://example.com/two"},
                ]},
            }],
        })

    provider = DeepSeekWebSearchProvider(
        "deepseek-secret",
        transport=httpx.MockTransport(handler),
    )
    raw = provider.search("latest research", max_results=2)
    assert [item["url"] for item in raw] == [
        "https://example.com/one", "https://example.com/two",
    ]
    assert raw[1]["title"] == "example.com"
    assert seen[0].url.path == "/responses"
    assert seen[0].headers["authorization"] == "Bearer deepseek-secret"
    body = __import__("json").loads(seen[0].content)
    assert body["tools"] == [{"type": "web_search"}]
    assert body["tool_choice"] == {"type": "web_search"}
    assert body["reasoning"] == {"effort": "none"}
    response = provider.search_with_metadata("latest research", max_results=2)
    assert response.response_id == "resp-search-1"
    assert response.usage == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cached_tokens": 10,
        "reasoning_tokens": 5,
    }


def test_deepseek_provider_rejects_incomplete_response_without_search_call() -> None:
    provider = DeepSeekWebSearchProvider(
        "deepseek-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "status": "incomplete",
            "output": [{"type": "message", "content": []}],
        })),
    )
    with pytest.raises(WebRetrievalError, match="incomplete"):
        provider.search_with_metadata("latest research", max_results=2)


def test_tavily_provider_posts_search_request_and_normalizes_content() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "results": [{
                "title": "Tavily result",
                "url": "https://example.com/research",
                "content": "A bounded result snippet.",
            }],
            "usage": {"credits": 1},
            "request_id": "tavily-1",
        })

    provider = TavilyWebSearchProvider(
        "tvly-secret",
        transport=httpx.MockTransport(handler),
    )
    response = provider.search_with_metadata("latest research", max_results=5)

    assert response.results == ({
        "title": "Tavily result",
        "url": "https://example.com/research",
        "snippet": "A bounded result snippet.",
    },)
    assert response.model == "tavily"
    assert response.response_id == "tavily-1"
    assert response.usage == {"credits": 1}
    assert seen[0].url.path == "/search"
    assert seen[0].headers["authorization"] == "Bearer tvly-secret"
    body = __import__("json").loads(seen[0].content)
    assert body == {
        "query": "latest research",
        "search_depth": "basic",
        "topic": "general",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
    }


def test_tavily_provider_retries_one_transient_empty_result() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"results": [], "request_id": "empty-1"})
        return httpx.Response(200, json={
            "results": [{
                "title": "Recovered result",
                "url": "https://example.com/recovered",
                "content": "A result after a transient empty response.",
            }],
            "request_id": "ok-2",
        })

    provider = TavilyWebSearchProvider(
        "tvly-secret",
        transport=httpx.MockTransport(handler),
    )

    response = provider.search_with_metadata("latest research")

    assert calls == 2
    assert response.results[0]["url"] == "https://example.com/recovered"


def test_environment_builder_can_select_tavily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    monkeypatch.delenv("DEEPSEEK_WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    provider = build_env_web_search_provider()

    assert isinstance(provider, TavilyWebSearchProvider)
    assert provider.api_key == "tvly-secret"


def test_environment_builder_prefers_dedicated_web_search_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("RESEARCH_PULSE_WEB_SEARCH_API_KEY", "tavily-secret")
    assert build_env_web_search_provider() is None

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("DEEPSEEK_WEB_SEARCH_API_KEY", "web-search-secret")
    provider = build_env_web_search_provider()
    assert isinstance(provider, DeepSeekWebSearchProvider)
    assert provider.api_key == "web-search-secret"


def test_environment_builder_falls_back_to_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    provider = build_env_web_search_provider()
    assert isinstance(provider, DeepSeekWebSearchProvider)
    assert provider.api_key == "deepseek-secret"
