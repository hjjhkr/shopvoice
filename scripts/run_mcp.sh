#!/usr/bin/env bash
# Start the MCP server over streamable HTTP (set MCP_TRANSPORT=http in .env to make the agent use it).
exec python -m voice_agent.mcp_server.server --transport http --port 8765
