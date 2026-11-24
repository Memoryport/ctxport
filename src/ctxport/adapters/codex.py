"""Codex (CLI / IDE extension / desktop app).

Sources (all under $CODEX_HOME or ~/.codex):
  * sessions/YYYY/MM/DD/rollout-*.jsonl and archived_sessions/ — rollout files
  * thread_history_N.sqlite (+ state_N.sqlite for titles/metadata) — newer Codex builds keep the
    full thread history here; many threads have no rollout file any more (observed on real data)
  * AGENTS.md, memories/ and memories_v2/ (model-generated, imported as candidates)
Export: managed block in ~/.codex/AGENTS.md (or <project>/AGENTS.md).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..models import Conversation, ImportResult, Message
from ..utils import compact_json, env_path, has_table, home, iter_jsonl, parse_time, sanitize_raw, sqlite_copy, text_of
from ._instruction import InstructionFileExporter, parse_instruction_text, read_instruction_file
from .base import Importer, register

INJECTED_PREFIXES = (
    "<environment_context>", "<user_instructions>", "# AGENTS.md instructions", "<permissions instructions>",
    "<user_shell_command>", "<turn_aborted>", "<external_codex_apps_", "<INSTRUCTIONS>", "<collaboration_mode>",
    "<developer_instructions>", "<skill>", "<subagent_notification>", "<app-context>", "<apps_instructions>",
)
_FILES_HDR = re.compile(r"^\s*# Files (?:mentioned|pasted|attached) by the user:\s*\n", re.I)
_MY_REQ = re.compile(r"^## My request(?: for Codex)?:\s*$", re.M)


def codex_home() -> Path:
    return env_path("CODEX_HOME", home() / ".codex")


def classify_user_text(text: str) -> tuple:
    """(kind, text, meta) for a Codex user text part."""
    t = text or ""
    s = t.lstrip()
    if s.startswith(INJECTED_PREFIXES):
        return "system_prompt", t, {"injected": True}
    meta: dict = {}
    if _FILES_HDR.match(s):
        m = _MY_REQ.search(s)
        head = s[: m.start()] if m else s
        files = []
        for line in head.splitlines():
            fm = re.match(r"^## (.+?): (\S.*)$", line.strip())
            if fm:
                files.append({"name": fm.group(1), "path": fm.group(2)})
        meta["attachments"] = files
        if m:
            s = s[m.end():].strip()
        else:
            return "attachment", "", meta
        t = s
    sm = re.fullmatch(r"\s*<send_user_message_question_reply>(.*)</send_user_message_question_reply>\s*", t, re.S)
    if sm:
        t = sm.group(1).strip()
        meta["question_reply"] = True
    return "message", t, meta


def _tool_output_text(out) -> str:
    if isinstance(out, str):
        try:
            j = json.loads(out)
            if isinstance(j, dict) and "output" in j:
                return text_of(j["output"])
        except (ValueError, TypeError):
            pass
        return out
    if isinstance(out, dict) and "output" in out:
        return text_of(out["output"])
    if isinstance(out, list):
        return "\n".join(text_of(x, ("text", "output")) for x in out if x)
    return text_of(out) or compact_json(out)


@register
class CodexImporter(Importer):
    name = "codex"
    label = "Codex (CLI / IDE / App)"
    how_to_get = "Auto-detected: $CODEX_HOME or ~/.codex (rollout JSONL, thread_history_*.sqlite, AGENTS.md, memories/)."

    def default_roots(self) -> list:
        return [codex_home()]

    def discover(self, root: Path) -> list:
        root = Path(root)
        if root.is_file():
            return [root]
        units: list = []
        if (root / "AGENTS.md").is_file():
            units.append(root / "AGENTS.md")
        for sub in ("sessions", "archived_sessions"):
            if (root / sub).is_dir():
                units += sorted((root / sub).rglob("*.jsonl"))
        if not units and any(root.glob("*.jsonl")):
            units += sorted(root.glob("*.jsonl"))
        hist = _newest(root, "thread_history_*.sqlite")
        if hist:
            units.append(hist)
        for mdir in ("memories", "memories_v2"):
            d = root / mdir
            if d.is_dir():
                for f in sorted(d.iterdir()):
                    if f.is_file() and f.suffix.lower() in (".md", ".txt", ".json") and f.name != "raw_memories.md":
                        units.append(f)
        return units

    def load(self, unit: Path) -> ImportResult:
        unit = Path(unit)
        if unit.suffix == ".sqlite":
            return self._load_thread_db(unit)
        if unit.suffix.lower() in (".md", ".txt", ".json"):
            return self._load_memory_file(unit)
        return self._load_rollout(unit)

    # ------------------------------------------------------------ memories
    def _load_memory_file(self, f: Path) -> ImportResult:
        res = ImportResult()
        if f.name == "AGENTS.md" and f.parent.name not in ("memories", "memories_v2"):
            res.memories = read_instruction_file(f, self.name)
            return res
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix.lower() == ".json":
            from ..utils import json_strings
            try:
                items = json_strings(json.loads(text), 8)
            except ValueError:
                items = []
            text = "\n".join(f"- {i}" for i in items)
        res.memories = parse_instruction_text(text, self.name, category="platform_memory", status="candidate",
                                              source_ref=str(f), confidence=0.6, max_len=600)
        return res

    # ------------------------------------------------------------ rollout jsonl
    def _load_rollout(self, path: Path) -> ImportResult:
        res = ImportResult()
        recs = list(iter_jsonl(path, res.warnings))
        if not recs:
            return res
        meta: dict = {}
        native = None
        cwd = None
        model = None
        created = None
        msgs: list = []
        fallback: list = []
        have_items = False
        tokens = None
        archived = "archived_sessions" in path.parts
        for i, r in enumerate(recs):
            if "payload" in r and isinstance(r.get("payload"), dict):
                typ, pl = r.get("type"), r["payload"]
            else:  # legacy: bare items, first line may be session meta
                typ, pl = ("session_meta", r) if (i == 0 and "id" in r and "type" not in r) else ("response_item", r)
            ts = parse_time(r.get("timestamp")) or parse_time(pl.get("timestamp")) \
                or parse_time((pl.get("internal_chat_message_metadata_passthrough") or {}).get("create_time"))
            if typ == "session_meta":
                native = pl.get("id") or native
                cwd = pl.get("cwd") or cwd
                created = parse_time(pl.get("timestamp")) or created
                for k in ("cli_version", "originator", "source", "thread_source", "model_provider"):
                    if pl.get(k) not in (None, ""):
                        meta[k] = pl[k] if not isinstance(pl[k], dict) else compact_json(pl[k], 300)
                if isinstance(pl.get("git"), dict):
                    meta["git_branch"] = pl["git"].get("branch")
                    meta["git_commit"] = pl["git"].get("commit_hash")
                    meta["git_repo"] = pl["git"].get("repository_url")
                ui = pl.get("instructions") or pl.get("user_instructions")
                if isinstance(ui, str) and ui.strip():
                    msgs.append(Message(role="system", text=ui, created_at=ts, kind="system_prompt",
                                        meta={"source": "session_meta.instructions"}))
            elif typ == "turn_context":
                model = pl.get("model") or model
                if pl.get("approval_policy"):
                    meta["approval_policy"] = pl.get("approval_policy")
            elif typ == "compacted":
                msgs.append(Message(role="system", text=text_of(pl.get("message") or pl), created_at=ts,
                                    kind="system_prompt", meta={"compacted": True}, raw=sanitize_raw(r)))
            elif typ == "event_msg":
                et = pl.get("type")
                if et == "token_count" and isinstance(pl.get("info"), dict):
                    tokens = pl["info"].get("total_token_usage") or tokens
                elif et == "user_message" and pl.get("message"):
                    kind, text, extra = classify_user_text(pl["message"])
                    fallback.append(Message(role="user", text=text, created_at=ts, kind=kind, meta=extra))
                elif et == "agent_message" and pl.get("message"):
                    fallback.append(Message(role="assistant", text=pl["message"], created_at=ts, model=model))
            elif typ == "response_item":
                ms = self._response_item(pl, ts, model, r)
                if ms:
                    have_items = True
                    msgs += ms
        if not have_items:
            msgs += fallback
        if not native:
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.name)
            native = m.group(1) if m else path.stem
        if tokens:
            meta["tokens"] = tokens
        if archived:
            meta["archived"] = True
        conv = Conversation(platform=self.name, native_id=native, project=cwd, created_at=created, model=model,
                            source_path=str(path), meta=meta, messages=msgs)
        res.conversations.append(conv.finalize())
        if cwd:
            res.memories += read_instruction_file(Path(cwd) / "AGENTS.md", self.name, scope=f"project:{cwd}")
        return res

    def _response_item(self, pl: dict, ts, model, rec) -> list:
        t = pl.get("type")
        raw = sanitize_raw(rec)
        mid = pl.get("id")
        if t == "message":
            role = pl.get("role")
            out = []
            for part in pl.get("content") or []:
                if not isinstance(part, dict):
                    continue
                pt = part.get("type")
                if pt in ("input_text", "output_text", "text"):
                    text = part.get("text") or ""
                    if role == "user":
                        kind, text, extra = classify_user_text(text)
                        out.append(Message(role="user", text=text, id=mid, created_at=ts, kind=kind, meta=extra, raw=raw))
                    elif role in ("developer", "system"):
                        out.append(Message(role="system", text=text, id=mid, created_at=ts, kind="system_prompt", raw=raw))
                    else:
                        out.append(Message(role="assistant", text=text, id=mid, created_at=ts, model=model, raw=raw))
                elif pt in ("input_image", "input_audio", "input_file"):
                    url = part.get("image_url") or part.get("file_url") or ""
                    att = {"type": pt, "path": None if str(url).startswith("data:") else url,
                           "mime": (re.match(r"data:([^;,]+)", str(url)) or [None, None])[1],
                           "name": part.get("filename")}
                    if out and out[-1].role == "user" and out[-1].kind == "message":
                        out[-1].meta.setdefault("attachments", []).append(att)
                    else:
                        out.append(Message(role=role or "user", text="", id=mid, created_at=ts, kind="attachment",
                                           meta=att))
            # merge multiple text parts of the same message
            merged: list = []
            for m in out:
                if merged and merged[-1].kind == m.kind == "message" and merged[-1].role == m.role:
                    merged[-1].text = (merged[-1].text + "\n\n" + m.text).strip()
                    if m.meta.get("attachments"):
                        merged[-1].meta.setdefault("attachments", []).extend(m.meta["attachments"])
                else:
                    merged.append(m)
            for m in merged[1:]:
                m.raw = None
            return merged
        if t == "reasoning":
            summ = "\n".join(text_of(s) for s in pl.get("summary") or [])
            content = "\n".join(text_of(s) for s in pl.get("content") or []) if pl.get("content") else ""
            text = (summ + ("\n\n" + content if content else "")).strip()
            return [Message(role="assistant", text=text, id=mid, created_at=ts, model=model, kind="reasoning",
                            meta={"encrypted": bool(pl.get("encrypted_content")) and not text})]
        if t == "agent_message":
            return [Message(role="assistant", text=text_of(pl.get("content") or pl.get("message") or pl.get("text")),
                            id=mid, created_at=ts, model=model, meta={"inter_agent": True}, raw=raw)]
        if t in ("additional_tools", "configuration_update"):
            return []
        if t and t.endswith("_output"):
            return [Message(role="tool", text=_tool_output_text(pl.get("output")), id=mid, created_at=ts,
                            kind="tool_result", raw=raw, meta={"tool": pl.get("name"), "call_id": pl.get("call_id")})]
        if t and (t.endswith("_call") or t in ("web_search", "local_shell")):
            args = pl.get("arguments") if pl.get("arguments") is not None else (
                pl.get("input") if pl.get("input") is not None else pl.get("action"))
            text = args if isinstance(args, str) else compact_json(args)
            return [Message(role="assistant", text=text or "", id=mid, created_at=ts, model=model, kind="tool_call",
                            raw=raw, meta={"tool": pl.get("name") or t, "call_id": pl.get("call_id"),
                                           "status": pl.get("status")})]
        return []

    # ------------------------------------------------------------ thread_history sqlite
    def _load_thread_db(self, path: Path) -> ImportResult:
        res = ImportResult()
        root = path.parent
        threads: dict = {}
        parents: dict = {}
        state = _newest(root, "state_*.sqlite")
        if state:
            try:
                with sqlite_copy(state) as s:
                    if has_table(s, "threads"):
                        for r in s.execute("SELECT * FROM threads"):
                            threads[r["id"]] = dict(r)
                    if has_table(s, "thread_spawn_edges"):
                        for r in s.execute("SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"):
                            parents[r[1]] = r[0]
            except Exception as e:
                res.warnings.append(f"state db unreadable: {e}")
        with sqlite_copy(path) as db:
            if not has_table(db, "thread_items"):
                res.warnings.append("no thread_items table")
                return res
            ids = [r[0] for r in db.execute("SELECT DISTINCT thread_id FROM thread_items")]
            for tid in ids:
                th = threads.get(tid, {})
                rp = th.get("rollout_path")
                if rp and Path(rp).exists():
                    continue  # the rollout file is richer; it is imported as its own unit
                try:
                    conv = self._thread_from_db(db, tid, th, parents.get(tid))
                except Exception as e:
                    res.warnings.append(f"thread {tid}: {type(e).__name__}: {e}")
                    continue
                if conv.messages:
                    conv.source_path = f"{path}#{tid}"
                    res.conversations.append(conv)
        return res

    def _thread_from_db(self, db, tid: str, th: dict, parent: Optional[str]) -> Conversation:
        model = th.get("model")
        msgs: list = []
        rows = db.execute("SELECT * FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal, created_at_ms",
                          (tid,)).fetchall()
        for r in rows:
            try:
                it = json.loads(r["item_json"])
            except ValueError:
                continue
            itype = (r["item_type"] if "item_type" in r.keys() else "") or it.get("type")
            msgs += self._thread_item(it, itype, parse_time(r["created_at_ms"]), model,
                                      r["item_json"])
        meta: dict = {}
        for k in ("source", "thread_source", "cli_version", "git_branch", "git_sha", "git_origin_url", "reasoning_effort",
                  "agent_nickname", "agent_role", "originator", "tokens_used", "is_pinned"):
            v = th.get(k)
            if v not in (None, "", 0):
                meta[k] = v if not (k == "source" and isinstance(v, str) and len(v) > 300) else v[:300]
        if th.get("archived"):
            meta["archived"] = True
        if th.get("thread_source") == "subagent" or parent:
            meta["is_subagent"] = True
            if parent:
                meta["parent_id"] = f"{self.name}:{parent}"
        meta["recovered_from"] = "thread_history_db"
        title = th.get("name") or th.get("title") or ""
        return Conversation(platform=self.name, native_id=tid, title=title,
                            created_at=parse_time(th.get("created_at_ms") or th.get("created_at")),
                            updated_at=parse_time(th.get("updated_at_ms") or th.get("updated_at")),
                            project=th.get("cwd"), model=model, meta=meta, messages=msgs).finalize()

    def _thread_item(self, it: dict, typ: str, ts, model, raw_json: str) -> list:
        iid = it.get("id")
        raw = raw_json if len(raw_json) < 200_000 else None
        if typ == "userMessage":
            texts, atts = [], []
            for c in it.get("content") or []:
                ct = c.get("type")
                if ct == "text":
                    texts.append(c.get("text") or "")
                elif ct in ("image", "localImage", "file", "mention", "skill"):
                    url = c.get("url") or c.get("path") or c.get("name")
                    atts.append({"type": ct, "path": None if str(url).startswith("data:") else url})
            kind, text, extra = classify_user_text("\n\n".join(texts))
            if atts:
                extra.setdefault("attachments", []).extend(atts)
            return [Message(role="user", text=text, id=iid, created_at=ts, kind=kind if text or kind != "message"
                            else "attachment", meta=extra, raw=raw)]
        if typ == "agentMessage":
            return [Message(role="assistant", text=it.get("text") or "", id=iid, created_at=ts, model=model,
                            meta={"phase": it.get("phase")} if it.get("phase") else {}, raw=raw)]
        if typ == "reasoning":
            text = "\n".join(text_of(s) for s in it.get("summary") or [])
            content = "\n".join(text_of(s) for s in it.get("content") or [])
            text = (text + ("\n\n" + content if content else "")).strip()
            return [Message(role="assistant", text=text, id=iid, created_at=ts, model=model, kind="reasoning",
                            meta={"encrypted": not text})]
        if typ == "plan":
            return [Message(role="assistant", text=it.get("text") or "", id=iid, created_at=ts, kind="artifact",
                            meta={"artifact": "plan"})]
        if typ == "contextCompaction":
            return [Message(role="system", text="(context compacted)", id=iid, created_at=ts, kind="system_prompt",
                            meta={"compacted": True})]
        if typ == "sleep":
            return []
        if typ == "imageView":
            return [Message(role="assistant", text="", id=iid, created_at=ts, kind="attachment",
                            meta={"path": it.get("path"), "tool": "imageView"})]
        if typ == "imageGeneration":
            return [Message(role="assistant", text=it.get("revisedPrompt") or "", id=iid, created_at=ts,
                            kind="artifact", meta={"artifact": "image", "path": it.get("savedPath")})]
        # tool-like items → call + result
        if typ == "commandExecution":
            call, result, tool = it.get("command") or "", it.get("aggregatedOutput") or "", "shell"
            extra = {"exit_code": it.get("exitCode"), "cwd": it.get("cwd")}
        elif typ == "fileChange":
            ch = it.get("changes") or []
            call = "\n".join(f"{c.get('kind') if not isinstance(c.get('kind'), dict) else c['kind'].get('type')} "
                             f"{c.get('path')}" for c in ch)
            result, tool, extra = "\n\n".join(c.get("diff") or "" for c in ch), "apply_patch", {}
        elif typ == "mcpToolCall":
            call = compact_json(it.get("arguments"))
            res = it.get("result") or {}
            result = text_of((res or {}).get("content")) if isinstance(res, dict) else text_of(res)
            if it.get("error"):
                result = (result + "\n" + text_of(it["error"]) if result else text_of(it["error"]) or compact_json(it["error"]))
            tool, extra = f"{it.get('server')}.{it.get('tool')}", {}
        elif typ == "dynamicToolCall":
            call, result = compact_json(it.get("arguments")), text_of(it.get("contentItems"))
            tool, extra = it.get("tool"), {}
        elif typ == "webSearch":
            call, result = it.get("query") or compact_json(it.get("action")), text_of(it.get("results")) or ""
            tool, extra = "web_search", {}
        else:
            call, result, tool, extra = compact_json({k: v for k, v in it.items() if k not in ("id", "type")}, 20000), "", typ, {}
        out = [Message(role="assistant", text=call, id=iid, created_at=ts, model=model, kind="tool_call", raw=raw,
                       meta={"tool": tool, "status": it.get("status"), **{k: v for k, v in extra.items() if v is not None}})]
        if result:
            out.append(Message(role="tool", text=result, id=f"{iid}:result", parent_id=iid, created_at=ts,
                               kind="tool_result", meta={"tool": tool}))
        return out


def _newest(root: Path, pattern: str) -> Optional[Path]:
    cands = []
    for p in Path(root).glob(pattern):
        m = re.search(r"_(\d+)\.sqlite$", p.name)
        cands.append((int(m.group(1)) if m else 0, p))
    return max(cands)[1] if cands else None


@register
class CodexExporter(InstructionFileExporter):
    name = "codex"
    label = "Codex (AGENTS.md)"
    filename = "AGENTS.md"
    instructions = ("Preview written. Re-run with --apply to update the ctxport block in ~/.codex/AGENTS.md "
                    "(or <project>/AGENTS.md with --project DIR). AGENTS.md is also read by Cursor, Aider, Jules, etc.")

    @property
    def global_path(self) -> Path:  # type: ignore[override]
        return codex_home() / "AGENTS.md"
