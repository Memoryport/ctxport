"""End-to-end MCP test: start `ctxport serve-mcp` over stdio with the official client."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

mcp = pytest.importorskip("mcp")

from ctxport.models import Conversation, MemoryItem, Message  # noqa: E402
from ctxport.store import Store  # noqa: E402


def _seed(home):
    s = Store(home / "ctxport.db")
    c = Conversation(platform="test", native_id="m1", title="MCP 测试对话")
    c.messages = [Message(role="user", text="我们讨论了量子计算的基础"), Message(role="assistant", text="好的")]
    s.upsert_conversation(c)
    s.upsert_memory(MemoryItem(text="User studies math", status="accepted"))
    s.commit()
    s.close()


def test_mcp_stdio_roundtrip(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = tmp_path / "home"
    home.mkdir()
    _seed(home)
    env = dict(os.environ, CTXPORT_HOME=str(home), PYTHONPATH=os.pathsep.join(sys.path))
    params = StdioServerParameters(command=sys.executable, args=["-m", "ctxport", "serve-mcp"], env=env)

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"get_user_memory", "search_history", "list_recent_conversations", "get_conversation",
                        "remember"} <= tools
                res = await session.call_tool("search_history", {"query": "量子计算"})
                text = "".join(getattr(c, "text", "") for c in res.content)
                assert "test:m1" in text
                res = await session.call_tool("remember", {"fact": "Prefers dark mode", "category": "preference"})
                assert "candidate" in "".join(getattr(c, "text", "") for c in res.content)
                res = await session.call_tool("get_user_memory", {})
                mem = "".join(getattr(c, "text", "") for c in res.content)
                # an MCP client can only suggest: the fact waits for the user's review
                assert "User studies math" in mem and "Prefers dark mode" not in mem

    asyncio.run(asyncio.wait_for(run(), 60))
    s = Store(home / "ctxport.db")
    cands = {m.text: m.platform for m in s.memories(status=("candidate",))}
    assert cands.get("Prefers dark mode") == "mcp"
    s.close()
