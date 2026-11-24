"""Gemini web/app via Google Takeout (My Activity → Gemini Apps, JSON format). Experimental.

Takeout has no conversation boundaries: entries closer than GAP seconds are merged.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from ..models import Conversation, ImportResult, Message, OutputFile
from ..utils import is_json_name, read_json_file, Bundle, parse_time, sanitize_raw, strip_html
from ._render import render_memories_plain
from .base import ExportOptions, Exporter, Importer, inbox_units, is_inbox, register

# "Prompted ", "Asked ", "已提问 ", "Gesendeter Prompt: " … — strip a short localized verb prefix.
_PREFIX = re.compile(r"^(Prompted|Asked|Searched for|Used|Said|已提问|提问了|已询问|已使用|Gesendeter Prompt:|A demandé|Preguntaste|"
                     r"Pediste|Prompt inviato|質問しました|プロンプト)\s*[:：]?\s*", re.I)


def gap_seconds() -> int:
    try:
        return int(os.environ.get("CTXPORT_TAKEOUT_GAP_MIN", "30")) * 60
    except ValueError:
        return 1800


@register
class GeminiWebImporter(Importer):
    name = "gemini-web"
    label = "Gemini web / app (Google Takeout)"
    experimental = True
    how_to_get = ("Automatic: ctxport browser extension (one click on gemini.google.com; real conversation "
                  "boundaries). Or Google Takeout → My Activity → Gemini Apps (JSON), then pass the .zip.")

    def default_roots(self) -> list:
        from ..ingest import inbox_dir
        return [inbox_dir(self.name)]

    def discover(self, root: Path) -> list:
        if Path(root).is_dir() and is_inbox(Path(root) / "x"):
            return inbox_units(root)
        return [Path(root)]

    def _load_scraped(self, data: dict, unit: Path) -> ImportResult:
        """Conversation scraped by the extension: {id, title, url, scrapedAt, messages:[{role, text, model?}]}."""
        res = ImportResult()
        conv = Conversation(platform=self.name, native_id=str(data.get("id")), title=data.get("title") or "",
                            source_path=str(unit), meta={"url": data.get("url"), "source": "extension-scrape"})
        ts = parse_time(data.get("updatedAt") or data.get("scrapedAt"))
        for m in data.get("messages") or []:
            role = "user" if m.get("role") == "user" else "assistant"
            kind = "reasoning" if m.get("kind") == "reasoning" else "message"
            msg = Message(role=role, text=m.get("text") or "", kind=kind, model=m.get("model"),
                          created_at=parse_time(m.get("time")))
            if m.get("attachments"):
                msg.meta["attachments"] = m["attachments"]
            conv.messages.append(msg)
        conv.updated_at = ts
        conv.created_at = parse_time(data.get("createdAt")) or ts
        res.conversations.append(conv.finalize())
        return res

    def load(self, unit: Path) -> ImportResult:
        res = ImportResult()
        unit = Path(unit)
        if unit.is_file() and is_json_name(unit.name) and unit.name != "MyActivity.json":
            data = read_json_file(unit)
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                return self._load_scraped(data, unit)
        with Bundle(unit) as b:
            files = b.find(r"MyActivity\.json$")
            if not files:
                if b.find(r"MyActivity\.html$"):
                    res.warnings.append("only MyActivity.html found — re-export Takeout with JSON format")
                else:
                    res.warnings.append("MyActivity.json not found")
                return res
            entries = []
            for name in files:
                data = b.read_json(name)
                for e in data if isinstance(data, list) else []:
                    if isinstance(e, dict):
                        entries.append(e)
        entries.sort(key=lambda e: parse_time(e.get("time")) or 0)
        gap = gap_seconds()
        current: list = []
        groups: list = []
        last = None
        for e in entries:
            t = parse_time(e.get("time")) or 0
            if current and last is not None and t - last > gap:
                groups.append(current)
                current = []
            current.append(e)
            last = t
        if current:
            groups.append(current)
        for g in groups:
            conv = Conversation(platform=self.name, native_id="", source_path=str(unit),
                                meta={"merged_from_takeout_entries": len(g), "gap_minutes": gap // 60})
            for e in g:
                ts = parse_time(e.get("time"))
                prompt = _PREFIX.sub("", e.get("title") or "").strip()
                atts = [{"name": a} if isinstance(a, str) else a for a in e.get("attachedFiles") or []]
                um = Message(role="user", text=prompt, created_at=ts, raw=sanitize_raw(e))
                if atts:
                    um.meta["attachments"] = atts
                conv.messages.append(um)
                answer = "\n\n".join(strip_html(x.get("html") or "") for x in e.get("safeHtmlItem") or []
                                     if isinstance(x, dict))
                if answer:
                    conv.messages.append(Message(role="assistant", text=answer, created_at=ts))
                for sub in e.get("subtitles") or []:
                    if isinstance(sub, dict) and sub.get("name"):
                        conv.meta.setdefault("subtitles", []).append(sub["name"])
            first_ts = conv.messages[0].created_at if conv.messages else 0
            conv.native_id = "takeout-" + hashlib.sha1(f"{first_ts}|{conv.messages[0].text if conv.messages else ''}"
                                                       .encode()).hexdigest()[:16]
            res.conversations.append(conv.finalize())
        return res


@register
class GeminiWebExporter(Exporter):
    name = "gemini-web"
    label = "Gemini web (Saved info)"
    instructions = ("Open gemini.google.com → Settings → Saved info (Personal context) and add the lines from "
                    "gemini-saved-info.txt, or paste them in a chat and ask Gemini to remember them.")

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        text = "Remember these facts about me / 请记住这些关于我的信息：\n\n" + render_memories_plain(memories, opts.redact)
        return [OutputFile("gemini-web/gemini-saved-info.txt", text)]
