"""Cherry Studio.

Sources:
  * 2.x  <userData>/Data/cherrystudio.sqlite (drizzle, snake_case: topic / message / assistant / agent_session*)
  * 1.x  <userData>/IndexedDB/file__0.indexeddb.leveldb (Dexie DB "CherryStudio": topics{id, messages[]},
         message_blocks) + topic names from Local Storage "persist:cherry-studio" — read with the pure-Python
         Chromium IndexedDB reader (auto-detected, no backup needed)
  * 1.x  <userData>/Data/agents.db (agent sessions: sessions + session_messages{content:{message, blocks}})
         — observed on real 1.9.x installs
  * backup zip v7: metadata.json + Data/cherrystudio.sqlite
  * backup zip 1.x: data.json → indexedDB.topics[].messages[] + indexedDB.message_blocks[]
Export: cherry-assistant-prompt.md (never writes Cherry's database).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from ..models import Conversation, ImportResult, MemoryItem, Message, OutputFile
from ..utils import Bundle, compact_json, has_table, home, parse_time, sanitize_raw, sqlite_copy, table_columns, text_of
from ._render import render_memories_md
from .base import ExportOptions, Exporter, Importer, register


def cherry_user_data() -> Path:
    if os.environ.get("CHERRY_STUDIO_HOME"):
        return Path(os.environ["CHERRY_STUDIO_HOME"]).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home() / "AppData/Roaming")) / "CherryStudio"
    if sys.platform == "darwin":
        return home() / "Library/Application Support/CherryStudio"
    return Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config")) / "CherryStudio"


def _loads(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def blocks_to_parts(blocks: list) -> tuple:
    """Cherry message blocks → (text, reasoning, tool_texts, attachments)."""
    texts, reasoning, tools, atts = [], [], [], []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        content = b.get("content")
        if bt in ("main_text", "text", "translation", "citation") and content:
            texts.append(text_of(content))
        elif bt == "code" and content:
            texts.append(f"```{b.get('language') or ''}\n{text_of(content)}\n```")
        elif bt in ("thinking", "reasoning") and content:
            reasoning.append(text_of(content))
        elif bt == "tool":
            tools.append({"tool": (b.get("toolName") or (b.get("metadata") or {}).get("rawMcpToolResponse", {}).get("tool", {}).get("name")),
                          "input": compact_json(b.get("arguments") or (b.get("metadata") or {}).get("rawMcpToolResponse", {}).get("arguments")),
                          "output": text_of(content) if content else ""})
        elif bt in ("image", "file"):
            f = b.get("file") or {}
            atts.append({"name": f.get("origin_name") or f.get("name"), "mime": f.get("type") or bt,
                         "size": f.get("size"), "path": f.get("path") or b.get("url")})
        elif bt == "error":
            texts.append(f"[error] {text_of(b.get('error') or content)}")
    return "\n\n".join(t for t in texts if t), "\n\n".join(reasoning), tools, atts


def message_from_blocks(msg: dict, blocks: list, ts_default=None) -> list:
    role = msg.get("role") or "unknown"
    ts = parse_time(msg.get("createdAt") or msg.get("created_at")) or ts_default
    model = (msg.get("model") or {}).get("id") if isinstance(msg.get("model"), dict) else (msg.get("modelId") or msg.get("model"))
    text, reasoning, tools, atts = blocks_to_parts(blocks)
    if not text and msg.get("content"):
        text = text_of(msg["content"])
    mid = msg.get("id")
    out = []
    if reasoning:
        out.append(Message(role="assistant", text=reasoning, id=f"{mid}#reasoning", created_at=ts, model=model,
                           kind="reasoning"))
    for i, t in enumerate(tools):
        out.append(Message(role="assistant", text=t["input"], id=f"{mid}#tool{i}", created_at=ts, model=model,
                           kind="tool_call", meta={"tool": t["tool"]}))
        if t["output"]:
            out.append(Message(role="tool", text=t["output"], id=f"{mid}#tool{i}:result", created_at=ts,
                               kind="tool_result", meta={"tool": t["tool"]}))
    m = Message(role=role, text=text, id=mid, parent_id=msg.get("askId"), created_at=ts,
                model=model if role == "assistant" else None, kind="message" if text or not atts else "attachment",
                raw=sanitize_raw({"message": msg, "blocks": blocks}))
    if atts:
        m.meta["attachments"] = atts
    if msg.get("usage"):
        m.meta["usage"] = msg["usage"]
    if msg.get("status"):
        m.meta["status"] = msg["status"]
    out.append(m)
    return out


def text_from_data(data: Any) -> tuple:
    """2.x message.data JSON → (text, reasoning)."""
    d = _loads(data)
    if isinstance(d, str):
        return d, ""
    if not isinstance(d, dict):
        return "", ""
    blocks = d.get("blocks") or d.get("parts") or []
    if blocks:
        text, reasoning, _tools, _atts = blocks_to_parts(
            [b if isinstance(b, dict) and "type" in b else {"type": "main_text", "content": text_of(b)} for b in blocks])
        if not text:
            text = "\n".join(text_of(b, ("text", "content", "main_text")) for b in blocks if isinstance(b, dict)
                             and b.get("type") not in ("thinking", "reasoning"))
        return text, reasoning
    return text_of(d, ("main_text", "text", "content")), ""


@register
class CherryStudioImporter(Importer):
    name = "cherry-studio"
    label = "Cherry Studio"
    how_to_get = ("Auto-detected: Cherry Studio data dir (2.x cherrystudio.sqlite, 1.x IndexedDB, agents.db). "
                  "Backup .zip files (Settings → Data settings → Backup) also work.")

    def default_roots(self) -> list:
        return [cherry_user_data()]

    def discover(self, root: Path) -> list:
        root = Path(root)
        if root.is_file():
            return [root]
        if root.name.endswith(".indexeddb.leveldb"):
            return [root]
        units = []
        for rel in ("Data/cherrystudio.sqlite", "cherrystudio.sqlite", "Data/agents.db", "agents.db"):
            if (root / rel).is_file():
                units.append(root / rel)
        if (root / "IndexedDB").is_dir():
            units += sorted((root / "IndexedDB").glob("*.indexeddb.leveldb"))
        return units

    def load(self, unit: Path) -> ImportResult:
        unit = Path(unit)
        if unit.is_dir() and unit.name.endswith(".indexeddb.leveldb"):
            return self._load_local_idb(unit)
        if unit.suffix in (".sqlite", ".db"):
            return self._load_sqlite(unit)
        res = ImportResult()
        with Bundle(unit) as b:
            names = b.names()
            sqlite_names = [n for n in names if n.endswith("cherrystudio.sqlite")]
            if sqlite_names:
                with b.materialize(sqlite_names[0]) as p:
                    res.extend(self._load_sqlite(p, source=str(unit)))
            for an in [n for n in names if n.endswith("agents.db")]:
                with b.materialize(an) as p:
                    res.extend(self._load_sqlite(p, source=str(unit)))
            dj = [n for n in names if n.split("/")[-1] == "data.json"]
            if dj:
                res.extend(self._load_v1(b.read_json(dj[0]), str(unit)))
            if not (sqlite_names or dj):
                res.warnings.append("no cherrystudio.sqlite or data.json in backup")
        return res

    # ------------------------------------------------------------ 1.x local IndexedDB
    def _load_local_idb(self, d: Path) -> ImportResult:
        from ..chromium.indexeddb import ChromiumIndexedDB, read_local_storage
        topics, blocks = [], []
        with ChromiumIndexedDB(d) as idb:
            for r in idb.records(database="CherryStudio"):
                if r.store == "topics" and isinstance(r.value, dict):
                    topics.append(r.value)
                elif r.store == "message_blocks" and isinstance(r.value, dict):
                    blocks.append(r.value)
            warnings = list(idb.warnings)
        local: dict = {}
        ls_dir = d.parent.parent / "Local Storage" / "leveldb"
        if ls_dir.is_dir():
            try:
                for (_origin, key), val in read_local_storage(ls_dir).items():
                    if key == "persist:cherry-studio":
                        local[key] = val
            except Exception as e:
                warnings.append(f"Local Storage unreadable (topic names missing): {e}")
        res = self._load_v1({"indexedDB": {"topics": topics, "message_blocks": blocks}, "localStorage": local}, str(d))
        res.warnings += warnings
        for c in res.conversations:
            c.meta["source"] = "cherry-local-indexeddb"
        return res

    # ------------------------------------------------------------ sqlite (2.x and agents.db)
    def _load_sqlite(self, path: Path, source: Optional[str] = None) -> ImportResult:
        res = ImportResult()
        with sqlite_copy(path) as db:
            if has_table(db, "topic") and has_table(db, "message"):
                self._load_v2(db, res, source or str(path))
            if has_table(db, "session_messages") and has_table(db, "sessions"):
                self._load_agents_db(db, res, source or str(path))
            for t in ("agent_session", "agent_sessions"):
                if has_table(db, t):
                    self._load_agent_session_tables(db, t, res, source or str(path))
            for t in ("assistant", "agents"):
                if has_table(db, t):
                    cols = table_columns(db, t)
                    pcol = "prompt" if "prompt" in cols else ("instructions" if "instructions" in cols else None)
                    if not pcol:
                        continue
                    where = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
                    for r in db.execute(f'SELECT * FROM "{t}"{where}'):
                        if r[pcol] and str(r[pcol]).strip():
                            res.memories.append(MemoryItem(
                                text=str(r[pcol]).strip()[:4000], platform=self.name, category="assistant_prompt",
                                status="candidate", confidence=0.5, source_ref=f"{t}:{r['name']}"))
        return res

    def _load_v2(self, db, res: ImportResult, source: str) -> None:
        tcols = table_columns(db, "topic")
        mcols = table_columns(db, "message")
        assistants = {}
        if has_table(db, "assistant"):
            for r in db.execute("SELECT * FROM assistant"):
                assistants[r["id"]] = r["name"]
        where = " WHERE deleted_at IS NULL" if "deleted_at" in tcols else ""
        for t in db.execute(f"SELECT * FROM topic{where}"):
            try:
                mwhere = " AND deleted_at IS NULL" if "deleted_at" in mcols else ""
                rows = db.execute(f"SELECT * FROM message WHERE topic_id=?{mwhere} ORDER BY created_at",
                                  (t["id"],)).fetchall()
                conv = self._tree_conversation(t, rows, assistants, source)
                if conv:
                    res.conversations.append(conv)
            except Exception as e:
                res.warnings.append(f"topic {t['id']}: {type(e).__name__}: {e}")

    def _row_message(self, r) -> list:
        keys = r.keys()
        text = (r["searchable_text"] if "searchable_text" in keys else "") or ""
        reasoning = ""
        if "data" in keys:
            dtext, reasoning = text_from_data(r["data"])
            text = text or dtext
        ts = parse_time(r["created_at"])
        out = []
        if reasoning:
            out.append(Message(role="assistant", text=reasoning, id=f"{r['id']}#reasoning", parent_id=r["parent_id"],
                               created_at=ts, kind="reasoning"))
        meta = {}
        for k in ("status", "siblings_group_id"):
            if k in keys and r[k] not in (None, "", 0):
                meta[k] = r[k]
        if "stats" in keys and r["stats"]:
            meta["stats"] = _loads(r["stats"])
        out.append(Message(role=r["role"], text=text, id=r["id"], parent_id=r["parent_id"], created_at=ts,
                           model=r["model_id"] if "model_id" in keys else None, meta=meta,
                           raw=sanitize_raw({k: r[k] for k in keys if k != "message_snapshot"})))
        return out

    def _tree_conversation(self, t, rows: list, assistants: dict, source: str) -> Optional[Conversation]:
        if not rows:
            return None
        tk = t.keys()
        by_id = {r["id"]: r for r in rows}
        children: dict = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r["id"])
        leaf = t["active_node_id"] if "active_node_id" in tk and t["active_node_id"] in by_id else None
        if not leaf:
            leaf = rows[-1]["id"]
        path, cur, seen = [], leaf, set()
        while cur in by_id and cur not in seen:
            seen.add(cur)
            path.append(cur)
            cur = by_id[cur]["parent_id"]
        path.reverse()
        main = set(path)
        conv = Conversation(platform=self.name, native_id=t["id"], title=t["name"] or "",
                            created_at=parse_time(t["created_at"]) if "created_at" in tk else None,
                            updated_at=parse_time(t["updated_at"]) if "updated_at" in tk else None,
                            project=assistants.get(t["assistant_id"]) if "assistant_id" in tk else None,
                            source_path=source)
        for mid in path:
            conv.messages += self._row_message(by_id[mid])
        # alternatives: every off-path subtree rooted at a child of a main-path node (or of nothing)
        off_roots = [r["id"] for r in rows if r["id"] not in main and (r["parent_id"] in main or r["parent_id"] not in by_id)]
        for root in off_roots:
            stack, branch = [root], []
            while stack:
                x = stack.pop(0)
                branch += self._row_message(by_id[x])
                stack += [c for c in children.get(x, []) if c not in main]
            if branch:
                conv.branches.append(branch)
        return conv.finalize()

    def _load_agents_db(self, db, res: ImportResult, source: str) -> None:
        sessions = {r["id"]: dict(r) for r in db.execute("SELECT * FROM sessions")}
        agents = {}
        if has_table(db, "agents"):
            agents = {r["id"]: dict(r) for r in db.execute("SELECT * FROM agents")}
        convs: dict = {}
        for r in db.execute("SELECT * FROM session_messages ORDER BY created_at, id"):
            sid = r["session_id"]
            s = sessions.get(sid, {})
            conv = convs.get(sid)
            if conv is None:
                agent = agents.get(s.get("agent_id"), {})
                conv = Conversation(platform=self.name, native_id=f"agent-session/{sid}", title=s.get("name") or "",
                                    created_at=parse_time(s.get("created_at")), updated_at=parse_time(s.get("updated_at")),
                                    project=agent.get("name") or s.get("agent_id"), model=s.get("model"),
                                    source_path=source, meta={"agent_session": True, "agent_type": s.get("agent_type"),
                                                              "accessible_paths": _loads(s.get("accessible_paths"))})
                if s.get("instructions"):
                    conv.messages.append(Message(role="system", text=s["instructions"], kind="system_prompt"))
                convs[sid] = conv
            content = _loads(r["content"])
            if isinstance(content, dict) and ("message" in content or "blocks" in content):
                msg = content.get("message") or {}
                msg.setdefault("role", r["role"])
                msg.setdefault("id", str(r["id"]))
                conv.messages += message_from_blocks(msg, content.get("blocks") or [], parse_time(r["created_at"]))
            else:
                conv.messages.append(Message(role=r["role"], text=text_of(content), id=str(r["id"]),
                                             created_at=parse_time(r["created_at"])))
        for c in convs.values():
            res.conversations.append(c.finalize())

    def _load_agent_session_tables(self, db, table: str, res: ImportResult, source: str) -> None:
        msg_table = next((t for t in (f"{table}_message", f"{table}_messages") if has_table(db, t)), None)
        if not msg_table:
            return
        mcols = table_columns(db, msg_table)
        fk = next((c for c in ("session_id", "agent_session_id") if c in mcols), None)
        if not fk:
            return
        for s in db.execute(f'SELECT * FROM "{table}"'):
            sk = s.keys()
            conv = Conversation(platform=self.name, native_id=f"{table}/{s['id']}",
                                title=(s["name"] if "name" in sk else "") or "",
                                created_at=parse_time(s["created_at"]) if "created_at" in sk else None,
                                updated_at=parse_time(s["updated_at"]) if "updated_at" in sk else None,
                                source_path=source, meta={"agent_session": True})
            for r in db.execute(f'SELECT * FROM "{msg_table}" WHERE {fk}=? ORDER BY created_at', (s["id"],)):
                rk = r.keys()
                content = _loads(r["content"]) if "content" in rk else (_loads(r["data"]) if "data" in rk else "")
                if isinstance(content, dict) and ("blocks" in content or "message" in content):
                    msg = dict(content.get("message") or {})
                    msg.setdefault("role", r["role"] if "role" in rk else "unknown")
                    msg.setdefault("id", str(r["id"]))
                    conv.messages += message_from_blocks(msg, content.get("blocks") or [], parse_time(r["created_at"]))
                else:
                    text = text_from_data(content)[0] if isinstance(content, dict) else text_of(content)
                    conv.messages.append(Message(role=r["role"] if "role" in rk else "unknown", text=text,
                                                 id=str(r["id"]), created_at=parse_time(r["created_at"])))
            if conv.messages:
                res.conversations.append(conv.finalize())

    # ------------------------------------------------------------ 1.x backup data.json
    def _load_v1(self, data: dict, source: str) -> ImportResult:
        res = ImportResult()
        idb = data.get("indexedDB") or {}
        blocks_by_msg: dict = {}
        for b in idb.get("message_blocks") or []:
            blocks_by_msg.setdefault(b.get("messageId"), []).append(b)
        names, assistants_by_topic, assistants = self._redux_topics(data.get("localStorage") or {})
        for a in assistants:
            if a.get("prompt"):
                res.memories.append(MemoryItem(text=a["prompt"].strip()[:4000], platform=self.name,
                                               category="assistant_prompt", status="candidate", confidence=0.5,
                                               source_ref=f"assistant:{a.get('name')}"))
        for t in idb.get("topics") or []:
            tid = t.get("id")
            meta_t = names.get(tid, {})
            conv = Conversation(platform=self.name, native_id=tid, title=meta_t.get("name") or t.get("name") or "",
                                created_at=parse_time(meta_t.get("createdAt")),
                                updated_at=parse_time(meta_t.get("updatedAt")),
                                project=assistants_by_topic.get(tid), source_path=source)
            for m in t.get("messages") or []:
                blocks = blocks_by_msg.get(m.get("id"))
                if blocks is None and m.get("blocks") and isinstance(m["blocks"][0], dict):
                    blocks = m["blocks"]
                if blocks is None:
                    blocks = []
                    if m.get("content"):
                        blocks = [{"type": "main_text", "content": m["content"]}]
                    if m.get("reasoning_content"):
                        blocks.append({"type": "thinking", "content": m["reasoning_content"]})
                conv.messages += message_from_blocks(m, blocks)
            if conv.messages:
                res.conversations.append(conv.finalize())
        return res

    @staticmethod
    def _redux_topics(local: dict) -> tuple:
        names: dict = {}
        by_topic: dict = {}
        assistants: list = []
        state = _loads(local.get("persist:cherry-studio"))
        if not isinstance(state, dict):
            return names, by_topic, assistants
        ast = _loads(state.get("assistants"))
        if isinstance(ast, dict):
            for a in ast.get("assistants") or []:
                assistants.append(a)
                for t in a.get("topics") or []:
                    names[t.get("id")] = t
                    by_topic[t.get("id")] = a.get("name")
        return names, by_topic, assistants


@register
class CherryStudioExporter(Exporter):
    name = "cherry-studio"
    label = "Cherry Studio (assistant prompt)"
    instructions = ("In Cherry Studio create a new assistant (e.g. \"Me\"), open its settings and paste "
                    "cherry-assistant-prompt.md as the prompt. To search your full history from Cherry, add the "
                    "ctxport MCP server: Settings → MCP → Add server → STDIO, command `ctxport`, args `serve-mcp` "
                    "(see `ctxport mcp-config cherry-studio`). ctxport never writes Cherry's database.")

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        text = ("You are my personal assistant. Use the following background about me (migrated with ctxport) "
                "when it is relevant.\n你是我的个人助手，请在相关时参考以下关于我的背景信息。\n\n"
                + render_memories_md(memories, opts.redact, heading_level=2))
        return [OutputFile("cherry-studio/cherry-assistant-prompt.md", text)]
