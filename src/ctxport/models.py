"""Unified intermediate data model shared by every importer and exporter."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

ROLES = ("user", "assistant", "system", "tool", "unknown")
KINDS = ("message", "tool_call", "tool_result", "reasoning", "artifact", "attachment", "system_prompt")
STATUSES = ("accepted", "candidate", "rejected")


def _norm_role(role: Any) -> str:
    r = str(role or "").strip().lower()
    if r in ROLES:
        return r
    if r in ("human", "me"):
        return "user"
    if r in ("model", "gemini", "ai", "bot", "agent", "assistant_message"):
        return "assistant"
    if r in ("developer",):
        return "system"
    if r in ("function", "tool_result", "toolresult"):
        return "tool"
    return "unknown"


@dataclass
class Message:
    role: str = "unknown"
    text: str = ""
    id: Optional[str] = None
    parent_id: Optional[str] = None
    created_at: Optional[float] = None
    model: Optional[str] = None
    kind: str = "message"
    meta: dict = field(default_factory=dict)
    raw: Optional[str] = None

    def __post_init__(self) -> None:
        self.role = _norm_role(self.role)
        if self.kind not in KINDS:
            self.kind = "message"
        if self.text is None:
            self.text = ""

    def to_dict(self, include_raw: bool = True) -> dict:
        d = asdict(self)
        if not include_raw:
            d.pop("raw", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        keys = {"role", "text", "id", "parent_id", "created_at", "model", "kind", "meta", "raw"}
        return cls(**{k: v for k, v in d.items() if k in keys})


@dataclass
class Conversation:
    platform: str
    native_id: str
    title: str = ""
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    project: Optional[str] = None
    model: Optional[str] = None
    source_path: Optional[str] = None
    meta: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    branches: list = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.platform}:{self.native_id}"

    def finalize(self) -> "Conversation":
        """Fill derived fields (times, model, title) from messages when missing."""
        allm = list(self.messages) + [m for b in self.branches for m in b]
        times = [m.created_at for m in allm if m.created_at]
        if times:
            if not self.created_at:
                self.created_at = min(times)
            if not self.updated_at:
                self.updated_at = max(times)
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.model:
            counts: dict = {}
            for m in self.messages:
                if m.model and m.role == "assistant":
                    counts[m.model] = counts.get(m.model, 0) + 1
            if counts:
                self.model = max(counts.items(), key=lambda kv: kv[1])[0]
        if not (self.title or "").strip():
            first = next((m.text for m in self.messages if m.role == "user" and m.kind == "message" and m.text.strip()), "")
            self.title = first_line(first, 80) or "(untitled)"
        self.title = first_line(self.title, 200)
        self.branches = [b for b in self.branches if b]
        return self

    def to_dict(self, include_raw: bool = True) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "native_id": self.native_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "project": self.project,
            "model": self.model,
            "source_path": self.source_path,
            "meta": self.meta,
            "messages": [m.to_dict(include_raw) for m in self.messages],
            "branches": [[m.to_dict(include_raw) for m in b] for b in self.branches],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Conversation":
        c = cls(
            platform=d["platform"],
            native_id=d["native_id"],
            title=d.get("title") or "",
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            project=d.get("project"),
            model=d.get("model"),
            source_path=d.get("source_path"),
            meta=d.get("meta") or {},
        )
        c.messages = [Message.from_dict(m) for m in d.get("messages") or []]
        c.branches = [[Message.from_dict(m) for m in b] for b in d.get("branches") or []]
        return c


def normalize_memory_text(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip()).lower()
    return t.rstrip("。.!！;；")


_SUBJECT = re.compile(r"^(the user|user|i am|i'm|i|my|用户|我是|我的|我们|我)\s*", re.I)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def similarity_key(text: str) -> str:
    """Loose form for spotting near-duplicates: width-folded, lower case, no subject, no spaces or punctuation.
    "I use VS Code." / "用户使用 VSCode" / "我用vscode" all come out close."""
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "").lower().strip()
    t = _SUBJECT.sub("", t)
    return _NON_WORD.sub("", t)


def _grams(key: str) -> set:
    return {key[i:i + 2] for i in range(len(key) - 1)} if len(key) > 1 else {key}


def similarity(a: str, b: str) -> float:
    """Dice coefficient on character bigrams of the similarity keys (0..1). Works without word splitting."""
    ka, kb = similarity_key(a), similarity_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    ga, gb = _grams(ka), _grams(kb)
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def memory_id(text: str, scope: str = "global") -> str:
    return hashlib.sha1(f"{scope}|{normalize_memory_text(text)}".encode("utf-8")).hexdigest()[:16]


@dataclass
class MemoryItem:
    text: str
    platform: str = "manual"
    category: str = "general"
    source_ref: Optional[str] = None
    confidence: float = 1.0
    status: str = "candidate"
    scope: str = "global"
    created_at: float = field(default_factory=time.time)
    id: str = ""
    seen_count: int = 1                 # how many conversations mention it
    first_seen: Optional[float] = None  # earliest / latest time a conversation mentioned it
    last_seen: Optional[float] = None
    question: Optional[str] = None      # questionnaire id it answers, when it came from a question

    def __post_init__(self) -> None:
        self.text = (self.text or "").strip()
        if self.status not in STATUSES:
            self.status = "candidate"
        if not self.id:
            self.id = memory_id(self.text, self.scope)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImportResult:
    conversations: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def extend(self, other: "ImportResult") -> None:
        self.conversations += other.conversations
        self.memories += other.memories
        self.warnings += other.warnings


@dataclass
class OutputFile:
    rel_path: str
    content: str
    apply_path: Optional[str] = None
    managed: bool = False


def first_line(text: str, limit: int = 80) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
