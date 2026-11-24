"""ChatGPT: official data export (zip or extracted folder).

conversations.json (or sharded conversations-000.json …): list of
{title, create_time, update_time, conversation_id, current_node, mapping{id:{id,parent,children,message}}}.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import Conversation, ImportResult, MemoryItem, Message, OutputFile
from ..utils import Bundle, compact_json, json_strings, parse_time, sanitize_raw
from ._render import render_memories_plain
from .base import ExportOptions, Exporter, Importer, inbox_units, is_inbox, register

CONV_FILE = r"(^|/)conversations(-\d+)?\.json$"


def _part_text(p) -> tuple:
    """(text, attachment|None) for one element of content.parts."""
    if isinstance(p, str):
        return p, None
    if isinstance(p, dict):
        ct = p.get("content_type") or ""
        if p.get("asset_pointer") or ct.endswith("asset_pointer"):
            meta = p.get("metadata") or {}
            return "", {"key": p.get("asset_pointer"), "mime": p.get("mime_type") or ct,
                        "size": p.get("size_bytes"), "width": p.get("width"), "height": p.get("height"),
                        "name": (meta.get("dalle") and "dalle") or None}
        if "text" in p:
            return str(p.get("text") or ""), None
    return "", None


def convert_message(node_id: str, parent: str, msg: dict) -> Message:
    author = (msg.get("author") or {})
    role = author.get("role") or "unknown"
    content = msg.get("content") or {}
    ct = content.get("content_type") or "text"
    md = msg.get("metadata") or {}
    meta: dict = {"content_type": ct}
    kind = "message"
    text = ""
    atts: list = []
    if ct in ("text", "multimodal_text"):
        for p in content.get("parts") or []:
            t, a = _part_text(p)
            if t:
                text = f"{text}\n{t}" if text else t
            if a:
                atts.append(a)
    elif ct == "code":
        text, kind = content.get("text") or "", "tool_call"
        meta["language"] = content.get("language")
    elif ct == "execution_output":
        text, kind = content.get("text") or "", "tool_result"
    elif ct in ("thoughts",):
        kind = "reasoning"
        text = "\n\n".join(
            (f"**{t.get('summary')}**\n" if t.get("summary") else "") + (t.get("content") or "")
            for t in content.get("thoughts") or [] if isinstance(t, dict))
    elif ct == "reasoning_recap":
        kind, text = "reasoning", content.get("content") or ""
    elif ct in ("tether_browsing_display", "tether_quote", "sonic_webpage", "system_error"):
        kind = "tool_result"
        text = content.get("result") or content.get("text") or content.get("summary") or ""
        if content.get("url") or content.get("title"):
            meta["citation"] = {"url": content.get("url"), "title": content.get("title"), "domain": content.get("domain")}
    elif ct == "user_editable_context":
        kind = "system_prompt"
        text = "\n\n".join(x for x in (content.get("user_profile"), content.get("user_instructions")) if x)
    elif ct == "model_editable_context":
        kind = "system_prompt"
        text = content.get("model_set_context") or ""
    else:
        text = content.get("text") or "\n".join(_part_text(p)[0] for p in content.get("parts") or [])
        if not text:
            text = compact_json({k: v for k, v in content.items() if k != "content_type"}, 4000)
        kind = "tool_result" if role == "tool" else "message"
    if role == "tool" and kind == "message":
        kind = "tool_result"
    if role == "system" and kind == "message":
        kind = "system_prompt"
    if md.get("is_visually_hidden_from_conversation") and kind == "message":
        kind = "system_prompt"
        meta["hidden"] = True
    if author.get("name"):
        meta["tool"] = author["name"]
    if md.get("attachments"):
        for a in md["attachments"]:
            atts.append({"name": a.get("name"), "mime": a.get("mime_type"), "size": a.get("size"), "key": a.get("id")})
    if md.get("citations"):
        meta["citations"] = [{"url": (c.get("metadata") or {}).get("url"), "title": (c.get("metadata") or {}).get("title")}
                             for c in md["citations"] if isinstance(c, dict)][:50]
    if atts:
        meta["attachments"] = atts
        if not text and kind == "message":
            kind = "attachment"
    if md.get("gizmo_id"):
        meta["gizmo_id"] = md["gizmo_id"]
    return Message(role=role, text=text, id=msg.get("id") or node_id, parent_id=parent,
                   created_at=parse_time(msg.get("create_time")), model=md.get("model_slug"), kind=kind, meta=meta,
                   raw=sanitize_raw(msg))


def convert_conversation(c: dict, platform: str = "chatgpt") -> tuple:
    """Returns (Conversation, custom-instruction memories)."""
    mapping = c.get("mapping") or {}
    mems: list = []
    msgs: dict = {}
    for nid, node in mapping.items():
        m = node.get("message")
        if m:
            msg = convert_message(nid, node.get("parent"), m)
            msgs[nid] = msg
            content = m.get("content") or {}
            if content.get("content_type") == "user_editable_context":
                for key in ("user_profile", "user_instructions"):
                    if content.get(key):
                        mems.append(MemoryItem(text=content[key].strip(), platform=platform,
                                               category="custom_instructions", status="accepted",
                                               source_ref=f"{platform}:{c.get('conversation_id') or c.get('id')}"))

    def path_to(nid) -> list:
        out, seen = [], set()
        while nid and nid in mapping and nid not in seen:
            seen.add(nid)
            out.append(nid)
            nid = mapping[nid].get("parent")
        return out[::-1]

    current = c.get("current_node")
    if not current or current not in mapping:
        leaves = [k for k, v in mapping.items() if not v.get("children")]
        current = max(leaves, key=lambda k: (msgs[k].created_at or 0) if k in msgs else 0) if leaves else None
    main_ids = path_to(current) if current else []
    main_set = set(main_ids)
    branches = []
    for nid, node in mapping.items():
        if node.get("children") or nid in main_set:
            continue
        p = path_to(nid)
        # only the part after the fork point
        fork = 0
        while fork < len(p) and p[fork] in main_set:
            fork += 1
        b = [msgs[x] for x in p[fork:] if x in msgs]
        if b:
            branches.append(b)
    conv = Conversation(
        platform=platform,
        native_id=c.get("conversation_id") or c.get("id") or str(c.get("create_time")),
        title=c.get("title") or "",
        created_at=parse_time(c.get("create_time")),
        updated_at=parse_time(c.get("update_time")),
        messages=[msgs[x] for x in main_ids if x in msgs],
        branches=sorted(branches, key=lambda b: b[0].created_at or 0),
    )
    gizmo = c.get("gizmo_id") or c.get("conversation_template_id")
    if gizmo:
        conv.meta["gizmo_id"] = gizmo
        if str(gizmo).startswith("g-p-"):
            conv.project = gizmo
    for k in ("is_archived", "default_model_slug", "is_starred", "workspace_id", "async_status"):
        if c.get(k) not in (None, "", False):
            conv.meta["archived" if k == "is_archived" else k] = c[k]
    if c.get("default_model_slug"):
        conv.model = c["default_model_slug"]
    return conv.finalize(), mems


@register
class ChatGPTImporter(Importer):
    name = "chatgpt"
    label = "ChatGPT"
    how_to_get = ("Automatic: ctxport browser extension (one click on chatgpt.com, saves to ~/.ctxport/inbox/chatgpt). "
                  "Or the official export: Settings → Data controls → Export data, then pass the .zip.")

    def default_roots(self) -> list:
        from ..ingest import inbox_dir
        return [inbox_dir(self.name)]

    def discover(self, root: Path) -> list:
        if Path(root).is_dir() and is_inbox(Path(root) / "x"):
            return inbox_units(root)
        return [Path(root)]

    def load(self, unit: Path) -> ImportResult:
        res = ImportResult()
        if Path(unit).name == "_account.json":
            return self._account(Path(unit))
        with Bundle(unit) as b:
            files = b.find(CONV_FILE) or ([b.names()[0]] if not b.is_zip and Path(unit).suffix == ".json" else [])
            if not files:
                res.warnings.append("no conversations*.json found")
            seen_mem = set()
            for name in files:
                try:
                    data = b.read_json(name)
                except Exception as e:
                    res.warnings.append(f"{name}: {e}")
                    continue
                if isinstance(data, dict):
                    data = data.get("conversations") or [data]
                for c in data or []:
                    try:
                        conv, mems = convert_conversation(c)
                    except Exception as e:
                        res.warnings.append(f"{name}: conversation skipped: {type(e).__name__}: {e}")
                        continue
                    res.conversations.append(conv)
                    for m in mems:
                        if m.id not in seen_mem:
                            seen_mem.add(m.id)
                            res.memories.append(m)
            for name in b.find(r"memor[^/]*\.json$"):
                try:
                    for s in json_strings(b.read_json(name), 6):
                        if len(s) <= 2000:
                            res.memories.append(MemoryItem(text=s, platform=self.name, category="platform_memory",
                                                           status="accepted", source_ref=name))
                except Exception as e:
                    res.warnings.append(f"{name}: {e}")
        return res


    def _account(self, p: Path) -> ImportResult:
        res = ImportResult()
        data = json.loads(p.read_text(encoding="utf-8"))
        for m in data.get("memories") or []:
            text = m.get("content") if isinstance(m, dict) else m
            if isinstance(text, str) and text.strip():
                res.memories.append(MemoryItem(text=text.strip(), platform=self.name, category="platform_memory",
                                               status="accepted", source_ref="chatgpt:memories"))
        ci = data.get("custom_instructions") or {}
        for key in ("about_user_message", "about_model_message"):
            if isinstance(ci, dict) and ci.get(key):
                res.memories.append(MemoryItem(text=ci[key].strip(), platform=self.name, category="custom_instructions",
                                               status="accepted", source_ref=f"chatgpt:{key}"))
        return res


@register
class ChatGPTExporter(Exporter):
    name = "chatgpt"
    label = "ChatGPT (paste text)"
    instructions = ("ChatGPT has no bulk memory import. Open chatgpt-memory.txt and either paste it into "
                    "Settings → Personalization → Custom instructions, or send it in a chat with Memory on and "
                    "ask ChatGPT to remember each line.")

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        body = render_memories_plain(memories, opts.redact)
        text = ("Please remember the following facts and preferences about me (migrated from my other AI tools):\n"
                "请记住以下关于我的信息和偏好：\n\n" + body)
        return [OutputFile("chatgpt/chatgpt-memory.txt", text, note="paste into ChatGPT")]
