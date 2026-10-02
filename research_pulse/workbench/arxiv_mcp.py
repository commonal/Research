"""Read-only arXiv literature search via a long-running HTTP MCP server.

The MCP server (``uvx arxiv-mcp-server``) runs as an independent HTTP process
(``TRANSPORT=http``) that we spawn once and keep alive. Each search opens a
short-lived ``streamable_http_client`` session in a fresh event loop — HTTP
handshakes are cheap, the server stays up, and this sidesteps the Windows
background-thread/Proactor issues that plague ``stdio`` MCP subprocesses.

Exposed strictly read-only. This is the external-retrieval backend for the
exploration tool surface (fail-closed: no writes, no execution, no publishing).
"""

from __future__ import annotations

from collections.abc import Sequence
import asyncio
import json
import os
import socket
import subprocess
import time
from typing import Any
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ArxivMCPClient:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        proxy: str | None = "http://127.0.0.1:7890",
        tool_timeout: float = 60.0,
        serve_args: Sequence[str] = ("arxiv-mcp-server",),
    ) -> None:
        self._host = host
        self._port = port
        self._proxy = proxy
        self._timeout = tool_timeout
        self._serve_args = list(serve_args)
        self._proc: subprocess.Popen | None = None
        self._started = False

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/mcp"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "ArxivMCPClient":
        if self._started:
            return self
        env = {**os.environ}
        env["TRANSPORT"] = "http"
        env["HOST"] = self._host
        env["PORT"] = str(self._port)
        if self._proxy:
            env.setdefault("HTTP_PROXY", self._proxy)
            env.setdefault("HTTPS_PROXY", self._proxy)
        self._proc = subprocess.Popen(
            ["uvx", *self._serve_args],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_healthy(deadline=time.monotonic() + self._timeout + 15)
        self._started = True
        return self

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._started = False
        self._proc = None

    def _wait_healthy(self, *, deadline: float) -> None:
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError("arxiv mcp server exited during startup")
            try:
                with urllib.request.urlopen(
                    f"http://{self._host}:{self._port}/healthz", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        raise TimeoutError("arxiv mcp http server did not become healthy")

    # -- read-only query ---------------------------------------------------

    def search_papers(
        self,
        query: str,
        *,
        categories: Sequence[str] | None = None,
        max_results: int = 5,
    ) -> list[dict[str, object]]:
        if not query.strip():
            raise ValueError("arxiv search query must not be blank")
        # Lazy start: the MCP server (uvx) is only needed when a search actually
        # runs, so spawn it on first use rather than blocking app startup.
        if not self._started:
            self.start()
        args: dict[str, object] = {"query": query, "max_results": int(max_results)}
        if categories:
            args["categories"] = list(categories)
        raw = self._search_once(args)
        papers = raw.get("papers", []) if isinstance(raw, dict) else []
        return [self._normalize(item) for item in papers if isinstance(item, dict)]

    def _search_once(self, args: dict[str, object]) -> Any:
        import asyncio

        return asyncio.run(self._search_async(args))

    async def _search_async(self, args: dict[str, object]) -> Any:
        async def roundtrip() -> Any:
            async with streamable_http_client(self.url) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("search_papers", args)

        # The local MCP server may hang on its upstream arXiv fetch (proxy /
        # network stall). Bound the whole handshake + search so a stuck server
        # surfaces as TimeoutError instead of freezing the agent forever.
        try:
            result = await asyncio.wait_for(roundtrip(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("arxiv search timed out after the configured tool timeout") from exc
        text = "".join(getattr(item, "text", "") or "" for item in result.content)
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    @staticmethod
    def _normalize(item: dict[str, object]) -> dict[str, object]:
        abstract = str(item.get("abstract", ""))
        return {
            "source_id": str(item.get("resource_uri") or item.get("id") or ""),
            "arxiv_id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "authors": list(item.get("authors", []) or []),
            "abstract_preview": abstract[:400],
            "categories": list(item.get("categories", []) or []),
            "published": str(item.get("published") or ""),
            "url": str(item.get("url") or ""),
            "resource_uri": str(item.get("resource_uri") or ""),
            "source_authority": "original_research",
            "representation": "arxiv_metadata",
        }


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
