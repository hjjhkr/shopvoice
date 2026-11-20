"""Synchronous facade over an MCP client session.

The session lives on a background event-loop thread, so LangGraph nodes, Streamlit
and the eval harness can all call tools with plain function calls. With
MCP_TRANSPORT=stdio the server is spawned as a subprocess; with MCP_TRANSPORT=http
the client connects to MCP_URL (streamable HTTP).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from contextlib import AsyncExitStack
from typing import Any

from .config import ROOT, get_settings
from .safety import ALLOWED_TOOLS


def llm_tool_name(mcp_name: str) -> str:
    """LLM APIs only accept [a-zA-Z0-9_-] in function names: 'rag.search' -> 'rag_search'."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", mcp_name)


class MCPToolClient:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._session = None
        self._stop: asyncio.Event | None = None
        self._tools: list[dict[str, Any]] | None = None
        self._thread = threading.Thread(target=self._run, name="mcp-client", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=120):
            raise TimeoutError("MCP server did not start within 120 s")
        if self._error:
            raise RuntimeError(f"Could not start MCP session: {self._error}") from self._error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except BaseException as e:  # noqa: BLE001 - reported to the caller thread
            self._error = e
            self._ready.set()

    async def _main(self) -> None:
        from mcp import ClientSession

        settings = get_settings()
        self._stop = asyncio.Event()
        async with AsyncExitStack() as stack:
            if settings.mcp_transport == "http":
                from mcp.client.streamable_http import streamablehttp_client
                read, write, _ = await stack.enter_async_context(streamablehttp_client(settings.mcp_url))
            else:
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "voice_agent.mcp_server.server", "--transport", "stdio"],
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    cwd=str(ROOT),
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            self._session = await stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            self._ready.set()
            await self._stop.wait()

    def _call(self, coro, timeout: float = 90):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ---- public API ---------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        """MCP tool discovery (tools/list): name, description and JSON schema of each tool."""
        if self._tools is None:
            res = self._call(self._session.list_tools())
            self._tools = [
                {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                for t in res.tools if t.name in ALLOWED_TOOLS      # tool allow-list
            ]
        return self._tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in ALLOWED_TOOLS:
            return {"results": [], "error": f"tool '{name}' is not allowed"}
        arguments = {k: v for k, v in arguments.items() if v is not None}
        try:
            res = self._call(self._session.call_tool(name, arguments))
        except Exception as e:  # noqa: BLE001
            return {"results": [], "error": f"{type(e).__name__}: {e}"}
        if getattr(res, "structuredContent", None):
            data = res.structuredContent
            data = data.get("result", data) if isinstance(data, dict) else data
        else:
            text = "".join(getattr(c, "text", "") for c in res.content)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {"results": [], "error": text[:500] or "empty tool response"}
        if res.isError and "error" not in data:
            data = {"results": [], "error": str(data)[:500]}
        return data

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=10)


_client: MCPToolClient | None = None
_lock = threading.Lock()


def get_mcp_client() -> MCPToolClient:
    global _client
    with _lock:
        if _client is None:
            _client = MCPToolClient()
        return _client


def close_mcp_client() -> None:
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None
