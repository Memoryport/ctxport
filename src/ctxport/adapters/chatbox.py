"""Chatbox backups (Settings → Data backup → Export).

V1: one JSON  {configVersion, __exported_items, __exported_at, chat-sessions-list:[{id,name}], "session:<id>": {...}}
    (older: "chat-sessions": [...], message.content string)
V2: zip with manifest.json + sessions/<path>/session.json (+ resources/)
V3: zip with sessions/<urlencoded id>.jsonl; lines {type: session|thread|message, ...}; settings.json,
    copilots.json, session-settings.json (agent-memories / copilot-memories → memories; the agent's own persona,
    agent-soul, describes the assistant and is not imported as a memory)
Local app (auto-detected): <userData>/IndexedDB/file__0.indexeddb.leveldb, localforage DB "chatboxstore",
store "keyvaluepairs" with the same keys as a V1 backup (values are JSON strings) — read with the pure-Python
Chromium IndexedDB reader. <userData>/config.json (electron-store) holds settings and, in old versions, sessions.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..models import Conversation, ImportResult, MemoryItem, Message, OutputFile
from ..utils import Bundle, home, json_strings, parse_time, sanitize_raw, text_of
from ._render import render_memories_md, visible
from .base import ExportOptions, Exporter, Importer, register


def _msg(m: dict) -> list:
    parts = m.get("contentParts")
    texts, atts, out = [], [], []
    reasoning = m.get("reasoningContent") or ""
    tools = []
    if isinstance(parts, list):
        for p in parts:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "text":
                texts.append(p.get("text") or "")
            elif pt == "image":
                atts.append({"key": p.get("storageKey") or p.get("url"), "mime": "image"})
            elif pt == "reasoning":
                reasoning = (reasoning + "\n" + (p.get("text") or "")).strip()
            elif pt in ("tool-call", "tool_call"):
                tools.append(p)
            elif pt == "info":
                texts.append(p.get("text") or "")
    elif m.get("content") is not None:
        texts.append(text_of(m.get("content")))
    for f in m.get("files") or []:
        atts.append({"name": f.get("name"), "mime": f.get("fileType"), "key": f.get("storageKey")})
    for p in m.get("pictures") or []:
        atts.append({"key": p.get("storageKey") or p.get("url"), "mime": "image"})
    ts = parse_time(m.get("timestamp") or m.get("createdAt"))
    model = m.get("model") or (m.get("aiProvider") and m.get("model"))
    role = m.get("role") or "unknown"
    if reasoning:
        out.append(Message(role="assistant", text=reasoning, id=f"{m.get('id')}#reasoning", created_at=ts,
                           model=model, kind="reasoning"))
    for i, t in enumerate(tools):
        out.append(Message(role="assistant", text=json.dumps(t.get("args"), ensure_ascii=False), created_at=ts,
                           id=f"{m.get('id')}#tool{i}", kind="tool_call", meta={"tool": t.get("toolName")}))
        if t.get("result") is not None:
            out.append(Message(role="tool", text=text_of(t.get("result")) or json.dumps(t.get("result"), ensure_ascii=False),
                               created_at=ts, id=f"{m.get('id')}#tool{i}:r", kind="tool_result",
                               meta={"tool": t.get("toolName")}))
    text = "\n".join(x for x in texts if x)
    kind = "system_prompt" if role == "system" else ("message" if text or not atts else "attachment")
    msg = Message(role=role, text=text, id=m.get("id"), created_at=ts, model=model, kind=kind, raw=sanitize_raw(m))
    if atts:
        msg.meta["attachments"] = atts
    if m.get("usage") or m.get("tokensUsed"):
        msg.meta["usage"] = m.get("usage") or {"tokens": m.get("tokensUsed")}
    if m.get("generating") or m.get("error"):
        msg.meta["error"] = m.get("error")
    out.append(msg)
    return out


def session_to_conv(s: dict, source: str, platform: str = "chatbox") -> Conversation:
    conv = Conversation(platform=platform, native_id=str(s.get("id")), title=s.get("name") or "",
                        source_path=source, meta={})
    if s.get("type"):
        conv.meta["session_type"] = s["type"]
    if s.get("starred"):
        conv.meta["starred"] = True
    if s.get("copilotId"):
        conv.project = s["copilotId"]
    if s.get("settings", {}).get("modelId") if isinstance(s.get("settings"), dict) else None:
        conv.model = s["settings"]["modelId"]
    for m in s.get("messages") or []:
        conv.messages += _msg(m)
    for th in s.get("threads") or []:
        b = []
        for m in th.get("messages") or []:
            b += _msg(m)
        for x in b:
            x.meta["thread"] = th.get("name") or th.get("id")
        if b:
            conv.branches.append(b)
    return conv.finalize()


def memories_from_settings(obj: Any, source: str, platform: str = "chatbox") -> list:
    out = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("agent-memories", "copilot-memories", "agentMemories", "copilotMemories"):
                    for s in json_strings(v, 4):
                        out.append(MemoryItem(text=s, platform=platform, category="platform_memory", status="accepted",
                                              source_ref=f"{source}:{k}"))
                elif k in ("agent-soul", "agentSoul"):
                    continue    # the assistant's persona ("Friendly tutor"), not a fact about the user
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(obj)
    return out


@register
class ChatboxImporter(Importer):
    name = "chatbox"
    label = "Chatbox"
    how_to_get = ("Auto-detected: the Chatbox app's local IndexedDB. Backups also work: Settings → Data backup → "
                  "Export, then pass the .json (V1) or .zip (V2/V3).")

    def default_roots(self) -> list:
        if os.environ.get("CHATBOX_HOME"):
            return [Path(os.environ["CHATBOX_HOME"]).expanduser()]
        names = ("xyz.chatboxapp.app", "xyz.chatboxapp.ce", "Chatbox")
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", home() / "AppData/Roaming"))
        elif sys.platform == "darwin":
            base = home() / "Library/Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config"))
        return [base / n for n in names]

    def discover(self, root: Path) -> list:
        root = Path(root)
        if root.is_file() or root.name.endswith(".indexeddb.leveldb"):
            return [root]
        units = sorted((root / "IndexedDB").glob("*.indexeddb.leveldb")) if (root / "IndexedDB").is_dir() else []
        if (root / "config.json").is_file():
            units.append(root / "config.json")
        return units

    def _load_local_idb(self, d: Path) -> ImportResult:
        from ..chromium.indexeddb import ChromiumIndexedDB
        data: dict = {}
        with ChromiumIndexedDB(d) as idb:
            for r in idb.records(store="keyvaluepairs"):
                v = r.value
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except ValueError:
                        pass
                if isinstance(r.key, str):
                    data[r.key] = v
            warnings = list(idb.warnings)
        res = self._v1(data, str(d))
        res.warnings += warnings
        for c in res.conversations:
            c.meta["source"] = "chatbox-local-indexeddb"
            if "demo" in c.native_id:
                c.meta["builtin_demo"] = True
        # copilot prompts ("You are a patient math tutor") describe an assistant, not the user: not memories
        return res

    def load(self, unit: Path) -> ImportResult:
        unit = Path(unit)
        if unit.is_dir():
            return self._load_local_idb(unit)
        res = ImportResult()
        with Bundle(unit) as b:
            if not b.is_zip:
                data = b.read_json(b.names()[0])
                return self._v1(data, str(unit))
            names = b.names()
            v3 = [n for n in names if n.startswith("sessions/") and n.endswith(".jsonl")]
            v2 = [n for n in names if n.endswith("session.json")]
            for cfg in ("settings.json", "copilots.json", "session-settings.json", "config.json"):
                for n in [x for x in names if x.split("/")[-1] == cfg]:
                    try:
                        res.memories += memories_from_settings(b.read_json(n), n)
                    except Exception as e:
                        res.warnings.append(f"{n}: {e}")
            if v3:
                for n in v3:
                    try:
                        res.conversations += self._v3_file(b.read(n).decode("utf-8", "replace"), n, str(unit), res)
                    except Exception as e:
                        res.warnings.append(f"{n}: {type(e).__name__}: {e}")
            elif v2:
                for n in v2:
                    try:
                        res.conversations.append(session_to_conv(b.read_json(n), str(unit)))
                    except Exception as e:
                        res.warnings.append(f"{n}: {type(e).__name__}: {e}")
            else:
                jsons = [n for n in names if n.endswith(".json")]
                for n in jsons:
                    try:
                        res.extend(self._v1(b.read_json(n), str(unit)))
                    except Exception as e:
                        res.warnings.append(f"{n}: {e}")
        return res

    def _v1(self, data: Any, source: str) -> ImportResult:
        res = ImportResult()
        if not isinstance(data, dict):
            res.warnings.append("unexpected backup shape")
            return res
        sessions = []
        if isinstance(data.get("chat-sessions"), list):
            sessions += data["chat-sessions"]
        listed = [x.get("id") for x in data.get("chat-sessions-list") or [] if isinstance(x, dict)]
        keys = [k for k in data if k.startswith("session:")]
        for k in keys:
            if isinstance(data[k], dict):
                sessions.append(data[k])
        seen = set()
        order = {sid: i for i, sid in enumerate(listed)}
        for s in sorted(sessions, key=lambda s: order.get(s.get("id"), 1e9)):
            if s.get("id") in seen:
                continue
            seen.add(s.get("id"))
            try:
                res.conversations.append(session_to_conv(s, source))
            except Exception as e:
                res.warnings.append(f"session {s.get('id')}: {type(e).__name__}: {e}")
        res.memories += memories_from_settings({k: v for k, v in data.items() if not k.startswith("session:")}, source)
        return res

    def _v3_file(self, text: str, name: str, source: str, res: ImportResult) -> list:
        session: dict = {}
        meta: dict = {}
        threads: dict = {}
        thread_order: list = []
        msgs_by_thread: dict = {}
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except ValueError:
                res.warnings.append(f"{name}:{n}: invalid JSON")
                continue
            t = o.get("type")
            if t == "session":
                session = o.get("session") or {}
                meta = o.get("meta") or {}
            elif t == "thread":
                th = o.get("thread") or {}
                tid = th.get("threadId") or th.get("id")
                threads[tid] = th
                thread_order.append(tid)
            elif t == "message":
                wrap = o.get("message") or {}
                tid = wrap.get("threadId")
                inner = wrap.get("message") or wrap
                msgs_by_thread.setdefault(tid, []).append(inner)
        sid = session.get("id") or unquote(Path(name).stem)
        main_tid = session.get("threadId") or session.get("currentThreadId")
        if main_tid not in msgs_by_thread:
            cands = [t for t in msgs_by_thread if t in (None, "", sid, "main", "current")]
            main_tid = cands[0] if cands else (thread_order[-1] if thread_order else next(iter(msgs_by_thread), None))
        conv = Conversation(platform=self.name, native_id=str(sid), title=session.get("name") or "",
                            created_at=parse_time(meta.get("createdAt") or session.get("createdAt")),
                            source_path=source, meta={"sort_order": meta.get("sortOrder")} if meta.get("sortOrder") is not None else {})
        if session.get("starred"):
            conv.meta["starred"] = True
        for m in msgs_by_thread.get(main_tid, []):
            conv.messages += _msg(m)
        for tid, ms in msgs_by_thread.items():
            if tid == main_tid:
                continue
            b = []
            for m in ms:
                b += _msg(m)
            th = threads.get(tid, {})
            for x in b:
                x.meta["thread"] = th.get("name") or tid
            if b:
                conv.branches.append(b)
        return [conv.finalize()]


@register
class ChatboxExporter(Exporter):
    name = "chatbox"
    label = "Chatbox (V1 backup + system prompt)"
    instructions = ("Chatbox → Settings → Data backup → Import → choose chatbox-import-v1.json (conversations only; "
                    "ids are prefixed ctxport- so existing chats are not overwritten). Paste chatbox-system-prompt.md "
                    "as the default system prompt (Settings → Chat → default prompt) or a copilot's prompt.")

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        from ..redact import maybe
        sessions = []
        data: dict = {"configVersion": 15, "__exported_items": ["conversations"],
                      "__exported_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
        for conv in store.iter_conversations(platform=opts.platform, limit=opts.limit, since=opts.since):
            sid = "ctxport-" + hashlib.sha1(conv.id.encode()).hexdigest()[:20]
            msgs = []
            for i, m in enumerate(visible(conv.messages, opts.include_tools, opts.include_reasoning)):
                if not (m.text or "").strip():
                    continue
                role = m.role if m.role in ("user", "assistant", "system") else ("assistant" if m.role == "tool" else "user")
                msgs.append({"id": f"{sid}-{i}", "role": role,
                             "contentParts": [{"type": "text", "text": maybe(m.text, opts.redact)}],
                             "timestamp": int((m.created_at or conv.created_at or time.time()) * 1000)})
            if not msgs:
                continue
            title = f"[{conv.platform}] {conv.title}"
            sessions.append({"id": sid, "name": title})
            data[f"session:{sid}"] = {"id": sid, "name": title, "type": "chat", "messages": msgs}
        data["chat-sessions-list"] = sessions
        prompt = ("Background about me (migrated with ctxport):\n关于我的背景信息：\n\n"
                  + render_memories_md(memories, opts.redact, heading_level=2))
        return [OutputFile("chatbox/chatbox-import-v1.json", json.dumps(data, ensure_ascii=False, indent=1)),
                OutputFile("chatbox/chatbox-system-prompt.md", prompt)]
