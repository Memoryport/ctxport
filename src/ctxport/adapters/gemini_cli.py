"""Gemini CLI: ~/.gemini/tmp/<project_hash>/chats/session-*.json|.jsonl, checkpoint-<tag>.json, GEMINI.md."""
from __future__ import annotations

import json
from pathlib import Path

from ..models import Conversation, ImportResult, Message
from ..utils import compact_json, env_path, home, parse_time, sanitize_raw, text_of
from ._instruction import InstructionFileExporter, read_instruction_file
from .base import Importer, register


def gemini_home() -> Path:
    return env_path("GEMINI_CLI_HOME", home() / ".gemini")


def parts_text(content) -> tuple:
    """PartListUnion → (text, thoughts, attachments, function_calls, function_responses)."""
    texts, thoughts, atts, calls, resps = [], [], [], [], []
    items = content if isinstance(content, list) else [content]
    for p in items:
        if p is None:
            continue
        if isinstance(p, str):
            texts.append(p)
            continue
        if not isinstance(p, dict):
            continue
        if p.get("thought") and p.get("text"):
            thoughts.append(p["text"])
        elif "text" in p:
            texts.append(p.get("text") or "")
        elif p.get("inlineData") or p.get("fileData"):
            d = p.get("inlineData") or p.get("fileData") or {}
            atts.append({"mime": d.get("mimeType"), "path": d.get("fileUri"),
                         "size": int(len(d.get("data") or "") * 3 / 4) or None})
        elif p.get("functionCall"):
            calls.append(p["functionCall"])
        elif p.get("functionResponse"):
            resps.append(p["functionResponse"])
    return "\n".join(t for t in texts if t), thoughts, atts, calls, resps


def convert_record_message(m: dict, model_default=None) -> list:
    typ = (m.get("type") or m.get("role") or "").lower()
    ts = parse_time(m.get("timestamp"))
    mid = m.get("id")
    model = m.get("model") or model_default
    content = m.get("content") if "content" in m else m.get("parts")
    text, thoughts, atts, calls, resps = parts_text(content)
    if not text and m.get("displayContent"):
        text = text_of(m["displayContent"])
    raw = sanitize_raw(m)
    out: list = []
    if typ in ("info", "error", "warning"):
        return [Message(role="system", text=text, id=mid, created_at=ts, kind="system_prompt",
                        meta={"level": typ}, raw=raw)]
    role = "user" if typ == "user" else "assistant"
    for th in m.get("thoughts") or []:
        t = th if isinstance(th, str) else ((th.get("subject") or "") + ("\n" if th.get("subject") else "")
                                             + (th.get("description") or th.get("text") or ""))
        out.append(Message(role="assistant", text=t.strip(), created_at=parse_time((th or {}).get("timestamp")
                                                                                   if isinstance(th, dict) else None) or ts,
                           model=model, kind="reasoning"))
    for t in thoughts:
        out.append(Message(role="assistant", text=t, created_at=ts, model=model, kind="reasoning"))
    if text or atts or not (calls or resps or m.get("toolCalls")):
        msg = Message(role=role, text=text, id=mid, created_at=ts, model=model if role == "assistant" else None,
                      kind="message" if text or not atts else "attachment", raw=raw)
        if atts:
            msg.meta["attachments"] = atts
        if m.get("tokens"):
            msg.meta["usage"] = m["tokens"]
        out.append(msg)
    for c in calls:
        out.append(Message(role="assistant", text=compact_json(c.get("args")), created_at=ts, model=model,
                           kind="tool_call", meta={"tool": c.get("name"), "call_id": c.get("id")}))
    for r in resps:
        out.append(Message(role="tool", text=text_of(r.get("response")) or compact_json(r.get("response")),
                           created_at=ts, kind="tool_result", meta={"tool": r.get("name"), "call_id": r.get("id")}))
    for tc in m.get("toolCalls") or []:
        tts = parse_time(tc.get("timestamp")) or ts
        out.append(Message(role="assistant", text=compact_json(tc.get("args")), id=tc.get("id"), created_at=tts,
                           model=model, kind="tool_call",
                           meta={"tool": tc.get("name") or tc.get("displayName"), "status": tc.get("status")}))
        result = tc.get("resultDisplay") or tc.get("result")
        if result:
            rt = result if isinstance(result, str) else (text_of(result, ("text", "output", "response", "content"))
                                                         or compact_json(result, 20000))
            out.append(Message(role="tool", text=rt, id=f"{tc.get('id')}:result", parent_id=tc.get("id"),
                               created_at=tts, kind="tool_result", meta={"tool": tc.get("name")}))
    if out:
        out[0].raw = out[0].raw or raw
    return out


@register
class GeminiCLIImporter(Importer):
    name = "gemini-cli"
    label = "Gemini CLI"
    how_to_get = "Auto-detected: ~/.gemini/tmp/<hash>/chats/session-*.json(l), checkpoint-*.json, ~/.gemini/GEMINI.md."

    def default_roots(self) -> list:
        return [gemini_home()]

    def discover(self, root: Path) -> list:
        root = Path(root)
        if root.is_file():
            return [root]
        units = []
        if (root / "GEMINI.md").is_file():
            units.append(root / "GEMINI.md")
        tmp = root / "tmp" if (root / "tmp").is_dir() else root
        units += sorted(tmp.glob("*/chats/session-*.json")) + sorted(tmp.glob("*/chats/session-*.jsonl"))
        units += sorted(tmp.glob("*/checkpoint-*.json"))
        if tmp == root:
            units += sorted(p for p in root.glob("session-*.json*") if p not in units)
        return units

    def load(self, unit: Path) -> ImportResult:
        unit = Path(unit)
        res = ImportResult()
        if unit.suffix.lower() == ".md":
            res.memories = read_instruction_file(unit, self.name)
            return res
        if unit.name.startswith("checkpoint-"):
            return self._checkpoint(unit)
        if unit.suffix == ".jsonl":
            record, msgs = self._read_jsonl(unit, res)
        else:
            record = json.loads(unit.read_text(encoding="utf-8", errors="replace"))
            msgs = record.get("messages") or [] if isinstance(record, dict) else record
            if not isinstance(record, dict):
                record = {}
        conv = Conversation(platform=self.name,
                            native_id=record.get("sessionId") or unit.stem.replace("session-", ""),
                            title=record.get("summary") or "", created_at=parse_time(record.get("startTime")),
                            updated_at=parse_time(record.get("lastUpdated")), source_path=str(unit))
        conv.project = record.get("projectHash") or unit.parent.parent.name
        conv.meta["project_hash"] = record.get("projectHash") or unit.parent.parent.name
        if record.get("kind") == "subagent":
            conv.meta["is_subagent"] = True
        if record.get("directories"):
            conv.meta["directories"] = record["directories"]
            conv.project = record["directories"][0] if record["directories"] else conv.project
        for m in msgs:
            try:
                conv.messages += convert_record_message(m)
            except Exception as e:
                res.warnings.append(f"message skipped: {e}")
        res.conversations.append(conv.finalize())
        return res

    def _read_jsonl(self, unit: Path, res: ImportResult) -> tuple:
        record: dict = {}
        msgs: list = []
        by_id: dict = {}
        with open(unit, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    res.warnings.append(f"line {n}: invalid JSON")
                    continue
                if not isinstance(o, dict):
                    continue
                if isinstance(o.get("messages"), list):  # full record snapshot
                    record.update({k: v for k, v in o.items() if k != "messages"})
                    msgs = list(o["messages"])
                    by_id = {m.get("id"): i for i, m in enumerate(msgs) if m.get("id")}
                elif o.get("type") in ("user", "gemini", "info", "error", "warning", "model") or "content" in o:
                    if o.get("id") in by_id:
                        msgs[by_id[o["id"]]] = o  # later write wins (streaming updates)
                    else:
                        if o.get("id"):
                            by_id[o["id"]] = len(msgs)
                        msgs.append(o)
                else:  # metadata line
                    record.update(o.get("$set") if isinstance(o.get("$set"), dict) else o)
        return record, msgs

    def _checkpoint(self, unit: Path) -> ImportResult:
        res = ImportResult()
        data = json.loads(unit.read_text(encoding="utf-8", errors="replace"))
        hist = data.get("history") if isinstance(data, dict) else data
        tag = unit.stem.replace("checkpoint-", "")
        conv = Conversation(platform=self.name, native_id=f"{unit.parent.name}/checkpoint-{tag}",
                            title=f"Checkpoint: {tag}", project=unit.parent.name, source_path=str(unit),
                            meta={"checkpoint": tag})
        mtime = unit.stat().st_mtime
        for item in hist or []:
            role = "user" if item.get("role") == "user" else "assistant"
            text, thoughts, atts, calls, resps = parts_text(item.get("parts") or [])
            for t in thoughts:
                conv.messages.append(Message(role="assistant", text=t, kind="reasoning"))
            if text or atts:
                conv.messages.append(Message(role=role, text=text, kind="message",
                                             meta={"attachments": atts} if atts else {}))
            for c in calls:
                conv.messages.append(Message(role="assistant", text=compact_json(c.get("args")), kind="tool_call",
                                             meta={"tool": c.get("name")}))
            for r in resps:
                conv.messages.append(Message(role="tool", text=compact_json(r.get("response")), kind="tool_result",
                                             meta={"tool": r.get("name")}))
        conv.created_at = conv.updated_at = mtime
        res.conversations.append(conv.finalize())
        return res


@register
class GeminiCLIExporter(InstructionFileExporter):
    name = "gemini-cli"
    label = "Gemini CLI (GEMINI.md)"
    filename = "GEMINI.md"
    instructions = ("Preview written. Re-run with --apply to update the ctxport block in ~/.gemini/GEMINI.md "
                    "(or <project>/GEMINI.md). Run /memory refresh in Gemini CLI afterwards.")

    @property
    def global_path(self) -> Path:  # type: ignore[override]
        return gemini_home() / "GEMINI.md"
