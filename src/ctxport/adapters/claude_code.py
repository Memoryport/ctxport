"""Claude Code: ~/.claude/projects/<encoded-cwd>/<session>.jsonl (+ subagents/agent-*.jsonl).

Import: sessions, CLAUDE.md files, auto-memory (projects/*/memory/*.md).
Export: managed block in ~/.claude/CLAUDE.md (or <project>/CLAUDE.md with --project).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Conversation, ImportResult, Message
from ..utils import env_path, home, iter_jsonl, parse_time, sanitize_raw, compact_json
from ._instruction import InstructionFileExporter, read_instruction_file
from .base import Importer, register

INJECTED_TAGS = (
    "<system-reminder>", "<command-name>", "<command-message>", "<command-args>", "<local-command-stdout>",
    "<local-command-stderr>", "<local-command-caveat>", "<task-notification>", "<user-prompt-submit-hook>",
    "<bash-stdout>", "<bash-stderr>", "<ide_selection>", "<ide_opened_file>", "<session-start-hook>",
    "Caveat: The messages below were generated", "[Request interrupted", "<command-contents>",
)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
MESSAGE_TYPES = ("user", "assistant")


def claude_home() -> Path:
    return env_path("CLAUDE_CONFIG_DIR", home() / ".claude")


def classify_user_text(text: str) -> tuple:
    """Return (kind, cleaned_text, meta) for a user text block."""
    t = text or ""
    s = t.lstrip()
    if s.startswith("<bash-input>"):
        return "tool_call", re.sub(r"</?bash-input>", "", s).strip(), {"tool": "bash", "user_initiated": True}
    if s.startswith(("<bash-stdout>", "<bash-stderr>")):
        return "tool_result", re.sub(r"</?bash-std(out|err)>", "", s).strip(), {"tool": "bash"}
    if s.startswith(INJECTED_TAGS):
        cleaned = _REMINDER.sub("", t).strip()
        if cleaned and not cleaned.startswith(INJECTED_TAGS):
            return "message", cleaned, {"had_system_reminder": True}
        return "system_prompt", t, {"injected": True}
    if "<system-reminder>" in t:
        cleaned = _REMINDER.sub("", t).strip()
        if not cleaned:
            return "system_prompt", t, {"injected": True}
        return "message", cleaned, {"had_system_reminder": True}
    return "message", t, {}


def _is_user_prompt(r: dict) -> bool:
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"):
        return False
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return classify_user_text(c)[0] == "message"
    return any(isinstance(b, dict) and b.get("type") == "text" and classify_user_text(b.get("text") or "")[0] == "message"
               for b in c or [])


def _tool_result_text(content) -> tuple:
    atts = []
    if isinstance(content, str):
        return content, atts
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text") or "")
        elif b.get("type") == "image":
            src = b.get("source") or {}
            atts.append({"mime": src.get("media_type"), "size": int(len(src.get("data") or "") * 3 / 4) or None})
            parts.append("[image]")
    return "\n".join(parts), atts


@register
class ClaudeCodeImporter(Importer):
    name = "claude-code"
    label = "Claude Code"
    how_to_get = "Auto-detected: $CLAUDE_CONFIG_DIR or ~/.claude (projects/*/*.jsonl, CLAUDE.md, memory/)."

    def default_roots(self) -> list:
        return [claude_home()]

    def discover(self, root: Path) -> list:
        root = Path(root)
        if root.is_file():
            return [root]
        units = []
        if (root / "CLAUDE.md").is_file():
            units.append(root / "CLAUDE.md")
        proj = root / "projects" if (root / "projects").is_dir() else root
        units += sorted(proj.rglob("*.jsonl"))
        units += sorted(p for p in proj.glob("*/memory") if p.is_dir())
        return units

    # ------------------------------------------------------------------
    def load(self, unit: Path) -> ImportResult:
        unit = Path(unit)
        if unit.is_dir():
            return self._load_memory_dir(unit)
        if unit.suffix.lower() == ".md":
            res = ImportResult()
            res.memories = read_instruction_file(unit, self.name)
            return res
        return self._load_session(unit)

    def _load_memory_dir(self, d: Path) -> ImportResult:
        res = ImportResult()
        project = d.parent.name
        for f in sorted(d.glob("*.md")):
            if f.name.upper() == "MEMORY.MD":  # index of pointers, not facts
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            body = re.sub(r"(?s)^---\n.*?\n---\n", "", text).strip()
            mtype = re.search(r"(?m)^\s*type:\s*(\w+)", text)
            category = {"user": "identity", "feedback": "preference", "project": "project",
                        "reference": "general"}.get(mtype.group(1) if mtype else "", "general")
            from ._instruction import parse_instruction_text
            items = parse_instruction_text(body, self.name, category=category, scope=f"project:{project}",
                                           status="candidate", source_ref=str(f), confidence=0.7)
            res.memories += items
        return res

    def _load_session(self, path: Path) -> ImportResult:
        res = ImportResult()
        records = list(iter_jsonl(path, res.warnings))
        if not records:
            return res
        is_sub = path.parent.name == "subagents"
        session_id = next((r.get("sessionId") for r in records if r.get("sessionId")), None) or path.stem
        if is_sub:
            agent_id = next((r.get("agentId") for r in records if r.get("agentId")), path.stem)
            native = f"{session_id}/{path.stem if path.stem.startswith('agent-') else 'agent-' + str(agent_id)}"
        else:
            native = path.stem if re.fullmatch(r"[0-9a-f-]{36}", path.stem) else session_id

        title = summary = agent_name = None
        meta: dict = {}
        cwd = None
        for r in records:
            t = r.get("type")
            if t == "custom-title" and r.get("customTitle"):
                title = r["customTitle"]
            elif t == "summary" and r.get("summary"):
                summary = r["summary"]
            elif t == "agent-name" and r.get("agentName"):
                agent_name = r["agentName"]
            elif t == "pr-link" and r.get("prUrl"):
                meta.setdefault("pr_links", [])
                if r["prUrl"] not in meta["pr_links"]:
                    meta["pr_links"].append(r["prUrl"])
            if r.get("cwd") and not cwd:
                cwd = r["cwd"]
            if r.get("gitBranch"):
                meta["git_branch"] = r["gitBranch"]
            if r.get("version"):
                meta["cli_version"] = r["version"]
            if r.get("entrypoint"):
                meta["entrypoint"] = r["entrypoint"]

        # --- tree over every record that has a uuid
        nodes = {}
        order = {}
        for i, r in enumerate(records):
            u = r.get("uuid")
            if u:
                nodes[u] = r
                order[u] = i
        msg_recs = [r for r in records if r.get("type") in MESSAGE_TYPES and isinstance(r.get("message"), dict)]
        main_recs = [r for r in msg_recs if not r.get("isSidechain") or is_sub]
        side_recs = [r for r in msg_recs if r.get("isSidechain") and not is_sub]

        uuids_in_order = [r.get("uuid") for r in records if r.get("uuid")]
        pos = {u: i for i, u in enumerate(uuids_in_order)}
        on_path: set = set()
        if main_recs:
            cur, guard = main_recs[-1].get("uuid"), 0
            while cur and cur in nodes and guard < 1_000_000 and cur not in on_path:
                on_path.add(cur)
                n = nodes[cur]
                nxt = n.get("parentUuid") or n.get("logicalParentUuid")
                if nxt and nxt not in nodes and pos.get(cur, 0) > 0:
                    # dangling parent (record not written to this file): continue with the previous record
                    nxt = uuids_in_order[pos[cur] - 1]
                cur = nxt
                guard += 1
        off = [r for r in main_recs if r.get("uuid") and r.get("uuid") not in on_path]

        # group off-path records by the on-path node they fork from
        groups: dict = {}
        for r in off:
            cur, root, guard = r, r.get("uuid"), 0
            while guard < 100000:
                p = cur.get("parentUuid") or cur.get("logicalParentUuid")
                if not p or p in on_path or p not in nodes:
                    break
                root, cur = p, nodes[p]
                guard += 1
            groups.setdefault(root, []).append(r)
        # Parallel tool calls make sibling chains (each tool_result points at its own tool_use record).
        # Those are part of the visible conversation. Only a fork that contains a real user prompt
        # (rewind / edit) is an alternative branch.
        for root in list(groups):
            if not any(_is_user_prompt(r) for r in groups[root]):
                on_path.update(r.get("uuid") for r in groups.pop(root))
        main_list = [r for r in main_recs if r.get("uuid") in on_path or not r.get("uuid")]
        if main_recs and len(main_list) < 0.3 * len(main_recs) and len(main_recs) > 10:
            res.warnings.append(f"parent chain covers {len(main_list)}/{len(main_recs)} records; using file order")
            main_list, groups = main_recs, {}

        conv = Conversation(platform=self.name, native_id=native, project=cwd, source_path=str(path), meta=meta)
        conv.messages = self._records_to_messages(main_list)
        conv.branches = [self._records_to_messages(sorted(g, key=lambda x: order.get(x.get("uuid"), 0)))
                         for g in groups.values()]
        if side_recs:
            sb = self._records_to_messages(side_recs)
            for m in sb:
                m.meta["sidechain"] = True
            conv.branches.append(sb)
        if is_sub:
            conv.meta["is_subagent"] = True
            conv.meta["parent_id"] = f"{self.name}:{session_id}"
            first = next((m.text for m in conv.messages if m.role == "user" and m.kind == "message"), "")
            conv.title = "Subagent: " + (first[:70] if first else native)
        else:
            conv.title = title or summary or ""
            if agent_name:
                conv.meta["agent_name"] = agent_name
        res.conversations.append(conv.finalize())
        if cwd and not is_sub:
            res.memories += read_instruction_file(Path(cwd) / "CLAUDE.md", self.name, scope=f"project:{cwd}")
        return res

    # ------------------------------------------------------------------
    def _records_to_messages(self, recs: list) -> list:
        out: list = []
        last_text_by_mid: dict = {}
        for r in recs:
            msg = r.get("message") or {}
            role = msg.get("role") or r.get("type")
            ts = parse_time(r.get("timestamp"))
            uid = r.get("uuid")
            parent = r.get("parentUuid") or r.get("logicalParentUuid")
            model = msg.get("model") if msg.get("model") not in (None, "<synthetic>") else None
            raw = sanitize_raw(r)
            content = msg.get("content")
            base_meta: dict = {}
            if r.get("isSidechain"):
                base_meta["sidechain"] = True
            if role == "assistant" and msg.get("usage"):
                u = msg["usage"]
                base_meta["usage"] = {k: u.get(k) for k in ("input_tokens", "output_tokens",
                                                            "cache_read_input_tokens", "cache_creation_input_tokens")
                                      if u.get(k) is not None}
            if r.get("isMeta") or r.get("isCompactSummary"):
                text = content if isinstance(content, str) else "\n".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict))
                out.append(Message(role="system" if r.get("isMeta") else "user", text=text, id=uid, parent_id=parent,
                                   created_at=ts, kind="system_prompt", raw=raw,
                                   meta={**base_meta, "compact_summary": bool(r.get("isCompactSummary")),
                                         "is_meta": bool(r.get("isMeta"))}))
                continue
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            mid = msg.get("id")
            start = len(out)
            pending_atts: list = []
            for bi, b in enumerate(content or []):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                bid = f"{uid}#{bi}" if bi else uid
                if bt == "text":
                    text = b.get("text") or ""
                    if role == "user":
                        kind, text, extra = classify_user_text(text)
                        m = Message(role="user", text=text, id=bid, parent_id=parent, created_at=ts, kind=kind,
                                    raw=raw, meta={**base_meta, **extra})
                        if pending_atts and kind == "message":
                            m.meta["attachments"] = pending_atts
                            pending_atts = []
                        out.append(m)
                    else:
                        prev = last_text_by_mid.get(mid) if mid else None
                        if prev is not None and out and out[-1] is prev:
                            prev.text = (prev.text + "\n\n" + text).strip() if prev.text else text
                            continue
                        m = Message(role="assistant", text=text, id=bid, parent_id=parent, created_at=ts,
                                    model=model, raw=raw, meta=dict(base_meta, api_message_id=mid) if mid else base_meta)
                        out.append(m)
                        if mid:
                            last_text_by_mid[mid] = m
                elif bt in ("thinking", "redacted_thinking"):
                    txt = b.get("thinking") or ""
                    out.append(Message(role="assistant", text=txt, id=bid, parent_id=parent, created_at=ts,
                                       model=model, kind="reasoning",
                                       meta={**base_meta, "encrypted": not bool(txt)}))
                elif bt == "tool_use":
                    out.append(Message(role="assistant", text=compact_json(b.get("input")), id=bid, parent_id=parent,
                                       created_at=ts, model=model, kind="tool_call", raw=raw,
                                       meta={**base_meta, "tool": b.get("name"), "tool_use_id": b.get("id")}))
                elif bt == "tool_result":
                    txt, atts = _tool_result_text(b.get("content"))
                    out.append(Message(role="tool", text=txt, id=bid, parent_id=parent, created_at=ts,
                                       kind="tool_result", raw=raw,
                                       meta={**base_meta, "tool_use_id": b.get("tool_use_id"),
                                             "is_error": bool(b.get("is_error")),
                                             **({"attachments": atts} if atts else {})}))
                elif bt in ("image", "document"):
                    src = b.get("source") or {}
                    att = {"type": bt, "mime": src.get("media_type"),
                           "size": int(len(src.get("data") or "") * 3 / 4) or None,
                           "path": src.get("url") or src.get("file_id")}
                    if role == "user":
                        pending_atts.append(att)
                    else:
                        out.append(Message(role=role, text="", id=bid, parent_id=parent, created_at=ts,
                                           kind="attachment", meta={**base_meta, **att}))
            if pending_atts:
                # attachment-only user turn
                if out and out[-1].role == "user" and out[-1].kind == "message" and out[-1].id and \
                        str(out[-1].id).startswith(str(uid)):
                    out[-1].meta.setdefault("attachments", []).extend(pending_atts)
                else:
                    out.append(Message(role="user", text="", id=uid, parent_id=parent, created_at=ts,
                                       kind="attachment", meta={**base_meta, "attachments": pending_atts,
                                                               **pending_atts[0]}))
            # keep the raw record once per record, not once per content block
            seen_raw = False
            for m in out[start:]:
                if m.raw == raw and not seen_raw:
                    seen_raw = True
                elif m.raw == raw:
                    m.raw = None
        return out


@register
class ClaudeCodeExporter(InstructionFileExporter):
    name = "claude-code"
    label = "Claude Code (CLAUDE.md)"
    filename = "CLAUDE.md"
    instructions = ("Preview written. Re-run with --apply to update the ctxport block in ~/.claude/CLAUDE.md "
                    "(or <project>/CLAUDE.md with --project DIR). Your own content outside the block is kept "
                    "and the original is backed up as CLAUDE.md.ctxport.bak.")

    @property
    def global_path(self) -> Path:  # type: ignore[override]
        return claude_home() / "CLAUDE.md"
