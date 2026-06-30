"""OQ4 spike — does claude_agent_sdk authenticate on the Claude SUBSCRIPTION
with ANTHROPIC_API_KEY unset (no API key)? Mirrors local-fitness's agent.

PASS = a text reply comes back with no API key in the environment.
"""
from __future__ import annotations

import asyncio
import os
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)


async def main() -> int:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("INCONCLUSIVE: ANTHROPIC_API_KEY is set — this would test the API path, not the subscription.")
        return 2

    opts = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        # No tools, no bypass needed — pure text round-trip.
    )
    chunks: list[str] = []
    try:
        async for msg in query(prompt="Reply with exactly one word: PONG", options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        chunks.append(b.text)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: SDK query raised without an API key: {type(e).__name__}: {e}")
        return 1

    reply = "".join(chunks).strip()
    print(f"reply={reply!r}")
    if "PONG" in reply.upper():
        print("PASS: subscription auth works with ANTHROPIC_API_KEY unset.")
        return 0
    print("FAIL: no usable reply returned.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
