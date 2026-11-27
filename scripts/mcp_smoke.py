"""Smoke test for the MCP server: discovery (tools/list) + one call to each tool over stdio.

    python scripts/mcp_smoke.py
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "voice_agent.mcp_server.server", "--transport", "stdio"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 60)
            print("initialized", flush=True)
            tools = await asyncio.wait_for(session.list_tools(), 30)
            for t in tools.tools:
                print(f"\nTOOL {t.name}\n  {t.description}\n  schema: {json.dumps(t.inputSchema)}", flush=True)
            res = await asyncio.wait_for(
                session.call_tool("rag.search", {"query": "eco-friendly stainless steel cleaner", "max_price": 15, "top_k": 2}), 120)
            print("\nrag.search ->", json.dumps(res.structuredContent)[:600], flush=True)
            res = await asyncio.wait_for(session.call_tool("web.search", {"query": "Affresh stainless steel cleaning spray price"}), 60)
            print("\nweb.search ->", json.dumps(res.structuredContent)[:600], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
