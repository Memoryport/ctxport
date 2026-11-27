"""Generic formats and the archive exporters.

Importers: openai-messages ([{role, content}] / {messages:[...]} / lists of those), ctxport-jsonl (lossless).
Exporters: markdown (archive), ctxport-jsonl, json, csv, memory-md.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from ..models import Conversation, ImportResult, MemoryItem, Message, OutputFile
from ..redact import maybe
from ..utils import Bundle, fmt_time, parse_time, slugify, text_of
from ._render import render_conversation_md, render_memories_md
from .base import ExportOptions, Exporter, Importer, register


def _looks_like_messages(x) -> bool:
    return isinstance(x, list) and bool(x) and all(isinstance(m, dict) and "role" in m for m in x)


@register
class OpenAIMessagesImporter(Importer):
    name = "openai-messages"
    label = "OpenAI-style messages JSON (DeepSeek, Kimi, LM Studio, scripts …)"
    how_to_get = "Any JSON/JSONL file shaped like [{role, content}], {messages:[…]} or a list of those."

    def discover(self, root: Path) -> list:
        root = Path(root)
        if root.is_dir():
            return sorted(list(root.rglob("*.json")) + list(root.rglob("*.jsonl")))
        return [root]

    def load(self, unit: Path) -> ImportResult:
        res = ImportResult()
        unit = Path(unit)
        raw = unit.read_text(encoding="utf-8-sig", errors="replace")
        items = []
        if unit.suffix == ".jsonl":
            for line in raw.splitlines():
                if line.strip():
                    try:
                        items.append(json.loads(line))
                    except ValueError:
                        res.warnings.append("invalid JSONL line skipped")
            if items and all(isinstance(i, dict) and "role" in i for i in items):
                items = [items]
        else:
            data = json.loads(raw)
            if _looks_like_messages(data):
                items = [data]
            elif isinstance(data, dict):
                items = [data]
            elif isinstance(data, list):
                items = data
        for n, it in enumerate(items):
            if _looks_like_messages(it):
                msgs, title, cid, created = it, "", None, None
            elif isinstance(it, dict) and _looks_like_messages(it.get("messages")):
                msgs, title = it["messages"], it.get("title") or it.get("name") or ""
                cid, created = it.get("id"), parse_time(it.get("created_at") or it.get("created"))
            else:
                continue
            cid = cid or hashlib.sha1(f"{unit.name}|{n}".encode()).hexdigest()[:16]
            conv = Conversation(platform=self.name, native_id=str(cid), title=title, created_at=created,
                                source_path=str(unit), meta={"file": unit.name})
            for m in msgs:
                role = m.get("role")
                content = m.get("content")
                text = text_of(content)
                kind = "message"
                if role in ("system", "developer"):
                    kind = "system_prompt"
                elif role in ("tool", "function"):
                    kind = "tool_result"
                if m.get("tool_calls"):
                    conv.messages.append(Message(role="assistant", text=json.dumps(m["tool_calls"], ensure_ascii=False),
                                                 kind="tool_call", model=m.get("model")))
                if m.get("reasoning_content"):
                    conv.messages.append(Message(role="assistant", text=m["reasoning_content"], kind="reasoning"))
                if text or kind == "message":
                    conv.messages.append(Message(role=role, text=text, kind=kind, model=m.get("model"),
                                                 created_at=parse_time(m.get("timestamp") or m.get("created_at"))))
            # no time in the file: leave it unknown (the file's own date is when it was saved, not when the
            # conversation happened, and would make old chats look recent)
            res.conversations.append(conv.finalize())
        if not res.conversations:
            res.warnings.append("no messages found")
        return res


@register
class CtxportJSONLImporter(Importer):
    name = "ctxport-jsonl"
    label = "ctxport lossless JSONL"
    how_to_get = "Produced by `ctxport export ctxport-jsonl` (e.g. to move to another computer)."

    def load(self, unit: Path) -> ImportResult:
        res = ImportResult()
        with Bundle(unit) as b:
            for name in [n for n in b.names() if n.endswith(".jsonl")]:
                for i, line in enumerate(b.read(name).decode("utf-8", "replace").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        res.warnings.append(f"{name}:{i}: invalid JSON")
                        continue
                    if d.get("type") == "memory":
                        mem = d["memory"]
                        res.memories.append(MemoryItem(**{k: mem[k] for k in mem if k in MemoryItem.__dataclass_fields__}))
                    elif d.get("type") == "conversation":
                        res.conversations.append(Conversation.from_dict(d["conversation"]))
        return res


# ---------------------------------------------------------------- exporters

def conv_filename(conv: Conversation) -> str:
    date = fmt_time(conv.created_at or conv.updated_at, with_time=False)
    if date == "-":
        date = "undated"
    h = hashlib.sha1(conv.id.encode()).hexdigest()[:8]
    return f"{date}-{slugify(conv.title, 40)}-{h}"


@register
class MarkdownExporter(Exporter):
    name = "markdown"
    label = "Markdown archive (one file per conversation)"
    instructions = "Open markdown/INDEX.md. Every conversation is one .md file; branches are at the end of each file."

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        files = []
        index: dict = {}
        for conv in store.iter_conversations(platform=opts.platform, limit=opts.limit, since=opts.since,
                                             order="created_at DESC"):
            fn = f"markdown/{conv.platform}/{conv_filename(conv)}.md"
            files.append(OutputFile(fn, render_conversation_md(conv, opts.redact, opts.include_tools,
                                                               opts.include_reasoning)))
            n_user = sum(1 for m in conv.messages if m.role == "user" and m.kind == "message")
            index.setdefault(conv.platform, []).append((conv, fn[len("markdown/"):], n_user))
        lines = ["# ctxport archive / 对话归档", "",
                 f"{sum(len(v) for v in index.values())} conversations · generated {fmt_time(__import__('time').time())}", ""]
        for plat in sorted(index):
            lines += [f"## {plat} ({len(index[plat])})", "", "| Date | Title | Msgs | User | Branches |", "|---|---|---|---|---|"]
            for conv, rel, n_user in sorted(index[plat], key=lambda x: -(x[0].created_at or 0)):
                title = maybe(conv.title, opts.redact).replace("|", "\\|")
                lines.append(f"| {fmt_time(conv.created_at)} | [{title}]({rel.replace(' ', '%20')}) | "
                             f"{len(conv.messages)} | {n_user} | {len(conv.branches)} |")
            lines.append("")
        files.append(OutputFile("markdown/INDEX.md", "\n".join(lines)))
        files.append(OutputFile("markdown/MEMORY.md", render_memories_md(memories, opts.redact, 2,
                                                                         title="Memory / 记忆")))
        return files


@register
class CtxportJSONLExporter(Exporter):
    name = "ctxport-jsonl"
    label = "ctxport lossless JSONL"
    instructions = "Copy ctxport.jsonl to another computer and run `ctxport import ctxport-jsonl ctxport.jsonl`."

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        buf = io.StringIO()
        for conv in store.iter_conversations(platform=opts.platform, limit=opts.limit, since=opts.since,
                                             include_raw=opts.include_raw):
            buf.write(json.dumps({"type": "conversation", "conversation": conv.to_dict(opts.include_raw)},
                                 ensure_ascii=False) + "\n")
        for m in store.memories(status=None):
            buf.write(json.dumps({"type": "memory", "memory": m.to_dict()}, ensure_ascii=False) + "\n")
        return [OutputFile("ctxport-jsonl/ctxport.jsonl", buf.getvalue())]


@register
class JSONExporter(Exporter):
    name = "json"
    label = "Structured JSON (one file per conversation)"
    instructions = "One JSON file per conversation under json/<platform>/."

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        files = []
        for conv in store.iter_conversations(platform=opts.platform, limit=opts.limit, since=opts.since,
                                             include_raw=opts.include_raw):
            d = conv.to_dict(opts.include_raw)
            if opts.redact:
                for m in d["messages"] + [x for b in d["branches"] for x in b]:
                    m["text"] = maybe(m["text"], True)
            files.append(OutputFile(f"json/{conv.platform}/{conv_filename(conv)}.json",
                                    json.dumps(d, ensure_ascii=False, indent=1)))
        return files


@register
class CSVExporter(Exporter):
    name = "csv"
    label = "CSV conversation list"
    instructions = "conversations.csv lists every conversation (open in a spreadsheet)."

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "platform", "title", "project", "model", "created_at", "updated_at", "messages",
                    "user_messages", "chars"])
        for r in store.conversation_rows(opts.platform, opts.limit, opts.since):
            w.writerow([r["id"], r["platform"], maybe(r["title"] or "", opts.redact), r["project"] or "", r["model"] or "",
                        fmt_time(r["created_at"]), fmt_time(r["updated_at"]), r["message_count"], r["user_count"],
                        r["char_count"]])
        return [OutputFile("csv/conversations.csv", "﻿" + buf.getvalue())]


@register
class MemoryMarkdownExporter(Exporter):
    name = "memory-md"
    label = "Memory as Markdown"
    instructions = "memory/MEMORY.md — paste anywhere that accepts a profile or system prompt."

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        return [OutputFile("memory/MEMORY.md", render_memories_md(memories, opts.redact, 2, title="Memory / 记忆"))]
