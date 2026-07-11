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


def _jsonable(obj: dict) -> dict:
    """Round-trip through JSON with default=str so non-JSON values (dates,
    Decimals) never break the SDK's structuredContent serialization — the same
    coercion the old json.dumps(default=str) text path applied."""
    return json.loads(json.dumps(obj, default=str))


def build_server() -> Server:
    server: Server = Server("budget")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [types.Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
                for s in agent_tools.TOOL_SPECS]

    @server.call_tool()
    async def _call(
        name: str, arguments: dict
    ) -> tuple[list[types.TextContent], dict] | dict:
        spec = agent_tools.SPEC_BY_NAME.get(name)
        if spec is None:
            return {"error": f"unknown tool: {name}"}
        result = await spec.handler(arguments or {})
        # The deterministic `rendered` markdown stays the text block (skills print
        # it verbatim — byte-identical to the pre-structuredContent transport),
        # while the structured `data` payload rides in structuredContent so the
        # client model can resolve row references (txn_id, merchant, cents)
        # without parsing the printed table (budget-analyst rule 6).
        rendered = result.get("rendered")
        if rendered:
            structured = {"data": result["data"]} if "data" in result else {
                k: v for k, v in result.items() if k != "rendered"}
            return [types.TextContent(type="text", text=rendered)], _jsonable(structured)
        # No rendered (errors, notes, write acks): the dict alone — the SDK
        # serializes it into a JSON text block AND structuredContent.
        return _jsonable(result)

    return server


async def run_stdio() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    import asyncio

    asyncio.run(run_stdio())
