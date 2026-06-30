"""Standalone stdio MCP server exposing the budget read tools (design §3, §6).

Lets a Claude Code session (or any MCP client) query budget.db's deterministic
read tools over stdio. Built directly on the raw `mcp` low-level Server from the
`ToolSpec` registry in `agent/tools.py` (one source of truth). The server runs
NO Claude inference — it serves data + the deterministic `rendered` markdown;
synthesis happens in the client (skills).
"""
from __future__ import annotations

import json

from mcp import types
from mcp.server.lowlevel import Server

from ..agent import tools as agent_tools


def build_server() -> Server:
    server: Server = Server("budget")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [types.Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
                for s in agent_tools.TOOL_SPECS]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        spec = agent_tools.SPEC_BY_NAME.get(name)
        if spec is None:
            return [types.TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        result = await spec.handler(arguments or {})
        # Prefer the deterministic rendered markdown; fall back to JSON for
        # error/notes payloads that carry no `rendered`.
        text = result.get("rendered") or json.dumps(result.get("data", result), default=str)
        return [types.TextContent(type="text", text=text)]

    return server


async def run_stdio() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    import asyncio

    asyncio.run(run_stdio())
