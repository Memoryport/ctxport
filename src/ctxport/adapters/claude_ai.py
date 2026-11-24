"""Claude.ai: official export (Settings → Privacy → Export data).

conversations.json: [{uuid, name, created_at, updated_at, chat_messages:[{uuid, sender, text, content[], attachments, files, created_at}]}]
projects.json: [{uuid, name, prompt_template, docs:[{filename, content}]}]
memories.json: loosely structured; all strings are taken.
"""
from __future__ import annotations

from pathlib import Path

from ..models import Conversation, ImportResult, MemoryItem, Message, OutputFile
from ..utils import is_json_name, read_json_file, Bundle, compact_json, json_strings, parse_time, sanitize_raw, text_of
from ._instruction import parse_instruction_text
from ._render import render_memories_plain
from .base import ExportOptions, Exporter, Importer, inbox_units, is_account_file, is_inbox, register


def convert_message(cm: dict) -> list:
    role = "user" if cm.get("sender") == "human" else (cm.get("sender") or "unknown")
    ts = parse_time(cm.get("created_at"))
    mid = cm.get("uuid")
    parent = cm.get("parent_message_uuid")
    atts = []
    for a in cm.get("attachments") or []:
        atts.append({"name": a.get("file_name"), "mime": a.get("file_type"), "size": a.get("file_size"),
                     "extracted_chars": len(a.get("extracted_content") or "")})
    for f in cm.get("files") or cm.get("files_v2") or []:
        atts.append({"name": f.get("file_name"), "key": f.get("file_uuid") or f.get("uuid"), "mime": f.get("file_kind")})
    out: list = []
    blocks = cm.get("content") or []
    raw = sanitize_raw(cm)
    if not blocks:
        out.append(Message(role=role, text=cm.get("text") or "", id=mid, parent_id=parent, created_at=ts, raw=raw))
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        bid = mid if i == 0 else f"{mid}#{i}"
        bts = parse_time(b.get("start_timestamp")) or ts
        if bt == "text":
            if out and out[-1].kind == "message" and out[-1].role == role:
                out[-1].text = (out[-1].text + "\n\n" + (b.get("text") or "")).strip()
                continue
            out.append(Message(role=role, text=b.get("text") or "", id=bid, parent_id=parent, created_at=bts,
                               meta={"citations": b.get("citations")} if b.get("citations") else {}))
        elif bt in ("thinking", "redacted_thinking"):
            txt = b.get("thinking") or ""
            out.append(Message(role=role, text=txt, id=bid, parent_id=parent, created_at=bts, kind="reasoning",
                               meta={"encrypted": not txt}))
        elif bt in ("tool_use", "server_tool_use"):
            out.append(Message(role=role, text=compact_json(b.get("input")), id=bid, parent_id=parent, created_at=bts,
                               kind="tool_call", meta={"tool": b.get("name"), "tool_use_id": b.get("id")}))
        elif bt in ("tool_result", "web_search_tool_result"):
            out.append(Message(role="tool", text=text_of(b.get("content")), id=bid, parent_id=parent, created_at=bts,
                               kind="tool_result", meta={"tool": b.get("name"), "is_error": bool(b.get("is_error"))}))
        else:
            txt = text_of(b)
            if txt:
                out.append(Message(role=role, text=txt, id=bid, parent_id=parent, created_at=bts,
                                   kind="artifact" if "artifact" in str(bt) else "message", meta={"block_type": bt}))
    if not out:
        out.append(Message(role=role, text=cm.get("text") or "", id=mid, parent_id=parent, created_at=ts))
    out[0].raw = raw
    if atts:
        target = next((m for m in out if m.kind == "message"), out[0])
        target.meta["attachments"] = atts
    return out


def convert_conversation(c: dict, platform: str = "claude-ai") -> Conversation:
    msgs_raw = c.get("chat_messages") or []
    has_tree = any(m.get("parent_message_uuid") for m in msgs_raw)
    conv = Conversation(platform=platform, native_id=c.get("uuid") or str(c.get("created_at")),
                        title=c.get("name") or "", created_at=parse_time(c.get("created_at")),
                        updated_at=parse_time(c.get("updated_at")))
    if c.get("project_uuid") or c.get("project"):
        p = c.get("project") or {}
        conv.project = (p.get("name") if isinstance(p, dict) else None) or c.get("project_uuid")
    if c.get("summary"):
        conv.meta["summary"] = c["summary"]
    if has_tree:
        by_id = {m.get("uuid"): m for m in msgs_raw}
        children: dict = {}
        for m in msgs_raw:
            children.setdefault(m.get("parent_message_uuid"), []).append(m.get("uuid"))
        leaf = c.get("current_leaf_message_uuid") or (msgs_raw[-1].get("uuid") if msgs_raw else None)
        path, cur, seen = [], leaf, set()
        while cur in by_id and cur not in seen:
            seen.add(cur)
            path.append(cur)
            cur = by_id[cur].get("parent_message_uuid")
        path.reverse()
        main = set(path)
        for u in path:
            conv.messages += convert_message(by_id[u])
        for m in msgs_raw:
            if m.get("uuid") not in main:
                conv.branches.append(convert_message(m))
    else:
        for m in msgs_raw:
            conv.messages += convert_message(m)
    return conv.finalize()


@register
class ClaudeAIImporter(Importer):
    name = "claude-ai"
    label = "Claude.ai"
    how_to_get = ("Automatic: ctxport browser extension (one click on claude.ai, saves to ~/.ctxport/inbox/claude-ai). "
                  "Or the official export: Settings → Privacy → Export data, then pass the .zip.")

    def default_roots(self) -> list:
        from ..ingest import inbox_dir
        return [inbox_dir(self.name)]

    def discover(self, root: Path) -> list:
        if Path(root).is_dir() and is_inbox(Path(root) / "x"):
            return inbox_units(root)
        return [Path(root)]

    def load(self, unit: Path) -> ImportResult:
        res = ImportResult()
        unit = Path(unit)
        if unit.is_file() and is_json_name(unit.name) and unit.name != "conversations.json":
            data = read_json_file(unit)
            if isinstance(data, dict) and "chat_messages" in data:
                res.conversations.append(convert_conversation(data))
                return res
            if isinstance(data, dict) and is_account_file(unit):
                for p in data.get("projects") or []:
                    if p.get("prompt_template"):
                        res.memories += parse_instruction_text(p["prompt_template"], self.name,
                                                               category="custom_instructions",
                                                               scope=f"project:{p.get('name') or p.get('uuid')}",
                                                               source_ref=f"project:{p.get('uuid')}")
                for s in json_strings(data.get("memories") or [], 6):
                    res.memories += parse_instruction_text(s, self.name, category="platform_memory",
                                                           source_ref="claude-ai:memory")
                return res
        with Bundle(unit) as b:
            files = b.find(r"(^|/)conversations\.json$")
            if not files:
                res.warnings.append("no conversations.json found")
            for name in files:
                try:
                    data = b.read_json(name)
                except Exception as e:
                    res.warnings.append(f"{name}: {e}")
                    continue
                for c in data if isinstance(data, list) else [data]:
                    try:
                        res.conversations.append(convert_conversation(c))
                    except Exception as e:
                        res.warnings.append(f"{name}: conversation skipped: {type(e).__name__}: {e}")
            for name in b.find(r"(^|/)projects\.json$"):
                try:
                    projects = b.read_json(name)
                except Exception as e:
                    res.warnings.append(f"{name}: {e}")
                    continue
                for p in projects if isinstance(projects, list) else []:
                    pname = p.get("name") or p.get("uuid")
                    scope = f"project:{pname}"
                    if p.get("prompt_template"):
                        res.memories += parse_instruction_text(p["prompt_template"], self.name,
                                                               category="custom_instructions", scope=scope,
                                                               source_ref=f"project:{p.get('uuid')}")
                    docs = p.get("docs") or []
                    if docs or p.get("description"):
                        conv = Conversation(platform=self.name, native_id=f"project-{p.get('uuid')}",
                                            title=f"Project: {pname}", project=pname,
                                            created_at=parse_time(p.get("created_at")),
                                            updated_at=parse_time(p.get("updated_at")), meta={"project_docs": True})
                        if p.get("description"):
                            conv.messages.append(Message(role="system", text=p["description"], kind="artifact",
                                                         meta={"artifact": "project_description"}))
                        for d in docs:
                            conv.messages.append(Message(role="system", text=d.get("content") or "",
                                                         id=d.get("uuid"), created_at=parse_time(d.get("created_at")),
                                                         kind="artifact", meta={"name": d.get("filename")}))
                        res.conversations.append(conv.finalize())
            for name in b.find(r"memor[^/]*\.json$"):
                try:
                    data = b.read_json(name)
                except Exception as e:
                    res.warnings.append(f"{name}: {e}")
                    continue
                for s in json_strings(data, 6):
                    if len(s) > 400 or "\n" in s:
                        res.memories += parse_instruction_text(s, self.name, category="platform_memory",
                                                               source_ref=name)
                    else:
                        res.memories.append(MemoryItem(text=s, platform=self.name, category="platform_memory",
                                                       status="accepted", source_ref=name))
        return res


@register
class ClaudeAIExporter(Exporter):
    name = "claude-ai"
    label = "Claude.ai (memory import)"
    instructions = ("Open https://claude.ai/import-memory, paste the content of claude-memory-import.txt, "
                    "and click \"Add to memory\".")

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        body = render_memories_plain(memories, opts.redact)
        text = ("Here is what I'd like you to remember about me, exported from my other AI assistants:\n"
                "以下是我希望你记住的关于我的信息：\n\n" + body)
        return [OutputFile("claude-ai/claude-memory-import.txt", text, note="paste at claude.ai/import-memory")]
