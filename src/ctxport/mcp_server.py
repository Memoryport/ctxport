"""MCP server exposing the local ctxport store (optional dependency: `pip install ctxport[mcp]`)."""
from __future__ import annotations

import json
import shutil
import sys
from typing import Optional

from .adapters._render import render_memories_md, visible
from .models import MemoryItem
from .redact import redact
from .store import Store
from .utils import fmt_time

INSTRUCTIONS = ("ctxport gives you the user's own chat history from other AI tools (ChatGPT, Claude, Codex, Gemini, "
                "Cherry Studio, Chatbox …) and their long-term memory. Call get_user_memory first for "
                "background; search_memory finds specific facts in the user's full profile; search_history finds "
                "earlier discussions.")


def _store() -> Store:
    return Store()


# ---------------------------------------------------------------- tool implementations (plain functions, testable)

def get_user_memory() -> str:
    """Return the user's long-term memory (profile, preferences, projects) as Markdown: the distilled profile
    (standard size) when there is one, otherwise the accepted memories."""
    from .distill import out_dir, profile_file
    if (out_dir() / "profile.md").exists():
        return redact(profile_file("standard").read_text(encoding="utf-8"))
    with _store() as s:
        mems = s.memories(status=("accepted",))
        if not mems:
            return "No accepted memories yet. The user can review candidates with `ctxport memories edit`."
        return render_memories_md(mems, redact=True, heading_level=2)


def search_memory(query: str, limit: int = 40) -> str:
    """Search the user's full distilled profile (every fact ctxport extracted from all their conversations) for
    facts containing any of the words in `query`. Use it for details the profile summary leaves out."""
    import json as _json
    from .distill import CATEGORY_TITLES, out_dir
    p = out_dir() / "facts.json"
    if not p.exists():
        return "No distilled profile yet (the user can create one in `ctxport app`, step 2)."
    try:
        facts = _json.loads(p.read_text(encoding="utf-8")).get("l2") or []
    except ValueError:
        return "The profile file could not be read."
    terms = [t.lower() for t in (query or "").split() if t]
    if not terms:
        return "Give one or more words to search for."
    limit = max(1, min(int(limit or 40), 200))
    hits = [f for f in facts if any(t in str(f.get("text", "")).lower() for t in terms)]
    hits.sort(key=lambda f: (-sum(t in str(f.get("text", "")).lower() for t in terms), -(f.get("last") or 0)))
    if not hits:
        return f"No facts mention {query!r}. Try search_history for the raw conversations."
    out = [f"- [{CATEGORY_TITLES.get(f.get('category'), f.get('category'))}] {redact(str(f.get('text')))}"
           for f in hits[:limit]]
    more = f"\n({len(hits) - limit} more; narrow the query)" if len(hits) > limit else ""
    return "\n".join(out) + more


def search_history(query: str, platform: Optional[str] = None, limit: int = 10) -> str:
    """Full-text search over all imported conversations (Chinese and English). Returns matching snippets."""
    limit = max(1, min(int(limit or 10), 50))
    with _store() as s:
        rows = s.search(query, platform=platform or None, limit=limit)
        if not rows:
            return f"No results for {query!r}."
        out = []
        for r in rows:
            text = redact(r["text"] or "")
            terms = [t for t in query.split() if t]
            pos = min([text.lower().find(t.lower()) for t in terms if text.lower().find(t.lower()) >= 0] or [0])
            snippet = text[max(0, pos - 150): pos + 350].replace("\n", " ")
            out.append(f"- [{r['platform']}] {redact(r['title'] or '')} · {fmt_time(r['created_at'])} · {r['role']}\n"
                       f"  id: {r['conversation_id']}\n  …{snippet}…")
        return "\n".join(out)


def list_recent_conversations(platform: Optional[str] = None, limit: int = 20) -> str:
    """List the most recently updated conversations (id, platform, title, date, message count)."""
    limit = max(1, min(int(limit or 20), 200))
    with _store() as s:
        rows = s.conversation_rows(platform=platform or None, limit=limit)
        return "\n".join(f"- {r['id']} · {fmt_time(r['updated_at'])} · {redact(r['title'] or '')} "
                         f"({r['message_count']} msgs)" for r in rows) or "No conversations imported yet."


def get_conversation(conversation_id: str, last_n: int = 30) -> str:
    """Return the last N user/assistant messages of one conversation (tools and reasoning omitted)."""
    with _store() as s:
        cid = s.find_conversation_id(conversation_id)
        conv = s.get_conversation(cid) if cid else None
        if not conv:
            return f"Conversation {conversation_id!r} not found."
        msgs = visible(conv.messages)[-max(1, int(last_n or 30)):]
        head = f"# {redact(conv.title)}\n{conv.id} · {conv.project or ''} · {fmt_time(conv.created_at)}\n"
        return head + "\n\n".join(f"[{m.role}] {redact(m.text)}" for m in msgs)


def remember(fact: str, category: str = "general") -> str:
    """Suggest a new fact about the user. It is saved as a *candidate*: it only becomes part of the user's
    memory (and of exported CLAUDE.md / AGENTS.md files) after the user accepts it with `ctxport memories edit`."""
    fact = (fact or "").strip()
    if not fact:
        return "Nothing to remember."
    # Never accept on the client's behalf: an MCP client may be acting on injected instructions from a web page
    # or a document, and accepted memories are written into other tools' instruction files on export.
    with _store() as s:
        r = s.upsert_memory(MemoryItem(text=fact[:2000], platform="mcp", category=(category or "general")[:40],
                                       status="candidate", confidence=0.9))
        s.commit()
    return {"new": "Saved as a candidate. The user will review it (`ctxport memories edit`) before it is used.",
            "updated": "Already known.", "kept": "Already known."}[r]


TOOLS = [get_user_memory, search_memory, search_history, list_recent_conversations, get_conversation, remember]


def build_server():
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as Server  # type: ignore
    except ImportError:
        try:  # mcp 1.x
            from mcp.server.fastmcp import FastMCP as Server  # type: ignore
        except ImportError as e:
            raise SystemExit("The MCP server needs the optional dependency: pip install 'ctxport[mcp]'") from e
    try:
        server = Server("ctxport", instructions=INSTRUCTIONS)
    except TypeError:
        server = Server("ctxport")
    for fn in TOOLS:
        server.tool()(fn)
    return server


def serve() -> None:
    build_server().run()


# ---------------------------------------------------------------- client configuration snippets

def _command() -> tuple:
    exe = shutil.which("ctxport")
    return (exe or "ctxport"), ["serve-mcp"]


def mcp_config(client: Optional[str] = None) -> str:
    cmd, args = _command()
    j = json.dumps({"mcpServers": {"ctxport": {"command": cmd, "args": args}}}, indent=2)
    blocks = {
        "claude-desktop": ("Claude Desktop", "Add to claude_desktop_config.json (Settings → Developer → Edit Config):\n" + j),
        "claude-code": ("Claude Code", f"claude mcp add --scope user ctxport -- {cmd} serve-mcp"),
        "codex": ("Codex", f"Add to ~/.codex/config.toml:\n[mcp_servers.ctxport]\ncommand = \"{cmd}\"\nargs = [\"serve-mcp\"]"),
        "gemini-cli": ("Gemini CLI", "Merge into ~/.gemini/settings.json:\n" + j),
        "antigravity": ("Antigravity", "Agent panel → … → MCP Servers → Manage → View raw config (mcp_config.json), merge:\n" + j),
        "cherry-studio": ("Cherry Studio", f"Settings → MCP → Add server → type STDIO\n  command: {cmd}\n  args: serve-mcp"),
        "chatbox": ("Chatbox", f"Settings → MCP → Add custom server (stdio)\n  command: {cmd} serve-mcp"),
    }
    keys = [client] if client else list(blocks)
    out = []
    for k in keys:
        if k not in blocks:
            return f"Unknown client {k!r}. Known: {', '.join(blocks)}"
        title, body = blocks[k]
        out.append(f"## {title}\n{body}\n")
    out.append("Tip: GUI apps often don't inherit your shell PATH. If `ctxport` is not found, use the absolute path"
               f" ({shutil.which('ctxport') or sys.executable + ' -m ctxport'}).")
    return "\n".join(out)


if __name__ == "__main__":
    serve()
