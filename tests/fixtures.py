"""Synthetic fixtures. Shapes mirror the real on-disk formats recorded in docs/formats.md;
every string here is made up — no real conversation content."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path


def _w(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _jsonl(path: Path, rows: list) -> Path:
    return _w(path, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def _zip(path: Path, files: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            if isinstance(data, (bytes, bytearray)):
                z.writestr(name, data)
            else:
                z.writestr(name, data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
    return path


# ------------------------------------------------------------------ ChatGPT (branch tree)

def chatgpt_export(tmp: Path) -> Path:
    def node(i, parent, children, role, body, t, ct="text", **extra):
        msg = {"id": i, "author": {"role": role}, "create_time": t,
               "content": {"content_type": ct, "parts": [body]} if ct in ("text", "multimodal_text") else {"content_type": ct, **extra},
               "metadata": {"model_slug": "gpt-4o"} if role == "assistant" else {}}
        return {"id": i, "parent": parent, "children": children, "message": msg}

    mapping = {
        "root": {"id": "root", "parent": None, "children": ["sys"], "message": None},
        "sys": {"id": "sys", "parent": "root", "children": ["ctx"],
                "message": {"id": "sys", "author": {"role": "system"}, "create_time": None,
                            "content": {"content_type": "text", "parts": [""]},
                            "metadata": {"is_visually_hidden_from_conversation": True}}},
        "ctx": {"id": "ctx", "parent": "sys", "children": ["u1"],
                "message": {"id": "ctx", "author": {"role": "user"}, "create_time": 1700000000,
                            "content": {"content_type": "user_editable_context",
                                        "user_profile": "I am a physics student in Lisbon.",
                                        "user_instructions": "Answer concisely."},
                            "metadata": {"is_visually_hidden_from_conversation": True}}},
        "u1": node("u1", "ctx", ["a1", "a1b"], "user", "Explain induction", 1700000001.5),
        "a1": node("a1", "u1", ["u2"], "assistant", "Old answer (regenerated away)", 1700000002),
        "a1b": node("a1b", "u1", ["c1"], "assistant", "Induction proves P(n) for all n.", 1700000003),
        "c1": node("c1", "a1b", ["o1"], "assistant", None, 1700000004, ct="code", text="print(1+1)", language="python"),
        "o1": node("o1", "c1", ["u3"], "tool", None, 1700000005, ct="execution_output", text="2"),
        "u3": node("u3", "o1", ["a3"], "user", "Thanks!", 1700000006),
        "a3": node("a3", "u3", [], "assistant", "You're welcome.", 1700000007),
        "u2": node("u2", "a1", [], "user", "Abandoned follow-up", 1700000008),
    }
    mapping["u1"]["message"]["content"] = {"content_type": "multimodal_text",
                                           "parts": [{"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-abc",
                                                      "size_bytes": 1234, "width": 10, "height": 10}, "Explain induction"]}
    conv = {"title": "Induction", "create_time": 1700000000, "update_time": 1700000010, "conversation_id": "cg-1",
            "current_node": "a3", "mapping": mapping, "default_model_slug": "gpt-4o"}
    memories = {"memories": [{"content": "Prefers metric units"}, {"content": "Uses Haskell for coursework"}]}
    return _zip(tmp / "chatgpt-export.zip", {"conversations-000.json": [conv], "user.json": {"id": "u"},
                                              "memories.json": memories})


# ------------------------------------------------------------------ Claude.ai

def claude_ai_export(tmp: Path) -> Path:
    convs = [{
        "uuid": "ca-1", "name": "Rust lifetimes", "created_at": "2025-01-02T03:04:05.1234567Z",
        "updated_at": "2025-01-02T03:10:00Z",
        "chat_messages": [
            {"uuid": "m1", "sender": "human", "text": "What is a lifetime?", "created_at": "2025-01-02T03:04:05Z",
             "content": [{"type": "text", "text": "What is a lifetime?"}],
             "attachments": [{"file_name": "notes.txt", "file_type": "text/plain", "file_size": 12, "extracted_content": "x"}],
             "files": []},
            {"uuid": "m2", "sender": "assistant", "text": "", "created_at": "2025-01-02T03:04:09Z",
             "content": [{"type": "thinking", "thinking": "", "signature": "ENCRYPTED"},
                         {"type": "text", "text": "A lifetime is a scope"},
                         {"type": "tool_use", "name": "web_search", "input": {"q": "rust"}},
                         {"type": "tool_result", "name": "web_search", "content": [{"type": "text", "text": "result"}]},
                         {"type": "text", "text": "for references."}],
             "attachments": [], "files": []},
        ]}]
    projects = [{"uuid": "p1", "name": "Thesis", "prompt_template": "- Always cite sources\n- Use LaTeX",
                 "docs": [{"uuid": "d1", "filename": "outline.md", "content": "# Outline", "created_at": "2025-01-01T00:00:00Z"}]}]
    return _zip(tmp / "claude-export.zip", {"conversations.json": convs, "projects.json": projects,
                                             "memories.json": [{"conversations_memory": "User studies math.",
                                                                "project_memories": {"p1": "Thesis on graphs."}}]})


# ------------------------------------------------------------------ Claude Code

def claude_code_home(tmp: Path) -> Path:
    root = tmp / "claude"
    _w(root / "CLAUDE.md", "# Me\n\n- I prefer pytest\n- Reply in Chinese\n")
    proj = root / "projects" / "-tmp-proj"
    sid = "11111111-2222-3333-4444-555555555555"
    base = {"sessionId": sid, "cwd": str(tmp / "proj"), "gitBranch": "main", "version": "2.1.0", "isSidechain": False}
    rows = [
        {"type": "custom-title", "customTitle": "Fix the parser", "sessionId": sid},
        {**base, "type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-01-01T00:00:00.000Z",
         "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {**base, "type": "user", "uuid": "u2", "parentUuid": "u1", "timestamp": "2026-01-01T00:00:01.000Z",
         "isMeta": True, "message": {"role": "user", "content": "Caveat: The messages below were generated by the user"}},
        {**base, "type": "user", "uuid": "u3", "parentUuid": "u2", "timestamp": "2026-01-01T00:00:02.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Please fix the parser"},
                                                  {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}},
        {"type": "api-request-blob", "sessionId": sid, "hash": "h", "message": {"role": "user", "content": [{"type": "text", "text": "DUPLICATE"}]}},
        {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u3", "timestamp": "2026-01-01T00:00:03.000Z",
         "message": {"id": "msg_1", "role": "assistant", "model": "claude-sonnet-x",
                     "content": [{"type": "thinking", "thinking": "", "signature": "SIG"}]}},
        {**base, "type": "assistant", "uuid": "a2", "parentUuid": "a1", "timestamp": "2026-01-01T00:00:03.100Z",
         "message": {"id": "msg_1", "role": "assistant", "model": "claude-sonnet-x",
                     "content": [{"type": "text", "text": "Looking at it."}]}},
        {**base, "type": "assistant", "uuid": "a3", "parentUuid": "a2", "timestamp": "2026-01-01T00:00:03.200Z",
         "message": {"id": "msg_1", "role": "assistant", "model": "claude-sonnet-x",
                     "content": [{"type": "text", "text": "Found the bug."}]}},
        # parallel tool calls: two tool_use records, results point at their own tool_use record
        {**base, "type": "assistant", "uuid": "t1", "parentUuid": "a3", "timestamp": "2026-01-01T00:00:04.000Z",
         "message": {"id": "msg_2", "role": "assistant", "model": "claude-sonnet-x",
                     "content": [{"type": "tool_use", "id": "tu1", "name": "Read", "input": {"path": "a.py"}}]}},
        {**base, "type": "assistant", "uuid": "t2", "parentUuid": "t1", "timestamp": "2026-01-01T00:00:04.100Z",
         "message": {"id": "msg_2", "role": "assistant", "model": "claude-sonnet-x",
                     "content": [{"type": "tool_use", "id": "tu2", "name": "Read", "input": {"path": "b.py"}}]}},
        {**base, "type": "user", "uuid": "r1", "parentUuid": "t1", "timestamp": "2026-01-01T00:00:05.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "A"}]}},
        {**base, "type": "user", "uuid": "r2", "parentUuid": "t2", "timestamp": "2026-01-01T00:00:05.100Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu2", "content": "B"}]}},
        # an abandoned branch (user rewound and re-asked)
        {**base, "type": "user", "uuid": "x1", "parentUuid": "r2", "timestamp": "2026-01-01T00:00:06.000Z",
         "message": {"role": "user", "content": "Old question I rewound"}},
        {**base, "type": "assistant", "uuid": "x2", "parentUuid": "x1", "timestamp": "2026-01-01T00:00:07.000Z",
         "message": {"id": "msg_x", "role": "assistant", "model": "claude-sonnet-x", "content": [{"type": "text", "text": "Old answer"}]}},
        {**base, "type": "user", "uuid": "u4", "parentUuid": "r2", "timestamp": "2026-01-01T00:00:08.000Z",
         "message": {"role": "user", "content": "Now run the tests<system-reminder>internal</system-reminder>"}},
        {**base, "type": "system", "subtype": "compact_boundary", "uuid": "cb", "parentUuid": None,
         "logicalParentUuid": "u4", "timestamp": "2026-01-01T00:00:08.500Z", "content": "Conversation compacted"},
        {**base, "type": "assistant", "uuid": "a9", "parentUuid": "cb", "timestamp": "2026-01-01T00:00:09.000Z",
         "message": {"id": "msg_3", "role": "assistant", "model": "claude-sonnet-x",
                     "content": [{"type": "text", "text": "All tests pass."}]}},
        {**base, "type": "assistant", "uuid": "s1", "parentUuid": "a9", "isSidechain": True, "timestamp": "2026-01-01T00:00:10.000Z",
         "message": {"id": "msg_s", "role": "assistant", "model": "claude-sonnet-x", "content": [{"type": "text", "text": "sidechain note"}]}},
    ]
    _jsonl(proj / f"{sid}.jsonl", rows)
    sub = [{**base, "isSidechain": True, "agentId": "abc", "type": "user", "uuid": "su1", "parentUuid": None,
            "timestamp": "2026-01-01T00:01:00Z", "message": {"role": "user", "content": "Search the repo"}},
           {**base, "isSidechain": True, "agentId": "abc", "type": "assistant", "uuid": "sa1", "parentUuid": "su1",
            "timestamp": "2026-01-01T00:01:01Z",
            "message": {"id": "m", "role": "assistant", "model": "claude-haiku-x", "content": [{"type": "text", "text": "Found 3 files"}]}}]
    _jsonl(proj / sid / "subagents" / "agent-abc.jsonl", sub)
    _w(proj / "memory" / "user_role.md", "---\nname: role\ndescription: x\nmetadata:\n  type: user\n---\n\nUser is a first-year math student.\n")
    _w(proj / "memory" / "MEMORY.md", "- [Role](user_role.md) — pointer\n")
    _w(tmp / "proj" / "CLAUDE.md", "Use 4-space indentation.\n")
    return root


# ------------------------------------------------------------------ Codex

def codex_home(tmp: Path) -> Path:
    root = tmp / "codex"
    _w(root / "AGENTS.md", "- Always run tests before committing\n")
    tid = "01a00000-0000-7000-8000-000000000001"
    rows = [
        {"timestamp": "2026-02-01T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": tid, "cwd": "/work/app", "timestamp": "2026-02-01T10:00:00Z", "cli_version": "0.99.0",
                     "originator": "codex_cli_rs", "git": {"branch": "dev", "commit_hash": "abc"}}},
        {"timestamp": "2026-02-01T10:00:00.100Z", "type": "turn_context", "payload": {"model": "gpt-x", "cwd": "/work/app"}},
        {"timestamp": "2026-02-01T10:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<permissions instructions>sandbox</permissions instructions>"}]}},
        {"timestamp": "2026-02-01T10:00:01.100Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /work/app\n..."}]}},
        {"timestamp": "2026-02-01T10:00:01.200Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"timestamp": "2026-02-01T10:00:02.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": "# Files mentioned by the user:\n\n## spec.md: /work/app/spec.md\n\n## My request:\nAdd a login page"},
             {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}},
        {"timestamp": "2026-02-01T10:00:02.500Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Add a login page"}},
        {"timestamp": "2026-02-01T10:00:03.000Z", "type": "response_item",
         "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Plan the page"}], "encrypted_content": "XYZ"}},
        {"timestamp": "2026-02-01T10:00:04.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "arguments": "{\"cmd\":[\"ls\"]}", "call_id": "c1"}},
        {"timestamp": "2026-02-01T10:00:05.000Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1", "output": "{\"output\":\"src\\n\"}"}},
        {"timestamp": "2026-02-01T10:00:06.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done: added login."}]}},
        {"timestamp": "2026-02-01T10:00:07.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 1234}}}},
        {"timestamp": "2026-02-01T10:00:08.000Z", "type": "compacted", "payload": {"message": "summary of earlier turns"}},
    ]
    _jsonl(root / "sessions" / "2026" / "02" / "01" / f"rollout-2026-02-01T10-00-00-{tid}.jsonl", rows)
    # event_msg-only fallback session in archived_sessions (legacy style)
    tid2 = "01a00000-0000-7000-8000-000000000002"
    _jsonl(root / "archived_sessions" / f"rollout-2026-01-01T00-00-00-{tid2}.jsonl", [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": tid2, "cwd": "/w"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello there"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "hi!"}},
    ])
    # legacy bare-format session
    _jsonl(root / "sessions" / "2025" / "05" / "01" / "rollout-2025-05-01-legacy-0003.jsonl", [
        {"id": "legacy-3", "timestamp": "2025-05-01T00:00:00Z", "instructions": None},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "legacy question"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "legacy answer"}]},
    ])
    # thread DB: one thread whose rollout file no longer exists, plus one whose rollout exists (must be skipped)
    tid3 = "01a00000-0000-7000-8000-000000000003"
    st = sqlite3.connect(root / "state_5.sqlite")
    st.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, "
               "source TEXT, cwd TEXT, title TEXT, archived INTEGER DEFAULT 0, model TEXT, git_branch TEXT, "
               "thread_source TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, name TEXT)")
    st.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT)")
    st.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (tid3, str(root / "sessions/gone.jsonl"), 1767225600, 1767225700, "vscode", "/work/db", "Recovered thread",
                1, "gpt-y", "feat", "user", 1767225600000, 1767225700000, None))
    st.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (tid, str(root / f"sessions/2026/02/01/rollout-2026-02-01T10-00-00-{tid}.jsonl"), 1, 2, "cli", "/work/app",
                "dup", 0, "gpt-x", None, "user", None, None, None))
    st.commit()
    st.close()
    th = sqlite3.connect(root / "thread_history_1.sqlite")
    th.execute("CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER, "
               "created_at_ms INTEGER, item_json TEXT, item_type TEXT)")
    items = [
        (tid3, "t1", "i1", 1, 1767225600000, {"type": "userMessage", "id": "i1", "content": [{"type": "text", "text": "Recover me"},
                                                                                               {"type": "localImage", "path": "/tmp/x.png"}]}, "userMessage"),
        (tid3, "t1", "i2", 2, 1767225601000, {"type": "reasoning", "id": "i2", "summary": ["think"], "content": []}, "reasoning"),
        (tid3, "t1", "i3", 3, 1767225602000, {"type": "commandExecution", "id": "i3", "command": "ls", "aggregatedOutput": "a b",
                                              "exitCode": 0, "status": "completed"}, "commandExecution"),
        (tid3, "t1", "i4", 4, 1767225603000, {"type": "agentMessage", "id": "i4", "text": "Recovered answer", "phase": "final_answer"}, "agentMessage"),
        (tid, "t1", "j1", 1, 1, {"type": "userMessage", "id": "j1", "content": [{"type": "text", "text": "SHOULD NOT APPEAR"}]}, "userMessage"),
    ]
    th.executemany("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?)",
                   [(a, b, c, d, e, json.dumps(f), g) for a, b, c, d, e, f, g in items])
    th.commit()
    th.close()
    _w(root / "memories" / "memory_summary.md", "## User Profile\n- The user studies physics in Lisbon.\n")
    _w(root / "memories" / "raw_memories.md", "- raw, should be skipped\n")
    return root


# ------------------------------------------------------------------ Gemini CLI

def gemini_home(tmp: Path) -> Path:
    root = tmp / "gemini"
    _w(root / "GEMINI.md", "## Gemini Added Memories\n- My favourite editor is Vim\n")
    h = "abcdef0123"
    record = {"sessionId": "g-json", "projectHash": h, "startTime": "2026-03-01T00:00:00Z",
              "lastUpdated": "2026-03-01T00:05:00Z", "summary": "Gemini JSON session", "kind": "main",
              "messages": [
                  {"id": "1", "timestamp": "2026-03-01T00:00:01Z", "type": "user", "content": [{"text": "list files"}]},
                  {"id": "2", "timestamp": "2026-03-01T00:00:02Z", "type": "gemini", "content": [{"text": "Here they are"}],
                   "model": "gemini-x", "thoughts": [{"subject": "Plan", "description": "use ls"}],
                   "toolCalls": [{"id": "tc1", "name": "list_directory", "args": {"path": "."}, "status": "success",
                                  "resultDisplay": "a.txt"}]},
                  {"id": "3", "timestamp": "2026-03-01T00:00:03Z", "type": "info", "content": "Model switched"},
              ]}
    _w(root / "tmp" / h / "chats" / "session-2026-03-01T00-00-g-json.json", record)
    lines = [{"sessionId": "g-jsonl", "projectHash": h, "startTime": "2026-03-02T00:00:00Z", "kind": "main"},
             {"id": "a", "timestamp": "2026-03-02T00:00:01Z", "type": "user", "content": [{"text": "hello jsonl"}]},
             {"id": "b", "timestamp": "2026-03-02T00:00:02Z", "type": "gemini", "content": [{"text": "partial"}]},
             {"id": "b", "timestamp": "2026-03-02T00:00:02Z", "type": "gemini", "content": [{"text": "final reply"}]},
             {"$set": {"summary": "JSONL session"}}]
    _jsonl(root / "tmp" / h / "chats" / "session-2026-03-02T00-00-g-jsonl.jsonl", lines)
    _w(root / "tmp" / h / "checkpoint-mytag.json",
       [{"role": "user", "parts": [{"text": "checkpoint question"}]},
        {"role": "model", "parts": [{"text": "thinking…", "thought": True}, {"text": "checkpoint answer"}]}])
    return root


# ------------------------------------------------------------------ Antigravity (hand-built protobuf)

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ld(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _vi(field: int, v: int) -> bytes:
    return _varint(field << 3) + _varint(v)


def antigravity_home(tmp: Path) -> Path:
    g = tmp / "gemini_ag"
    d = g / "antigravity"
    cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def step(n, text):
        inner = _ld(1, text.encode()) + _ld(2, b"/Users/x/project/file.py") + _vi(3, 1)
        return _ld(5, _vi(1, n) + _ld(4, inner) + _ld(9, b"0f3a9c6e-1111-2222-3333-444455556666"))

    pb = _ld(1, cid.encode()) + step(1, "Please build a todo app") + step(2, "I created the todo app with React.") + \
        step(3, "请把按钮改成蓝色") + _vi(7, 42)
    (d / "conversations").mkdir(parents=True)
    (d / "conversations" / f"{cid}.pb").write_bytes(pb)
    _w(d / "brain" / cid / "task.md", "# Task\n- [x] build app\n")
    _w(d / "brain" / cid / "walkthrough.md", "Walkthrough text")
    _w(d / "knowledge" / "react-style" / "metadata.json", {"title": "React style", "summary": "User prefers function components."})
    _w(d / "knowledge" / "react-style" / "artifacts" / "notes.md", "Use hooks.")
    _w(d / "annotations" / f"{cid}.pbtxt", f'title: "Todo app build"\n')
    _w(g / "config" / "rules" / "style.md", "- Prefer TypeScript\n")
    _w(g / "AGENTS.md", "- Be brief\n")
    return g


# ------------------------------------------------------------------ Cherry Studio

def _cherry_sqlite(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE topic(id TEXT PRIMARY KEY, name TEXT, assistant_id TEXT, active_node_id TEXT, order_key TEXT,
      created_at INTEGER, updated_at INTEGER, deleted_at INTEGER);
    CREATE TABLE message(id TEXT PRIMARY KEY, parent_id TEXT, topic_id TEXT, role TEXT, data TEXT, searchable_text TEXT,
      status TEXT, siblings_group_id INTEGER, model_id TEXT, message_snapshot TEXT, stats TEXT,
      created_at INTEGER, updated_at INTEGER, deleted_at INTEGER);
    CREATE TABLE assistant(id TEXT PRIMARY KEY, name TEXT, prompt TEXT, emoji TEXT, description TEXT, model_id TEXT,
      settings TEXT, deleted_at INTEGER);
    CREATE TABLE agent_session(id TEXT PRIMARY KEY, name TEXT, created_at INTEGER, updated_at INTEGER);
    CREATE TABLE agent_session_message(id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, created_at INTEGER);
    """)
    db.execute("INSERT INTO assistant VALUES('as1','Tutor','You are a patient math tutor.','🧮','',NULL,NULL,NULL)")
    db.execute("INSERT INTO topic VALUES('t1','Calculus help','as1','m4',NULL,1767225600000,1767225900000,NULL)")
    db.execute("INSERT INTO topic VALUES('t2','Deleted topic','as1',NULL,NULL,1,2,123)")
    rows = [
        ("m1", None, "user", {"blocks": [{"type": "main_text", "content": "What is a derivative?"}]}, "What is a derivative?", None, 1767225600000),
        ("m2", "m1", "assistant", {"blocks": [{"type": "main_text", "content": "First answer"}]}, "", 1, 1767225601000),
        ("m3", "m1", "assistant", {"blocks": [{"type": "thinking", "content": "hmm"}, {"type": "main_text", "content": "A rate of change."}]}, "", 1, 1767225602000),
        ("m4", "m3", "user", {"parts": [{"text": "Thanks"}]}, "", None, 1767225603000),
        ("mx", "m3", "user", {}, "deleted reply", None, 1767225604000),
    ]
    for mid, parent, role, data, st, sg, ts in rows:
        db.execute("INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (mid, parent, "t1", role, json.dumps(data), st, "success", sg, "gpt-x" if role == "assistant" else None,
                    None, None, ts, ts, 99 if mid == "mx" else None))
    db.execute("INSERT INTO agent_session VALUES('s1','Agent run',1767225600000,1767225600000)")
    db.execute("INSERT INTO agent_session_message VALUES('am1','s1','user',?,1767225600000)",
               (json.dumps({"message": {"id": "am1", "role": "user"}, "blocks": [{"type": "main_text", "content": "agent task"}]}),))
    db.commit()
    db.close()
    return path


def cherry_home(tmp: Path) -> Path:
    root = tmp / "CherryStudio"
    _cherry_sqlite(root / "Data" / "cherrystudio.sqlite")
    a = sqlite3.connect(root / "Data" / "agents.db")
    a.executescript("""
    CREATE TABLE agents(id TEXT, type TEXT, name TEXT, instructions TEXT);
    CREATE TABLE sessions(id TEXT, agent_type TEXT, agent_id TEXT, name TEXT, instructions TEXT, model TEXT,
      accessible_paths TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE session_messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, metadata TEXT,
      created_at TEXT, updated_at TEXT, agent_session_id TEXT);
    """)
    a.execute("INSERT INTO agents VALUES('ag','claude-code','Coder','Write tests first')")
    a.execute("INSERT INTO sessions VALUES('se','claude-code','ag','Session A',NULL,'m:x','[]','2026-06-05T03:27:58.259Z','2026-06-05T03:28:58.259Z')")
    a.execute("INSERT INTO session_messages(session_id,role,content,created_at) VALUES('se','user',?,'2026-06-05T03:27:58.259Z')",
              (json.dumps({"message": {"id": "q", "role": "user"}, "blocks": [{"type": "main_text", "content": "agent db question"}]}),))
    a.execute("INSERT INTO session_messages(session_id,role,content,created_at) VALUES('se','assistant',?,'2026-06-05T03:27:59.259Z')",
              (json.dumps({"message": {"id": "r", "role": "assistant"}, "blocks": [{"type": "main_text", "content": "agent db answer"}]}),))
    a.commit()
    a.close()
    return root


def cherry_backup_v7(tmp: Path) -> Path:
    db = _cherry_sqlite(tmp / "cherry_v7_src" / "cherrystudio.sqlite")
    return _zip(tmp / "cherry-backup-v7.zip", {"metadata.json": {"version": 7}, "Data/cherrystudio.sqlite": db.read_bytes()})


def cherry_backup_v1(tmp: Path) -> Path:
    persist = {"assistants": json.dumps({"assistants": [{"id": "a", "name": "Default", "prompt": "Be kind",
                                                          "topics": [{"id": "tp", "name": "Old topic", "createdAt": "2025-05-01T00:00:00Z"}]}]})}
    data = {"time": 1, "version": 5, "localStorage": {"persist:cherry-studio": json.dumps(persist)},
            "indexedDB": {"topics": [{"id": "tp", "messages": [
                {"id": "x1", "role": "user", "createdAt": "2025-05-01T00:00:01Z", "blocks": ["b1"]},
                {"id": "x2", "role": "assistant", "createdAt": "2025-05-01T00:00:02Z", "blocks": ["b2", "b3"], "model": {"id": "deepseek"}}]}],
                "message_blocks": [{"id": "b1", "messageId": "x1", "type": "main_text", "content": "v1 question"},
                                   {"id": "b2", "messageId": "x2", "type": "thinking", "content": "v1 thinking"},
                                   {"id": "b3", "messageId": "x2", "type": "main_text", "content": "v1 answer"}]}}
    return _zip(tmp / "cherry-backup-v1.zip", {"data.json": data})


# ------------------------------------------------------------------ Chatbox

def _cb_session(sid, name, texts, threads=None):
    return {"id": sid, "name": name, "type": "chat",
            "messages": [{"id": f"{sid}-{i}", "role": r, "contentParts": [{"type": "text", "text": t}], "timestamp": 1767225600000 + i * 1000}
                         for i, (r, t) in enumerate(texts)],
            "threads": [{"id": "th", "name": "old thread", "messages": [
                {"id": f"{sid}-t{i}", "role": r, "contentParts": [{"type": "text", "text": t}], "timestamp": 1767225000000}
                for i, (r, t) in enumerate(th)]} for th in (threads or [])]}


def chatbox_v1(tmp: Path) -> Path:
    s = _cb_session("s1", "Chatbox chat", [("user", "cb question"), ("assistant", "cb answer")],
                    threads=[[("user", "older thread q"), ("assistant", "older thread a")]])
    old = {"id": "s0", "name": "Legacy", "messages": [{"id": "o1", "role": "user", "content": "legacy string content"}]}
    data = {"configVersion": 12, "__exported_items": ["conversations", "setting"], "__exported_at": "2026-01-01",
            "chat-sessions-list": [{"id": "s1", "name": "Chatbox chat"}], "session:s1": s, "chat-sessions": [old],
            "settings": {"agent-memories": ["User likes dark mode"]}}
    return _w(tmp / "chatbox-v1.json", data)


def chatbox_v2(tmp: Path) -> Path:
    s = _cb_session("s2", "V2 chat", [("user", "v2 question"), ("assistant", "v2 answer")])
    return _zip(tmp / "chatbox-v2.zip", {"manifest.json": {"version": 2}, "sessions/abc/session.json": s})


def chatbox_v3(tmp: Path) -> Path:
    lines = [{"type": "session", "session": {"id": "s/3", "name": "V3 chat", "threadId": "main"}, "meta": {"sortOrder": 1, "createdAt": 1767225600000}},
             {"type": "thread", "thread": {"sessionId": "s/3", "threadId": "main"}},
             {"type": "thread", "thread": {"sessionId": "s/3", "threadId": "old"}},
             {"type": "message", "message": {"sessionId": "s/3", "threadId": "main", "messageId": "1",
                                             "message": {"id": "1", "role": "user", "contentParts": [{"type": "text", "text": "v3 question"}], "timestamp": 1767225601000}}},
             {"type": "message", "message": {"sessionId": "s/3", "threadId": "main", "messageId": "2",
                                             "message": {"id": "2", "role": "assistant", "contentParts": [{"type": "text", "text": "v3 answer"}], "timestamp": 1767225602000}}},
             {"type": "message", "message": {"sessionId": "s/3", "threadId": "old", "messageId": "3",
                                             "message": {"id": "3", "role": "user", "contentParts": [{"type": "text", "text": "v3 old thread"}], "timestamp": 1767225500000}}}]
    return _zip(tmp / "chatbox-v3.zip", {
        "sessions/s%2F3.jsonl": "\n".join(json.dumps(x) for x in lines),
        "settings.json": {"agent-memories": ["Prefers short answers"], "agent-soul": "Friendly tutor"},
        "copilots.json": [{"id": "c", "copilot-memories": ["Studies calculus"]}]})


# ------------------------------------------------------------------ Takeout

def takeout(tmp: Path) -> Path:
    items = [
        {"header": "Gemini Apps", "title": "Prompted What is a prime?", "time": "2026-04-01T10:00:00.000Z",
         "safeHtmlItem": [{"html": "<p>A prime has <b>two</b> divisors.</p>"}]},
        {"header": "Gemini Apps", "title": "Prompted Give an example", "time": "2026-04-01T10:05:00.000Z",
         "safeHtmlItem": [{"html": "<p>7 is prime.</p>"}], "attachedFiles": ["photo.jpg"]},
        {"header": "Gemini Apps", "title": "已提问 你好", "time": "2026-04-02T09:00:00.000Z",
         "safeHtmlItem": [{"html": "你好！"}]},
    ]
    return _zip(tmp / "takeout.zip", {"Takeout/我的活动/Gemini Apps/MyActivity.json": items})


def openai_messages(tmp: Path) -> Path:
    return _w(tmp / "deepseek.json", [{"title": "DS chat", "messages": [
        {"role": "system", "content": "You are helpful"}, {"role": "user", "content": "ds question"},
        {"role": "assistant", "content": "ds answer", "reasoning_content": "ds reasoning"}]}])
