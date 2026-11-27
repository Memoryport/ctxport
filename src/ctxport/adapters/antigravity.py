"""Google Antigravity (2.0 app / IDE / CLI). Experimental.

Data dirs: ~/.gemini/antigravity, antigravity-ide, antigravity-cli, antigravity-backup.
  conversations/<uuid>.pb   undocumented protobuf → generic wire-format decoder, readable strings only
  brain/<uuid>/*.md         task.md / implementation_plan.md / walkthrough.md → artifact messages
  knowledge/<item>/         metadata.json (title/summary) + artifacts → candidate memories + searchable doc
  annotations/*.pbtxt       text-proto metadata, used for titles when possible
  conversation_summaries.db titles / workspaces (observed on real data, schema in docs/formats.md)
Global rules read by Antigravity: ~/.gemini/AGENTS.md, ~/.gemini/config/{AGENTS,GEMINI}.md, ~/.gemini/config/rules/*.md
Export: ~/.gemini/AGENTS.md (GEMINI.md is shared with Gemini CLI). Rule files are limited to 12,000 chars.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..models import Conversation, ImportResult, Message
from ..utils import has_table, parse_time, sqlite_copy
from ._instruction import InstructionFileExporter, read_instruction_file
from .base import Importer, register
from .gemini_cli import gemini_home

DATA_DIRS = ("antigravity", "antigravity-ide", "antigravity-cli", "antigravity-backup")


# ------------------------------------------------------------------ generic protobuf wire decoder

def _varint(buf: bytes, i: int) -> tuple:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def decode_fields(buf: bytes) -> list:
    """Decode one protobuf message into [(field_no, wire_type, value)]; raises on malformed input."""
    out = []
    i = 0
    n = len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wt = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            if i + 8 > n:
                raise ValueError("truncated fixed64")
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            if i + ln > n:
                raise ValueError("truncated bytes")
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            if i + 4 > n:
                raise ValueError("truncated fixed32")
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        out.append((field, wt, v))
    return out


def _as_text(b: bytes) -> Optional[str]:
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s:
        return None
    printable = sum(1 for ch in s if ch.isprintable() or ch in "\n\r\t")
    return s if printable / len(s) > 0.95 else None


def walk_strings(buf: bytes, path: tuple = (), depth: int = 0) -> list:
    """[(field_path, text)] for every readable UTF-8 string inside nested messages."""
    out = []
    try:
        fields = decode_fields(buf)
    except ValueError:
        return out
    for field, wt, v in fields:
        if wt != 2:
            continue
        p = path + (field,)
        nested = None
        if depth < 40 and len(v) >= 2:
            try:
                nested = decode_fields(v)
            except ValueError:
                nested = None
        text = _as_text(v)
        # Prefer text when it looks like natural language; nested decoding of ASCII often "succeeds" by accident.
        if text is not None and (nested is None or _looks_natural(text)):
            out.append((p, text))
        elif nested is not None:
            out += walk_strings(v, p, depth + 1)
    return out


def _looks_natural(s: str) -> bool:
    return bool(re.search(r"[一-鿿]", s)) or (" " in s and len(s) >= 8) or "\n" in s


_NOISE = [
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),
    re.compile(r"^(file|https?|vscode|gs)://\S+$"),
    re.compile(r"^(/|~/|[A-Za-z]:\\)[^\s]*$"),
    re.compile(r"^[A-Za-z0-9+/=_-]{24,}$"),          # base64 / ids / hashes
    re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$"),  # relative paths, model ids like models/gemini-x
    re.compile(r"^\s*[\[{].*[\]}]\s*$", re.S),        # JSON tool args
    re.compile(r"^[A-Z][A-Z0-9_]{3,}$"),              # enum names
    re.compile(r"^[A-Za-z0-9_.:-]+$"),                 # single ASCII token
]


def is_noise(s: str) -> bool:
    t = s.strip()
    if len(t) < 4:
        return True
    if t.startswith(("{", "[")):
        try:
            json.loads(t)
            return True
        except ValueError:
            pass
    return any(rx.match(t) for rx in _NOISE)


def decode_conversation_pb(buf: bytes) -> list:
    """Group strings into steps: the most repeated top-level length-delimited field = steps."""
    try:
        top = decode_fields(buf)
    except ValueError:
        top = []
    counts: dict = {}
    for f, wt, v in top:
        if wt == 2:
            counts[f] = counts.get(f, 0) + 1
    steps: list = []
    if counts and max(counts.values()) > 1:
        step_field = max(counts.items(), key=lambda kv: kv[1])[0]
        for f, wt, v in top:
            if f == step_field and wt == 2:
                strs = [s for p, s in walk_strings(v) if not is_noise(s)]
                if strs:
                    steps.append(strs)
    else:
        for p, s in walk_strings(buf):
            if not is_noise(s):
                steps.append([s])
    return steps


# ------------------------------------------------------------------ importer

def data_dirs() -> list:
    g = gemini_home()
    return [g / d for d in DATA_DIRS]


@register
class AntigravityImporter(Importer):
    name = "antigravity"
    label = "Google Antigravity"
    experimental = True
    how_to_get = "Auto-detected: ~/.gemini/antigravity{,-ide,-cli,-backup}/ (conversations/*.pb, brain/, knowledge/) + rules."

    def __init__(self) -> None:
        self._titles: dict = {}
        self._titles_loaded: set = set()

    def default_roots(self) -> list:
        return [d for d in data_dirs()] + [gemini_home() / "config"]

    def discover(self, root: Path) -> list:
        root = Path(root)
        if root.is_file():
            return [root]
        units: list = []
        if root.name == "config":
            g = root.parent
            for f in (g / "AGENTS.md", root / "AGENTS.md", root / "GEMINI.md"):
                if f.is_file():
                    units.append(f)
            if (root / "rules").is_dir():
                units += sorted((root / "rules").glob("*.md"))
            return units
        if not any((root / d).exists() for d in ("conversations", "brain", "knowledge")):
            # a parent folder (the home folder or ~/.gemini, as other platforms take): look for the data folders
            found = [p for base in (root, root / ".gemini") for p in [base / d for d in DATA_DIRS] + [base / "config"]
                     if p.is_dir()]
            return [u for p in found for u in self.discover(p)]
        conv_dir = root / "conversations"
        ids = set()
        if conv_dir.is_dir():
            files = sorted(list(conv_dir.glob("*.pb")) + list(conv_dir.glob("*.db")), key=lambda p: p.stat().st_mtime)
            units += files
            ids = {p.stem for p in files}
        brain = root / "brain"
        if brain.is_dir():
            units += sorted(d for d in brain.iterdir() if d.is_dir() and d.name not in ids)
        kn = root / "knowledge"
        if kn.is_dir():
            units += sorted(d for d in kn.iterdir() if d.is_dir())
        return units

    def load(self, unit: Path) -> ImportResult:
        unit = Path(unit)
        if unit.suffix.lower() == ".md":
            res = ImportResult()
            res.memories = read_instruction_file(unit, self.name)
            return res
        if unit.is_dir() and unit.parent.name == "knowledge":
            return self._knowledge(unit)
        if unit.is_dir() and unit.parent.name == "brain":
            return self._conversation(unit.parent.parent, unit.name, None)
        return self._conversation(unit.parent.parent, unit.stem, unit)

    def _load_titles(self, data_dir: Path) -> None:
        if data_dir in self._titles_loaded:
            return
        self._titles_loaded.add(data_dir)
        db = data_dir / "conversation_summaries.db"
        if db.is_file():
            try:
                with sqlite_copy(db) as c:
                    if has_table(c, "conversation_summaries"):
                        for r in c.execute("SELECT * FROM conversation_summaries"):
                            self._titles[r["conversation_id"]] = dict(r)
            except Exception:
                pass
        ann = data_dir / "annotations"
        if ann.is_dir():
            for f in ann.glob("*.pbtxt"):
                txt = f.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'\btitle:\s*"((?:[^"\\]|\\.)*)"', txt)
                if m:
                    self._titles.setdefault(f.stem, {})["title"] = bytes(m.group(1), "utf-8").decode("unicode_escape", "ignore") \
                        if "\\" in m.group(1) and not re.search(r"[^\x00-\x7f]", m.group(1)) else m.group(1)

    def _conversation(self, data_dir: Path, cid: str, pb: Optional[Path]) -> ImportResult:
        res = ImportResult()
        self._load_titles(data_dir)
        info = self._titles.get(cid, {})
        conv = Conversation(platform=self.name, native_id=cid, title=info.get("title") or "",
                            source_path=str(pb or data_dir / "brain" / cid),
                            meta={"data_dir": data_dir.name, "experimental_decoder": True})
        if info.get("workspace_uris"):
            conv.project = str(info["workspace_uris"]).split(",")[0].strip("[]\" ")
        if info.get("parent_conversation_id"):
            conv.meta["parent_id"] = f"{self.name}:{info['parent_conversation_id']}"
            conv.meta["is_subagent"] = True
        mtime = None
        if pb is not None and pb.suffix == ".pb":
            buf = pb.read_bytes()
            mtime = pb.stat().st_mtime
            steps = decode_conversation_pb(buf)
            if not steps:
                res.warnings.append(f"{pb.name}: no readable text (compressed or encrypted?) header={buf[:16].hex()}")
            for i, strs in enumerate(steps):
                conv.messages.append(Message(role="unknown", text="\n\n".join(strs), id=f"{cid}#{i}",
                                             meta={"step": i, "decoded": "generic-protobuf"}))
        elif pb is not None:
            res.warnings.append(f"{pb.name}: .db conversation format not supported yet")
        brain = data_dir / "brain" / cid
        if brain.is_dir():
            for f in sorted(brain.glob("*.md")):
                st = f.stat()
                conv.messages.append(Message(role="assistant", text=f.read_text(encoding="utf-8", errors="replace"),
                                             id=f"{cid}/{f.name}", created_at=st.st_mtime, kind="artifact",
                                             meta={"name": f.name, "artifact": f.stem}))
                mtime = max(mtime or 0, st.st_mtime)
        conv.created_at = parse_time(info.get("created_time")) or None
        conv.updated_at = parse_time(info.get("last_modified_time")) or mtime
        if not conv.created_at:
            conv.created_at = conv.updated_at
        if conv.messages:
            res.conversations.append(conv.finalize())
        return res

    def _knowledge(self, d: Path) -> ImportResult:
        res = ImportResult()
        meta = {}
        mf = d / "metadata.json"
        if mf.is_file():
            try:
                meta = json.loads(mf.read_text(encoding="utf-8", errors="replace"))
            except ValueError:
                res.warnings.append(f"{mf}: invalid JSON")
        title = meta.get("title") or d.name
        summary = meta.get("summary") or meta.get("description") or ""
        from ..models import MemoryItem
        if summary:
            res.memories.append(MemoryItem(text=f"{title}: {summary}" if title not in summary else summary,
                                           platform=self.name, category="platform_memory", status="candidate",
                                           confidence=0.6, source_ref=str(d)))
        conv = Conversation(platform=self.name, native_id=f"knowledge/{d.name}", title=f"Knowledge: {title}",
                            source_path=str(d), meta={"knowledge_item": True})
        if summary:
            conv.messages.append(Message(role="system", text=summary, kind="artifact", meta={"name": "summary"}))
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in (".md", ".txt") and f.name != "metadata.json":
                conv.messages.append(Message(role="system", text=f.read_text(encoding="utf-8", errors="replace"),
                                             created_at=f.stat().st_mtime, kind="artifact",
                                             meta={"name": str(f.relative_to(d))}))
        conv.created_at = conv.updated_at = parse_time(meta.get("updated_at") or meta.get("created_at")) or \
            (mf.stat().st_mtime if mf.exists() else None)
        if conv.messages:
            res.conversations.append(conv.finalize())
        return res


@register
class AntigravityExporter(InstructionFileExporter):
    name = "antigravity"
    label = "Antigravity (~/.gemini/AGENTS.md)"
    filename = "AGENTS.md"
    max_chars = 11000
    instructions = ("Preview written. Re-run with --apply to update the ctxport block in ~/.gemini/AGENTS.md, a global "
                    "rules file Antigravity reads (GEMINI.md is avoided because Gemini CLI shares it). Rule files are "
                    "limited to 12,000 characters, so the block is truncated to 11,000.")

    @property
    def global_path(self) -> Path:  # type: ignore[override]
        return gemini_home() / "AGENTS.md"
