"""Coverage-guaranteed hierarchical memory distillation.

    L0  conversations (your messages + the assistant's answers, code/logs stripped, secrets masked)
    L1  one "episode card" per conversation: summary + atomic facts about you, each with evidence message numbers
        (made by a model the user chose; cached per conversation content, so re-runs only process changes)
    L2  canonical facts: duplicates merged, conflicts resolved by recency; every L1 fact id is covered by exactly
        one canonical fact (checked in code: an id the model drops is put back verbatim)
    L3  a themed document listing every canonical fact (checked in code: a fact the writer drops is appended)
    L4  per-target renderings: full document where length is free, importance-ranked selection where it isn't
        (coverage@budget is reported)

Nothing is sent to a model unless the user configured one (`ctxport llm set`) — remote endpoints additionally
need `allow_remote`. Without any model, `--manual` writes the L1 prompts as files to paste into any chat AI.
Each run writes ~/.ctxport/distilled/run-*.json with counts, tokens, timings and coverage for evaluation.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .extract import _CODE_HINT, compact_for_llm, is_sensitive, user_texts
from .llm import parse_json_object
from .models import Conversation
from .redact import redact
from .store import Store, default_home, ensure_private_dir, private_write_text

CATEGORIES = ("identity", "work", "study", "preference", "project", "tooling", "goal", "constraint", "general")
CATEGORY_TITLES = {
    "identity": "身份与背景 / Identity", "work": "工作 / Work", "study": "学习 / Study",
    "preference": "偏好与习惯 / Preferences", "project": "项目 / Projects", "tooling": "工具与环境 / Tools",
    "goal": "目标 / Goals", "constraint": "限制与要求 / Constraints", "general": "其他 / Other",
}
CHUNK_CHARS = 12000
ASSISTANT_CHARS = 500
_ID = re.compile(r"\[(c\d+(?:\s*,\s*c\d+)*)\]")

CARD_PROMPT = """You read one conversation between the USER and an AI assistant and write an "episode card".
Return ONLY a JSON object:
{"summary": "1-3 sentences: what the user wanted and what came out of it",
 "facts": [{"text": "...", "category": "identity|work|study|preference|project|tooling|goal|constraint|general",
            "evidence": [message numbers], "durable": true}]}
Rules for facts:
- Facts about the USER only: who they are, background, work/study, projects, goals, preferences, how they like
  to work with assistants, tools and environment, constraints. Use what the user said or explicitly confirmed;
  assistant text is context only.
- One atomic fact per item, specific (names, versions, dates when given), third person ("The user …" / "用户…"),
  in the language the user mainly writes in.
- "durable": false for one-off task details that will not matter in a month.
- Never include passwords, keys, ID/account numbers, exact addresses, or health, sexual or criminal information.
- If there is nothing about the user, return "facts": [].
"""

MERGE_PROMPT = """You merge a list of facts about the same user. Each fact has an id, a date range and a count.
Return ONLY a JSON object: {"facts": [{"text": "...", "covers": ["f1", "f7"]}]}
Rules:
- Merge facts that say the same thing or where one implies the other; keep every specific detail.
- If facts conflict, keep the most recent one and mention the change briefly ("…, previously …").
- EVERY input id must appear in exactly one "covers" list. Do not drop facts; do not invent facts.
- Write in the language most facts use.
"""

WRITE_PROMPT = """You write one section of a person's long-term profile for AI assistants, from canonical facts.
Each fact starts with the month it was last mentioned, e.g. "[2025-03]".
Write compact Markdown bullets (group related facts into one bullet when natural). Start every bullet with the
latest month among the facts it covers, e.g. "- [2025-03] Studies maths at … [c3,c12]". When facts conflict, the
newest is current: say so and mention the older value ("…, previously …"). End EVERY bullet with the ids it covers
in square brackets. EVERY id must appear at least once.
Do not invent anything that is not in the facts. Write in the language most facts use. Return only the bullets.
"""


# ---------------------------------------------------------------- cache db

def _db_path() -> Path:
    return default_home() / "distill.db"


def open_cache() -> sqlite3.Connection:
    ensure_private_dir(default_home())
    db = sqlite3.connect(str(_db_path()), timeout=60)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cards(conversation_id TEXT PRIMARY KEY, content_hash TEXT, model TEXT,
      card TEXT, created_at REAL);
    """)
    return db


# ---------------------------------------------------------------- L0 → prompt input

def conversation_input(conv: Conversation) -> list:
    """Numbered, compacted, masked lines; returned as a list of chunks of at most CHUNK_CHARS."""
    lines = []
    n = 0
    mine = set(user_texts(conv))
    for m in conv.messages:
        if m.kind != "message" or m.role not in ("user", "assistant") or not (m.text or "").strip():
            continue
        n += 1
        if m.role == "user":
            if m.text not in mine:
                continue
            t = compact_for_llm(m.text)
            if not t:
                continue
            lines.append(f"[{n}] USER: {redact(t)}")
        else:
            t = compact_for_llm(m.text)
            if len(t) > ASSISTANT_CHARS:
                t = t[:ASSISTANT_CHARS] + " …"
            lines.append(f"[{n}] ASSISTANT: {redact(t)}")
    chunks, cur = [], ""
    for line in lines:
        if len(line) > CHUNK_CHARS:
            line = line[:CHUNK_CHARS]
        if cur and len(cur) + len(line) + 1 > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- lean input (fewer tokens per conversation)

LEAN_CONV_CHARS = 3500       # whole conversation
LEAN_MSG_CHARS = 400         # one user message (long pasted text: translations, articles)
LEAN_ASSISTANT_CHARS = 160   # one assistant reply: enough to see what was explained
LEAN_ASSISTANT_MAX = 6       # assistant replies kept per conversation
_FENCE_LANG = re.compile(r"(?s)```([\w+#.-]*)[^\n]*\n(.*?)(?:```|$)")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\):\n(?:[ \t].*\n)+(.*)")
_SELF = re.compile(r"我(是|在|的|想|要|们|正在|喜欢|习惯|一般|平时)|\b(I'm|I am|I work|I study|my|I use|I prefer|I live)\b", re.I)


def _code_hint(lang: str, body: str) -> str:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    first = next((ln for ln in lines if not ln.startswith(("#", "//", "/*", "*"))), lines[0] if lines else "")
    return f"[代码 {lang or ''} {len(lines)} 行：{first[:80]}]".replace("  ", " ")


def lean_text(text: str) -> str:
    """A user message reduced to what a profile needs: code becomes one line saying what it is (language, size,
    first statement); runs of code/log lines likewise; long prose keeps its start and end."""
    text = _FENCE_LANG.sub(lambda m: _code_hint(m.group(1), m.group(2)), text or "")
    text = _TRACEBACK.sub(lambda m: f"[报错：{m.group(1).strip()[:120]}]", text)
    out, run = [], []
    for line in text.split("\n") + [None]:
        if line is not None and line.strip() and _CODE_HINT.search(line) and not line.startswith("[代码"):
            run.append(line)
            continue
        if len(run) >= 4:
            out.append(f"[{len(run)} 行代码/日志：{run[0].strip()[:80]}]")
        else:
            out += run
        run = []
        if line is not None:
            out.append(line)
    t = "\n".join(out).strip()
    if len(t) > LEAN_MSG_CHARS:
        t = t[:LEAN_MSG_CHARS - 150] + f" …[省略 {len(t) - LEAN_MSG_CHARS + 30} 字]… " + t[-120:]
    return t


def lean_input(conv: Conversation) -> str:
    """Numbered, masked lines like conversation_input, much shorter: what the user said (code and long pastes
    summarised), a glimpse of the first few assistant replies, and for long conversations the beginning, the end
    and every message where the user talks about themselves."""
    rows = []   # (n, role, text, keep)
    n = 0
    seen = set()
    mine = set(user_texts(conv))
    shown_assistant = 0
    for m in conv.messages:
        if m.kind != "message" or m.role not in ("user", "assistant") or not (m.text or "").strip():
            continue
        n += 1
        if m.role == "user":
            if m.text not in mine:
                continue
            t = lean_text(m.text)
            if not t or t in seen:
                continue
            seen.add(t)
            rows.append((n, "USER", redact(t), bool(_SELF.search(t))))
        elif shown_assistant < LEAN_ASSISTANT_MAX:
            t = " ".join(lean_text(m.text).split())
            if t:
                shown_assistant += 1
                rows.append((n, "ASSISTANT", redact(t[:LEAN_ASSISTANT_CHARS] + (" …" if len(t) > LEAN_ASSISTANT_CHARS else "")),
                             False))
    lines = [f"[{i}] {who}: {t}" for i, who, t, _ in rows]
    if sum(len(x) + 1 for x in lines) <= LEAN_CONV_CHARS:
        return "\n".join(lines)
    # too long: keep the start, the end and self-statements, in order, within the budget
    keep = set(range(min(6, len(rows)))) | set(range(max(0, len(rows) - 4), len(rows)))
    keep |= {k for k, r in enumerate(rows) if r[3]}
    out, used, skipped = [], 0, 0
    for k, line in enumerate(lines):
        if k in keep and used + len(line) <= LEAN_CONV_CHARS:
            if skipped:
                out.append(f"[… {skipped} 条消息省略 …]")
                skipped = 0
            out.append(line)
            used += len(line) + 1
        else:
            skipped += 1
    if skipped:
        out.append(f"[… {skipped} 条消息省略 …]")
    return "\n".join(out)


def chunks_hash(chunks: list) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.encode("utf-8"))
    return h.hexdigest()[:24]


def content_hash(conv: Conversation) -> str:
    return chunks_hash(conversation_input(conv))


def card_prompt(conv: Conversation, chunk: str, part: int, parts: int) -> str:
    head = f"Conversation: {conv.title or '(untitled)'} · platform {conv.platform}"
    if conv.created_at:
        head += f" · {time.strftime('%Y-%m-%d', time.localtime(conv.created_at))}"
    if parts > 1:
        head += f" · part {part}/{parts}"
    return f"{head}\n\n{chunk}"


def _clean_card(raw: dict) -> dict:
    facts = []
    for f in raw.get("facts") or []:
        if not isinstance(f, dict):
            continue
        text = " ".join(str(f.get("text") or "").split())
        if not (4 <= len(text) <= 500) or is_sensitive(text):
            continue
        cat = str(f.get("category") or "general").lower()
        ev = [int(x) for x in (f.get("evidence") or []) if str(x).isdigit()][:20]
        facts.append({"text": text, "category": cat if cat in CATEGORIES else "general", "evidence": ev,
                      "durable": f.get("durable") is not False})
    return {"summary": " ".join(str(raw.get("summary") or "").split())[:800], "facts": facts}


# ---------------------------------------------------------------- run state

@dataclass
class RunLog:
    started: float = field(default_factory=time.time)
    model: str = ""
    conversations: int = 0
    cards_new: int = 0
    cards_cached: int = 0
    cards_failed: int = 0
    facts_l1: int = 0
    facts_l2: int = 0
    l2_restored: int = 0
    l3_appended: int = 0
    input_chars: int = 0
    usage: dict = field(default_factory=dict)
    seconds: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)


# ---------------------------------------------------------------- L1

def build_cards(store: Store, chat: Optional[Callable], model: str, log: RunLog, platform: Optional[str] = None,
                limit: Optional[int] = None, progress: Optional[Callable[[str], None]] = None) -> dict:
    """{conversation_id: card}. Uses the cache; calls `chat(system, user) -> str` only for new/changed ones."""
    t0 = time.time()
    cache = open_cache()
    cards = {}
    rows = store.conversation_rows(platform=platform, limit=limit, order="updated_at DESC")
    for i, r in enumerate(rows, 1):
        conv = store.get_conversation(r["id"])
        if conv is None or conv.meta.get("is_subagent"):
            continue
        chunks = conversation_input(conv)
        if not any("USER:" in c for c in chunks):
            continue
        log.conversations += 1
        h = content_hash(conv)
        hit = cache.execute("SELECT card, content_hash FROM cards WHERE conversation_id=?", (conv.id,)).fetchone()
        if hit and hit["content_hash"] == h:
            cards[conv.id] = json.loads(hit["card"])
            log.cards_cached += 1
            continue
        if chat is None:
            continue
        parts = []
        try:
            for k, ch in enumerate(chunks, 1):
                prompt = card_prompt(conv, ch, k, len(chunks))
                log.input_chars += len(prompt)
                parts.append(_clean_card(parse_json_object(chat(CARD_PROMPT, prompt))))
        except Exception as e:
            log.cards_failed += 1
            if progress:
                progress(f"card failed for {conv.id}: {type(e).__name__}: {e}")
            continue
        card = {"summary": " ".join(p["summary"] for p in parts if p["summary"]),
                "facts": [f for p in parts for f in p["facts"]], "title": conv.title, "platform": conv.platform,
                "date": conv.updated_at or conv.created_at}
        cache.execute("INSERT OR REPLACE INTO cards VALUES(?,?,?,?,?)",
                      (conv.id, h, model, json.dumps(card, ensure_ascii=False), time.time()))
        cache.commit()
        cards[conv.id] = card
        log.cards_new += 1
        if progress and log.cards_new % 10 == 0:
            progress(f"cards: {log.cards_new} new, {log.cards_cached} cached ({i}/{len(rows)} conversations)")
    cache.close()
    log.seconds["l1"] = round(time.time() - t0, 1)
    return cards


# ---------------------------------------------------------------- L2

def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def _similar(a: set, b: set, threshold: float = 0.85) -> bool:
    return len(a & b) / max(1, min(len(a), len(b))) >= threshold if a and b else False


def collect_facts(store: Store, cards: dict, include_memories: bool = True, durable_only: bool = True) -> list:
    """Flat L1 fact list with ids f1…, sources and dates. Accepted memories count as facts too, so the final
    document covers everything the user already confirmed."""
    facts = []
    for cid, card in cards.items():
        for f in card.get("facts", []):
            if durable_only and not f.get("durable", True):
                continue
            facts.append({"text": f["text"], "category": f["category"], "sources": [cid],
                          "first": card.get("date"), "last": card.get("date"), "count": 1})
    if include_memories:
        for m in store.memories(status=("accepted",)):
            cat = m.category if m.category in CATEGORIES else ("preference" if m.category in ("custom_instructions",
                                                                                              "agent_instructions")
                                                               else "general")
            facts.append({"text": " ".join(m.text.split()), "category": cat, "sources": [f"memory:{m.id}"],
                          "first": m.first_seen or m.created_at, "last": m.last_seen or m.created_at,
                          "count": max(1, m.seen_count or 1)})
    for i, f in enumerate(facts, 1):
        f["id"] = f"f{i}"
    return facts


def premerge(facts: list) -> tuple:
    """Exact / near-duplicate merge without a model. Returns (groups, covers): each group is a canonical fact
    that covers the ids in covers[group_index]."""
    groups, covers, grams = [], [], []
    for f in sorted(facts, key=lambda x: -(x.get("last") or 0)):
        g = _bigrams(f["text"])
        hit = None
        for i, other in enumerate(groups):
            if other["category"] == f["category"] and _similar(g, grams[i]):
                hit = i
                break
        if hit is None:
            groups.append(dict(f))
            covers.append([f["id"]])
            grams.append(g)
        else:
            o = groups[hit]
            o["sources"] = sorted(set(o["sources"]) | set(f["sources"]))
            o["count"] += f["count"]
            o["first"] = min([x for x in (o.get("first"), f.get("first")) if x] or [None])
            covers[hit].append(f["id"])
    return groups, covers


def merge_facts(facts: list, chat: Optional[Callable], log: RunLog, batch: int = 120) -> list:
    """L2 canonical facts [{id c…, text, category, covers[f…], sources, first, last, count}] covering every fact."""
    t0 = time.time()
    by_id = {f["id"]: f for f in facts}
    groups, covers = premerge(facts)
    pre = []
    for g, cv in zip(groups, covers):
        g = dict(g)
        g["covers"] = cv
        pre.append(g)
    out = []
    if chat is None:
        out = pre
    else:
        for cat in CATEGORIES:
            items = [g for g in pre if g["category"] == cat]
            for start in range(0, len(items), batch):
                part = items[start:start + batch]
                ids = {g["id"]: g for g in part}
                listing = "\n".join(
                    f'{g["id"]} ({_d(g.get("first"))}–{_d(g.get("last"))}, ×{g["count"]}): {g["text"]}' for g in part)
                try:
                    reply = parse_json_object(chat(MERGE_PROMPT, f"Category: {cat}\n\n{listing}"))
                except Exception:
                    reply = {}
                used = set()
                for item in reply.get("facts") or []:
                    if not isinstance(item, dict):
                        continue
                    cov = [c for c in (item.get("covers") or []) if c in ids and c not in used]
                    text = " ".join(str(item.get("text") or "").split())
                    if not cov or not text or is_sensitive(text):
                        continue
                    used.update(cov)
                    members = [ids[c] for c in cov]
                    out.append({"text": text, "category": cat,
                                "covers": [x for m in members for x in m["covers"]],
                                "sources": sorted({s for m in members for s in m["sources"]}),
                                "first": min([m["first"] for m in members if m.get("first")] or [None]),
                                "last": max([m["last"] for m in members if m.get("last")] or [None]),
                                "count": sum(m["count"] for m in members)})
                for gid, g in ids.items():  # coverage guarantee: anything the model dropped comes back verbatim
                    if gid not in used:
                        out.append(g)
                        log.l2_restored += 1
    for i, c in enumerate(out, 1):
        c["id"] = f"c{i}"
    covered = {x for c in out for x in c["covers"]}
    assert covered == set(by_id), "every L1 fact must be covered"
    log.facts_l2 = len(out)
    log.seconds["l2"] = round(time.time() - t0, 1)
    return out


def _d(ts) -> str:
    return time.strftime("%Y-%m", time.localtime(ts)) if ts else "?"


def _line(c: dict) -> str:
    """One fact as a profile line. The month it was last mentioned is kept, so a reader can tell a current value from
    an older one that the profile also lists (a pilot on synthetic users: without dates, questions about a value that
    changed were answered correctly 35% of the time; with them, 87%)."""
    return f"- [{_d(c['last'])}] {c['text']}" if c.get("last") else f"- {c['text']}"


def importance(c: dict) -> float:
    """More sources and more recent = more important (used only where length is limited)."""
    age_days = (time.time() - (c.get("last") or 0)) / 86400 if c.get("last") else 3650
    confirmed = any(str(s).startswith("memory:") for s in c.get("sources", []))
    return (c.get("count", 1) ** 0.5) * (2.0 if confirmed else 1.0) / (1 + age_days / 180)


# ---------------------------------------------------------------- L3

def write_document(canon: list, chat: Optional[Callable], log: RunLog) -> tuple:
    """Returns (annotated_markdown_with_ids, sections). Every canonical fact id appears at least once."""
    t0 = time.time()
    sections = []
    for cat in CATEGORIES:
        items = sorted([c for c in canon if c["category"] == cat], key=lambda c: -importance(c))
        if not items:
            continue
        body = ""
        if chat is not None:
            listing = "\n".join(f'{c["id"]}: [{_d(c.get("last"))}] {c["text"]}' for c in items)
            try:
                body = chat(WRITE_PROMPT, f"Section: {CATEGORY_TITLES[cat]}\n\n{listing}").strip()
            except Exception:
                body = ""
            body = "\n".join(l for l in body.splitlines() if l.strip().startswith(("-", "*")))
        seen = {x.strip() for m in _ID.finditer(body) for x in m.group(1).split(",")}
        valid = {c["id"] for c in items}
        missing = [c for c in items if c["id"] not in seen]
        if chat is not None:
            log.l3_appended += len(missing)
        extra = "\n".join(f'{_line(c)} [{c["id"]}]' for c in missing)
        body = "\n".join(x for x in (body, extra) if x)
        # drop ids that don't belong to this section (a model may hallucinate ids)
        body = _ID.sub(lambda m: "[" + ",".join(x.strip() for x in m.group(1).split(",") if x.strip() in valid) + "]"
                       if any(x.strip() in valid for x in m.group(1).split(",")) else "", body)
        sections.append({"category": cat, "title": CATEGORY_TITLES[cat], "body": body, "ids": [c["id"] for c in items]})
    doc = "\n\n".join(f"## {s['title']}\n\n{s['body']}" for s in sections)
    log.seconds["l3"] = round(time.time() - t0, 1)
    return doc, sections


def strip_ids(text: str) -> str:
    return re.sub(r"[ \t]*\[(?:c\d+(?:\s*,\s*c\d+)*)?\]", "", text)


def coverage(doc: str, canon: list) -> float:
    seen = {x.strip() for m in _ID.finditer(doc) for x in m.group(1).split(",")}
    return len(seen & {c["id"] for c in canon}) / max(1, len(canon))


def render_budget(canon: list, sections: list, max_chars: int) -> tuple:
    """Importance-ranked selection that fits max_chars, each line dated. Returns (text, coverage_fraction)."""
    ranked = sorted(canon, key=lambda c: -importance(c))
    chosen, used = [], 0
    for c in ranked:
        line = _line(c) + "\n"
        if used + len(line) > max_chars:
            continue
        chosen.append(c)
        used += len(line)
    order = {s["category"]: i for i, s in enumerate(sections)}
    # oldest first inside a section, so where two lines disagree the current value comes last
    chosen.sort(key=lambda c: (order.get(c["category"], 99), c.get("last") or 0))
    text = "".join(_line(c) + "\n" for c in chosen)
    return text, len(chosen) / max(1, len(canon))


PROFILE_SIZES = {"compact": 8000, "standard": 20000}   # characters; "full" is profile.md


def render_balanced(canon: list, max_chars: int, headers: bool = True) -> tuple:
    """A selection that fits max_chars and still covers every category: each category first gets a share of the
    budget (proportional to the square root of its size, at least a small floor) filled by importance, then what
    is left goes to the most important remaining facts. Lines are dated and oldest first within a section, as in
    render_budget. Returns (markdown, coverage_fraction)."""
    by_cat = {}
    for c in sorted(canon, key=lambda c: -importance(c)):
        by_cat.setdefault(c["category"], []).append(c)
    cats = [k for k in CATEGORIES if k in by_cat] + [k for k in by_cat if k not in CATEGORIES]
    head_cost = sum(len(CATEGORY_TITLES.get(k, k)) + 6 for k in cats) if headers else 0
    budget = max(0, max_chars - head_cost)
    weights = {k: len(by_cat[k]) ** 0.5 for k in cats}
    total_w = sum(weights.values()) or 1
    floor = budget * 0.04
    chosen, used = set(), 0
    for k in cats:
        share, spent = max(floor, budget * weights[k] / total_w), 0
        for c in by_cat[k]:
            n = len(_line(c)) + 1
            if spent + n > share or used + n > budget:
                continue
            chosen.add(c["id"])
            spent += n
            used += n
    for c in sorted(canon, key=lambda c: -importance(c)):
        n = len(_line(c)) + 1
        if c["id"] not in chosen and used + n <= budget:
            chosen.add(c["id"])
            used += n
    parts = []
    for k in cats:
        items = sorted((c for c in by_cat[k] if c["id"] in chosen), key=lambda c: c.get("last") or 0)
        if not items:
            continue
        bullets = "".join(_line(c) + "\n" for c in items)
        parts.append(f"## {CATEGORY_TITLES.get(k, k)}\n\n{bullets}" if headers else bullets)
    return "\n".join(parts), len(chosen) / max(1, len(canon))


def profile_file(size: str = "full") -> Path:
    """profile.md (full), profile.standard.md, profile.compact.md or profile.short.md (ChatGPT)."""
    name = {"compact": "profile.compact.md", "standard": "profile.standard.md", "short": "profile.short.md"}.get(size)
    p = out_dir() / name if name else out_dir() / "profile.md"
    return p if p.exists() else out_dir() / "profile.md"


# ---------------------------------------------------------------- orchestration

def out_dir() -> Path:
    return default_home() / "distilled"


def run(store: Store, client=None, platform: Optional[str] = None, limit: Optional[int] = None,
        polish: bool = True, progress: Optional[Callable[[str], None]] = None) -> dict:
    """Full pipeline. `client` is an LLMClient (or anything with .chat(messages) and .model / .usage);
    None = only cached cards and deterministic L2/L3."""
    log = RunLog(model=getattr(client, "model", "") or "")
    chat = None
    if client is not None:
        def chat(system: str, user: str) -> str:
            return client.chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
    cards = build_cards(store, chat, log.model, log, platform, limit, progress)
    facts = collect_facts(store, cards)
    log.facts_l1 = len(facts)
    canon = merge_facts(facts, chat if polish else None, log)
    doc, sections = write_document(canon, chat if polish else None, log)
    full = strip_ids(doc)
    cov = coverage(doc, canon)
    short, short_cov = render_budget(canon, sections, 1400)
    tiers = {k: render_balanced(canon, n) for k, n in PROFILE_SIZES.items()}
    log.coverage = {"full": round(cov, 4), "chatgpt_1400": round(short_cov, 4),
                    **{k: round(v[1], 4) for k, v in tiers.items()}}
    log.usage = dict(getattr(client, "usage", {}) or {})
    d = ensure_private_dir(out_dir())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    private_write_text(d / "profile.md", "# My background / 我的背景\n\n" + full + "\n")
    private_write_text(d / "profile.annotated.md", doc + "\n")
    private_write_text(d / "profile.short.md", short)
    for k, (text, frac) in tiers.items():
        note = (f"\n（这是按类别和重要度挑选的版本，包含全部 {len(canon)} 条中的 {round(frac * len(canon))} 条。"
                "需要更多细节时，用 ctxport MCP 的 search_memory 查完整档案，用 search_history 查原始对话。）\n"
                if frac < 1 else "")
        private_write_text(d / f"profile.{k}.md", "# My background / 我的背景\n\n" + text + note)
    private_write_text(d / "facts.json", json.dumps({"l1": facts, "l2": canon}, ensure_ascii=False, indent=1))
    private_write_text(d / f"run-{stamp}.json", json.dumps(log.__dict__, ensure_ascii=False, indent=1, default=str))
    return {"log": log.__dict__, "profile": str(d / "profile.md"), "chars": len(full), "short_chars": len(short)}


# ---------------------------------------------------------------- manual mode (any chat AI, no API)

def manual_export(store: Store, folder: Path, platform: Optional[str] = None, limit: Optional[int] = None,
                  per_file_chars: int = 30000) -> dict:
    """Write L1 prompts that still need a card into folder/prompts/NNNN.md (several conversations per file)."""
    folder = Path(folder).expanduser()
    (folder / "prompts").mkdir(parents=True, exist_ok=True)
    (folder / "responses").mkdir(parents=True, exist_ok=True)
    cache = open_cache()
    todo = []
    for r in store.conversation_rows(platform=platform, limit=limit, order="updated_at DESC"):
        conv = store.get_conversation(r["id"])
        if conv is None or conv.meta.get("is_subagent"):
            continue
        chunks = conversation_input(conv)
        if not any("USER:" in c for c in chunks):
            continue
        hit = cache.execute("SELECT content_hash FROM cards WHERE conversation_id=?", (conv.id,)).fetchone()
        if hit and hit["content_hash"] == content_hash(conv):
            continue
        todo.append((conv, "\n\n".join(chunks)))
    cache.close()
    files, cur, n = [], [], 0
    instr = (CARD_PROMPT.replace("Return ONLY a JSON object:",
                                 "For EACH conversation below, write such an object. Return ONLY a JSON object "
                                 '{"cards": {"<conversation id>": <object>, ...}} where each object is:'))
    def flush():
        nonlocal cur, n
        if not cur:
            return
        n += 1
        p = folder / "prompts" / f"{n:04d}.md"
        p.write_text(instr + "\n\n" + "\n\n".join(cur) + "\n", encoding="utf-8")
        files.append(str(p))
        cur = []
    size = 0
    for conv, text in todo:
        block = f"=== conversation id: {conv.id} ===\n{card_prompt(conv, text, 1, 1)}"
        if cur and size + len(block) > per_file_chars:
            flush()
            size = 0
        cur.append(block)
        size += len(block)
    flush()
    readme = folder / "README.txt"
    readme.write_text("1. Paste each file in prompts/ into any chat AI you trust.\n"
                      "2. Save its JSON reply as responses/<same number>.json\n"
                      "3. Run `ctxport distill --manual <this folder>` again to import the replies and build the profile.\n",
                      encoding="utf-8")
    return {"prompts": files, "conversations": len(todo)}


def manual_import(store: Store, folder: Path) -> int:
    """Read responses/*.json written by the user and store the cards."""
    folder = Path(folder).expanduser()
    cache = open_cache()
    n = 0
    for p in sorted((folder / "responses").glob("*")):
        try:
            data = parse_json_object(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        for cid, raw in (data.get("cards") or {}).items():
            conv = store.get_conversation(cid) if isinstance(raw, dict) else None
            if conv is None:
                continue
            card = _clean_card(raw)
            card.update({"title": conv.title, "platform": conv.platform, "date": conv.updated_at or conv.created_at})
            cache.execute("INSERT OR REPLACE INTO cards VALUES(?,?,?,?,?)",
                          (cid, content_hash(conv), "manual", json.dumps(card, ensure_ascii=False), time.time()))
            n += 1
    cache.commit()
    cache.close()
    return n


def estimate(store: Store, platform: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """How much would be sent for new/changed cards (for the confirmation before a paid run)."""
    cache = open_cache()
    convs = calls = chars = 0
    for r in store.conversation_rows(platform=platform, limit=limit):
        conv = store.get_conversation(r["id"])
        if conv is None or conv.meta.get("is_subagent"):
            continue
        chunks = conversation_input(conv)
        if not any("USER:" in c for c in chunks):
            continue
        hit = cache.execute("SELECT content_hash FROM cards WHERE conversation_id=?", (conv.id,)).fetchone()
        if hit and hit["content_hash"] == content_hash(conv):
            continue
        convs += 1
        calls += len(chunks)
        chars += sum(len(c) for c in chunks) + len(CARD_PROMPT) * len(chunks)
    cache.close()
    # mixed Chinese/English text is roughly 1 token per 2-3 characters
    return {"conversations": convs, "calls": calls, "chars": chars, "tokens_approx": int(chars / 2.5)}


# ---------------------------------------------------------------- agent mode (Claude Code, Codex, any agent; no API key)

AGENT_TASK = """# Task: write episode cards for a memory profile

You are helping the owner of these conversations build a long-term profile of themselves for AI assistants.
Everything here is their own chat history (secrets already masked). Work only inside this folder.

For EACH file listed in the batch you are given (files in `conversations/`), read it and write
`cards/<same file name>.json` containing exactly:

{"conversation_id": "<the id on the first line of the file>",
 "summary": "1-3 sentences: what the user wanted and what came out of it",
 "facts": [{"text": "...", "category": "identity|work|study|preference|project|tooling|goal|constraint|general",
            "evidence": [message numbers], "durable": true}]}

Rules for facts:
- Facts about the USER only: who they are, background, work/study, projects, goals, preferences, how they like to
  work with assistants, tools and environment, constraints. Use what the user said or explicitly confirmed;
  assistant text is context only.
- One atomic fact per item, specific (names, versions, dates when given), third person ("The user …" / "用户…"),
  in the language the user mainly writes in. "durable": false for one-off task details.
- Never include passwords, keys, ID/account numbers, exact addresses, or health, sexual or criminal information.
- If a conversation says nothing about the user, still write the card with "facts": [].
- Do not edit or delete any other file. Do not summarise across conversations: ctxport merges the cards itself
  and checks that every fact ends up in the final profile.
"""


def _safe_file(cid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", cid)[:150]


def _pending(store: Store, cache, platform: Optional[str], limit: Optional[int]):
    """(conversation, input chunks) for the most recent conversations that have no up-to-date card yet."""
    for r in store.conversation_rows(platform=platform, limit=limit, order="updated_at DESC"):
        conv = store.get_conversation(r["id"])
        if conv is None or conv.meta.get("is_subagent"):
            continue
        chunks = conversation_input(conv)
        if not any("USER:" in c for c in chunks):
            continue
        hit = cache.execute("SELECT content_hash FROM cards WHERE conversation_id=?", (conv.id,)).fetchone()
        if hit and hit["content_hash"] == chunks_hash(chunks):
            continue
        yield conv, chunks


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")
CARD_OUT_TOKENS = 450      # a card is a short JSON object; measured runs replace this guess


def estimate_tokens(text: str) -> int:
    """Rough token count: about one token per CJK character and per four other characters."""
    cjk = len(_CJK.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def agent_pending(store: Store, platform: Optional[str] = None, limit: Optional[int] = None, batch: int = 20) -> dict:
    """How much an agent run would process, without writing anything."""
    cache = open_cache()
    n = chars = tokens = 0
    for conv, chunks in _pending(store, cache, platform, limit):
        n += 1
        text = card_prompt(conv, "\n".join(chunks), 1, 1)
        chars += len(text)
        tokens += estimate_tokens(text)
    cards = cache.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    cache.close()
    return {"conversations": n, "batches": -(-n // batch), "chars": chars, "cards": cards,
            "tokens_in": tokens + estimate_tokens(AGENT_TASK) * -(-n // batch), "tokens_out": n * CARD_OUT_TOKENS}


def agent_prepare(store: Store, folder: Path, platform: Optional[str] = None, limit: Optional[int] = None,
                  batch: int = 20) -> dict:
    """Write the task folder: conversations/*.md (only those without an up-to-date card), batches/NNNN.txt,
    TASK.md, and AGENTS.md / CLAUDE.md pointing at it so any coding agent picks the task up."""
    folder = Path(folder).expanduser()
    for sub in ("conversations", "cards", "batches"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    for old in (folder / "batches").glob("*.txt"):
        old.unlink()
    cache = open_cache()
    todo = []
    for conv, chunks in _pending(store, cache, platform, limit):
        name = _safe_file(conv.id) + ".md"
        private_write_text(folder / "conversations" / name,
                           f"conversation id: {conv.id}\n{card_prompt(conv, chr(10).join(chunks), 1, 1)}\n")
        stale = folder / "cards" / (_safe_file(conv.id) + ".json")
        if stale.exists():
            stale.unlink()
        todo.append(name)
    cache.close()
    batches = []
    for i in range(0, len(todo), batch):
        p = folder / "batches" / f"{i // batch + 1:04d}.txt"
        p.write_text("\n".join(todo[i:i + batch]) + "\n", encoding="utf-8")
        batches.append(str(p))
    (folder / "TASK.md").write_text(AGENT_TASK, encoding="utf-8")
    pointer = ("Read TASK.md. Process the conversation files listed in the batch file you are given "
               "(or, if none is given, every file in conversations/ that has no card yet in cards/).\n")
    (folder / "AGENTS.md").write_text(pointer, encoding="utf-8")
    (folder / "CLAUDE.md").write_text(pointer, encoding="utf-8")
    return {"folder": str(folder), "conversations": len(todo), "batches": batches}


def agent_import(store: Store, folder: Path) -> dict:
    """Read cards/*.json written by the agent into the cache. Returns {"imported", "invalid"}."""
    folder = Path(folder).expanduser()
    cache = open_cache()
    n = bad = 0
    for p in sorted((folder / "cards").glob("*.json")):
        try:
            raw = parse_json_object(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        cid = raw.get("conversation_id")
        conv = store.get_conversation(cid) if isinstance(cid, str) else None
        if conv is None:
            bad += 1
            continue
        card = _clean_card(raw)
        card.update({"title": conv.title, "platform": conv.platform, "date": conv.updated_at or conv.created_at})
        cache.execute("INSERT OR REPLACE INTO cards VALUES(?,?,?,?,?)",
                      (cid, content_hash(conv), "agent", json.dumps(card, ensure_ascii=False), time.time()))
        n += 1
    cache.commit()
    cache.close()
    return {"imported": n, "invalid": bad}


CODEX_LOCATIONS = ["/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",   # bundled with the ChatGPT app
                   "~/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                   "/Applications/Codex.app/Contents/Resources/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex"]


def agent_commands() -> dict:
    """Agent CLIs found on this computer that can run a batch unattended."""
    import os
    import shutil
    out = {}
    if shutil.which("claude"):
        out["claude-code"] = shutil.which("claude")
    codex = shutil.which("codex") or next((str(Path(p).expanduser()) for p in CODEX_LOCATIONS
                                           if os.access(Path(p).expanduser(), os.X_OK)), None)
    if codex:
        out["codex"] = codex
    return out


CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"]


def codex_models() -> list:
    """Models the user's Codex offers, from its own cache (~/.codex/models_cache.json)."""
    import os
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for m in sorted(data.get("models") or [], key=lambda m: m.get("priority") or 99):
        if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list":
            levels = [lv.get("effort") for lv in m.get("supported_reasoning_levels") or [] if isinstance(lv, dict)]
            out.append({"value": m["slug"], "label": m.get("display_name") or m["slug"],
                        "efforts": [e for e in levels if e in CODEX_EFFORTS]})
    return out


def agent_choices() -> dict:
    """Choices offered in the app. Model "" and effort "" mean the agent's own default."""
    cm = codex_models()
    return {
        "claude-code": {
            "models": [{"value": "", "label": "默认（Claude Code 当前设置）"},
                       {"value": "haiku", "label": "Haiku：最快、最省额度"},
                       {"value": "sonnet", "label": "Sonnet：速度和质量均衡（推荐）"},
                       {"value": "opus", "label": "Opus：质量最高、最耗额度"},
                       {"value": "fable", "label": "Fable"}],
            "efforts": CLAUDE_EFFORTS, "recommended": {"model": "sonnet", "effort": "low"}},
        "codex": {
            "models": [{"value": "", "label": "默认（Codex 当前设置）"}] + cm,
            "efforts": CODEX_EFFORTS, "recommended": {"model": "", "effort": "low"}},
    }


AGENT_CHOICES = agent_choices()   # static view for callers that only need the effort lists
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,79}")
_HELP_CACHE: dict = {}


def _codex_flags(exe: str) -> str:
    """`codex exec --help`, cached: flags changed between Codex versions (--full-auto was removed)."""
    import subprocess
    if exe not in _HELP_CACHE:
        try:
            _HELP_CACHE[exe] = subprocess.run([exe, "exec", "--help"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            _HELP_CACHE[exe] = ""
    return _HELP_CACHE[exe]


def codex_defaults() -> dict:
    """Top-level `model` and `model_reasoning_effort` from the user's ~/.codex/config.toml (before any [table])."""
    import os
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    try:
        text = (home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            break
        m = re.fullmatch(r'(model|model_reasoning_effort)\s*=\s*"([^"]*)"\s*(#.*)?', line)
        if m and _MODEL_RE.fullmatch(m.group(2)):
            out[m.group(1)] = m.group(2)
    if out.get("model_reasoning_effort") not in CODEX_EFFORTS:
        out.pop("model_reasoning_effort", None)
    return out


_LIMIT_RE = re.compile(r"usage limit|rate limit|hit your limit|limit reached|quota|too many requests|\b429\b|"
                       r"credit balance|out of credits", re.I)
_NOISE_RE = re.compile(r"rmcp::|mcp\.|failed to load skill|Reading additional input from stdin|^\s*$")


def agent_error(agent: str, stdout: str, stderr: str) -> str:
    """The reason an agent batch failed, in the agent's own words (not the noise its log ends with)."""
    msg = ""
    if agent == "claude-code":
        try:
            d = json.loads(stdout)
            if d.get("is_error") or d.get("subtype", "success") != "success":
                msg = str(d.get("result") or d.get("subtype") or "")
        except (ValueError, AttributeError):
            pass
    else:
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") in ("error", "turn.failed"):
                e = ev.get("error") if isinstance(ev.get("error"), dict) else ev
                msg = str(e.get("message") or msg)
    if not msg:
        lines = [ln for ln in (stderr or stdout or "").splitlines() if not _NOISE_RE.search(ln)]
        msg = "\n".join(lines[-3:])
    return msg.strip()[:400]


def is_limit_error(msg: str) -> bool:
    return bool(_LIMIT_RE.search(msg or ""))


_RESET_RE = re.compile(r"(?:try again|reset[s]?|available again)\D{0,12}?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", re.I)


def limit_reset_time(msg: str, now: Optional[float] = None) -> Optional[float]:
    """When a quota message says the limit lifts ("try again at 3:06 PM", "resets 3pm"), as a timestamp in local
    time (the next such clock time after `now`). None when the message has no time."""
    import datetime as dt
    m = _RESET_RE.search(msg or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    base = dt.datetime.fromtimestamp(now if now is not None else time.time())
    at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= base:
        at += dt.timedelta(days=1)
    return at.timestamp()


def agent_command(agent: str, exe: str, task: str, model: str = "", effort: str = "") -> list:
    if model and not _MODEL_RE.fullmatch(model):
        raise ValueError(f"模型名不合法：{model}")
    efforts = CLAUDE_EFFORTS if agent == "claude-code" else CODEX_EFFORTS
    if effort and effort not in efforts:
        raise ValueError(f"{agent} 不支持推理程度 {effort}")
    if agent == "claude-code":
        # no session file: otherwise ctxport would import its own distillation runs as conversations
        # --setting-sources "" / --strict-mcp-config: the user's CLAUDE.md, settings and MCP servers stay out, so
        # cards only hold what the conversation says and no MCP server starts for every batch
        cmd = [exe, "-p", task, "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write", "Glob",
               "--output-format", "json", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config"]
        cmd += ["--model", model] if model else []
        cmd += ["--effort", effort] if effort else []
    else:
        flags = _codex_flags(exe)
        cmd = [exe, "exec", "--skip-git-repo-check"]
        cmd += ["--full-auto"] if "--full-auto" in flags else ["-s", "workspace-write"]
        cmd += ["--ephemeral"] if "--ephemeral" in flags else []
        cmd += ["--json"] if "--json" in flags else []
        if "--ignore-user-config" in flags:
            # skip the user's MCP servers and plugins (each batch would start them); keep their default model/effort
            cmd.append("--ignore-user-config")
            defaults = codex_defaults()
            model = model or defaults.get("model", "")
            effort = effort or defaults.get("model_reasoning_effort", "")
        cmd += ["-m", model] if model else []
        cmd += ["-c", f"model_reasoning_effort={effort}"] if effort else []
        cmd.append(task)
    return cmd


def parse_usage(agent: str, stdout: str) -> Optional[dict]:
    """Token usage an agent reported for one batch: {input, cache_read, cache_write, output}, or None."""
    def num(d, *keys):
        return sum(int(d.get(k) or 0) for k in keys if isinstance(d.get(k), (int, float)))
    if agent == "claude-code":
        try:
            u = json.loads(stdout).get("usage") or {}
        except (ValueError, AttributeError):
            return None
        if not u:
            return None
        return {"input": num(u, "input_tokens"), "cache_read": num(u, "cache_read_input_tokens"),
                "cache_write": num(u, "cache_creation_input_tokens"), "output": num(u, "output_tokens")}
    total, seen = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}, False
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        u = ev.get("usage") if isinstance(ev, dict) else None
        if isinstance(u, dict) and "completed" in str(ev.get("type", "")):
            seen = True
            cached = num(u, "cached_input_tokens")
            total["input"] += max(0, num(u, "input_tokens") - cached)
            total["cache_read"] += cached
            total["output"] += num(u, "output_tokens", "reasoning_output_tokens")
    return total if seen else None


def _usage_file() -> Path:
    return work_dir("agent").parent / "agent-usage.json"


def record_usage(agent: str, model: str, effort: str, conversations: int, usage: dict, mode: str = "agent") -> None:
    try:
        rows = json.loads(_usage_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = []
    rows.append({"agent": agent, "model": model, "effort": effort, "conversations": conversations, "mode": mode,
                 "at": round(time.time()), **usage})
    ensure_private_dir(_usage_file().parent)
    private_write_text(_usage_file(), json.dumps(rows[-200:], ensure_ascii=False))


def measured_per_conversation(agent: str, model: str = "", effort: str = "", mode: str = "agent") -> Optional[dict]:
    """Average tokens per conversation from earlier batches: same agent+model+effort, else same agent+model,
    else same agent. None before anything was measured."""
    try:
        rows = json.loads(_usage_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for match in (lambda r: r.get("model") == model and r.get("effort") == effort,
                  lambda r: r.get("model") == model, lambda r: True):
        rs = [r for r in rows if r.get("agent") == agent and r.get("conversations") and r.get("mode", "agent") == mode
              and match(r)][-30:]
        if rs:
            n = sum(r["conversations"] for r in rs)
            per = {k: sum(r.get(k, 0) for r in rs) / n for k in ("input", "cache_read", "cache_write", "output")}
            per.update({"batches": len(rs), "model": rs[-1].get("model", ""), "effort": rs[-1].get("effort", "")})
            return per
    return None


def run_agent(store: Store, folder: Path, agent: str, progress: Optional[Callable[[str], None]] = None,
              timeout: int = 1800, model: str = "", effort: str = "", should_stop: Optional[Callable[[], bool]] = None,
              on_batch: Optional[Callable[[int, int], None]] = None,
              on_usage: Optional[Callable[[dict], None]] = None, parallel: int = 1) -> dict:
    """Run Claude Code (`claude -p`) or Codex (`codex exec`) once per batch, inside the task folder, using the
    user's own logged-in agent; up to `parallel` batches at a time (batches touch disjoint files). Cards are
    imported after every batch, so an interrupted run resumes. `should_stop` is checked every second; when it turns
    true the running batches are terminated and no new ones start. `on_batch(finished, total)` reports progress.
    Only the agent processes run in worker threads; the store is used from this thread alone."""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed
    folder = Path(folder).expanduser()
    exe = agent_commands().get(agent)
    if not exe:
        raise RuntimeError(f"{agent} 命令行没有安装；可以改用“打开任务文件夹”交给任意 Agent")
    agent_command(agent, exe, "", model, effort)   # validate before starting
    parallel = max(1, min(int(parallel or 1), 32))
    batches = sorted((folder / "batches").glob("*.txt"))

    halt = {"reason": ""}

    def stop_now() -> bool:
        return bool(halt["reason"]) or bool(should_stop and should_stop())

    def one(i: int, b: Path) -> dict:
        names = [n for n in b.read_text(encoding="utf-8").split() if n]
        if stop_now():
            return {"stopped": True}
        if all((folder / "cards" / (Path(n).stem + ".json")).exists() for n in names):
            return {"skipped": True}
        task = (f"Follow TASK.md exactly. Process only these files from conversations/ (listed in {b.relative_to(folder)}): "
                f"{', '.join(names)}. Write one card per file into cards/. Then stop.")
        if progress:
            progress(f"{agent}: batch {i}/{len(batches)} ({len(names)} conversations)")
        p = subprocess.Popen(agent_command(agent, exe, task, model, effort), cwd=str(folder),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        start = time.time()
        while True:
            try:
                out, err = p.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if stop_now() or time.time() - start > timeout:
                    p.terminate()
                    try:
                        out, err = p.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        out, err = p.communicate()
                    break
        timed_out = time.time() - start > timeout
        why = "timed out" if timed_out else (agent_error(agent, out or "", err or "") or f"exited {p.returncode}")
        return {"i": i, "names": names, "rc": p.returncode, "usage": parse_usage(agent, out or ""),
                "stopped": stop_now() and p.returncode != 0, "why": why}

    st = {"done": 0, "failed": 0, "finished": 0, "stopped": False}
    used = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "measured_batches": 0}

    def settle(r: dict) -> None:
        u = r.get("usage")
        if u:
            for k in ("input", "cache_read", "cache_write", "output"):
                used[k] += u[k]
            used["measured_batches"] += 1
            if r["rc"] == 0:
                record_usage(agent, model, effort, len(r["names"]), u)
            if on_usage:
                on_usage(dict(used))
        if "rc" in r:
            agent_import(store, folder)
            if r["rc"] != 0 and not halt["reason"] and is_limit_error(r["why"]):
                halt["reason"] = r["why"]       # the account is out of quota: every further batch would fail too
                if progress:
                    progress(f"{agent}: 额度用完了，暂停：{r['why']}")
        if r.get("stopped"):
            st["stopped"] = True
            return
        st["finished"] += 1
        if "rc" in r:
            st["done"] += 1
            if r["rc"] != 0:
                st["failed"] += 1
                if progress:
                    progress(f"{agent}: batch {r['i']} {r['why']}")
        if on_batch:
            on_batch(st["finished"], len(batches))

    if on_batch:
        on_batch(0, len(batches))
    if parallel == 1:
        for i, b in enumerate(batches, 1):
            settle(one(i, b))
            if st["stopped"]:
                break
    else:
        with ThreadPoolExecutor(parallel) as ex:
            for f in as_completed([ex.submit(one, i, b) for i, b in enumerate(batches, 1)]):
                settle(f.result())
    imp = agent_import(store, folder)
    return {"batches_run": st["done"], "batches_failed": st["failed"], "stopped": st["stopped"] and not halt["reason"],
            "halted": halt["reason"], "usage": used, **imp}


# ---------------------------------------------------------------- direct mode: one question per group

LEAN_SYSTEM = "You extract facts about a user from their chat history and answer with JSON only."
LEAN_PROMPT = """Below are several conversations between the USER and an AI assistant, each starting with a line
"=== C<k> ===". For EVERY conversation write an "episode card". Return ONLY this JSON:
{"cards": [{"id": "C1", "summary": "1-2 sentences: what the user wanted",
            "facts": [{"text": "...", "category": "identity|work|study|preference|project|tooling|goal|constraint|general",
                       "evidence": [message numbers of that conversation], "durable": true}]}]}
Rules:
- One card per conversation id, all ids, in order. "facts": [] when a conversation says nothing about the user.
- Facts about the USER only (who they are, background, work/study, projects, goals, preferences, how they like to work
  with assistants, tools, constraints), from what the user said or confirmed; assistant text is context only.
- One atomic fact per item, specific, third person ("The user …" / "用户…"), in the language the user mainly writes in.
- Code, logs and long pasted text were shortened to markers like [代码 python 40 行：…], [12 行代码/日志：…],
  [报错：…] or […省略 N 字…]. For coding conversations record what the user was building or fixing and what
  skills or knowledge it shows (e.g. "用户在用 Python 写一个记账小程序"), not code details. For translation or
  polishing record only what kind of text it was and its purpose, if that says something about the user.
- "durable": false for one-off task details that will not matter in a month.
- Never include passwords, keys, ID/account numbers, exact addresses, or health, sexual or criminal information.
"""
LEAN_GROUP_CHARS = 20000
LEAN_GROUP_MAX = 12


def lean_groups(items: list) -> list:
    """Pack [(conv, lean text)] into groups of at most LEAN_GROUP_MAX conversations and LEAN_GROUP_CHARS chars."""
    groups, cur, size = [], [], 0
    for conv, text in items:
        n = len(text) + 80
        if cur and (len(cur) >= LEAN_GROUP_MAX or size + n > LEAN_GROUP_CHARS):
            groups.append(cur)
            cur, size = [], 0
        cur.append((conv, text))
        size += n
    if cur:
        groups.append(cur)
    return groups


def lean_prompt(group: list) -> str:
    parts = [LEAN_PROMPT]
    for k, (conv, text) in enumerate(group, 1):
        parts.append(f"\n=== C{k} ===\n{card_prompt(conv, text, 1, 1)}")
    return "\n".join(parts)


def lean_pending(store: Store, platform: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """What a direct run would send: conversations, groups (= model calls) and estimated tokens."""
    cache = open_cache()
    items = [(conv, lean_input(conv)) for conv, _ in _pending(store, cache, platform, limit)]
    cards = cache.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    cache.close()
    groups = lean_groups(items)
    tokens = sum(estimate_tokens(t) for _, t in items) + len(groups) * estimate_tokens(LEAN_PROMPT)
    return {"conversations": len(items), "batches": len(groups), "chars": sum(len(t) for _, t in items),
            "cards": cards, "tokens_in": tokens, "tokens_out": len(items) * 300}


def direct_command(agent: str, exe: str, model: str = "", effort: str = "", out_file: str = "") -> list:
    """A one-shot, tool-less call that reads the prompt from stdin."""
    if model and not _MODEL_RE.fullmatch(model):
        raise ValueError(f"模型名不合法：{model}")
    efforts = CLAUDE_EFFORTS if agent == "claude-code" else CODEX_EFFORTS
    if effort and effort not in efforts:
        raise ValueError(f"{agent} 不支持推理程度 {effort}")
    if agent == "claude-code":
        cmd = [exe, "-p", "--output-format", "json", "--system-prompt", LEAN_SYSTEM, "--tools", "",
               "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"]
        cmd += ["--model", model] if model else []
        cmd += ["--effort", effort] if effort else []
        return cmd
    flags = _codex_flags(exe)
    cmd = [exe, "exec", "--skip-git-repo-check", "-s", "read-only"]
    cmd += ["--ephemeral"] if "--ephemeral" in flags else []
    cmd += ["--json"] if "--json" in flags else []
    if "--ignore-user-config" in flags:
        cmd.append("--ignore-user-config")
        d = codex_defaults()
        model, effort = model or d.get("model", ""), effort or d.get("model_reasoning_effort", "")
    cmd += ["-m", model] if model else []
    cmd += ["-c", f"model_reasoning_effort={effort}"] if effort else []
    cmd += ["-o", out_file] if out_file else []
    cmd.append("-")
    return cmd


def _direct_reply(agent: str, stdout: str, out_file: Optional[Path]) -> str:
    if agent == "claude-code":
        try:
            return str(json.loads(stdout).get("result") or "")
        except (ValueError, AttributeError):
            return ""
    if out_file and out_file.exists():
        return out_file.read_text(encoding="utf-8")
    last = ""
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") if isinstance(ev, dict) else None
        if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
            last = item["text"]
    return last


def run_direct(store: Store, agent: str, model: str = "", effort: str = "", limit: Optional[int] = None,
               parallel: int = 1, progress: Optional[Callable[[str], None]] = None,
               should_stop: Optional[Callable[[], bool]] = None, on_batch: Optional[Callable[[int, int], None]] = None,
               on_usage: Optional[Callable[[dict], None]] = None, timeout: int = 900,
               max_items: Optional[int] = None) -> dict:
    """Write cards by asking the user's own Claude Code / Codex directly: several shortened conversations per call,
    no tools, no agent loop (an agent re-sends its whole context on every step). Same result shape as run_agent."""
    import subprocess
    import tempfile
    from concurrent.futures import ThreadPoolExecutor, as_completed
    exe = agent_commands().get(agent)
    if not exe:
        raise RuntimeError(f"{agent} 命令行没有安装；可以改用网页版 AI 或“其他 Agent”")
    direct_command(agent, exe, model, effort)   # validate before starting
    parallel = max(1, min(int(parallel or 1), 32))
    cache = open_cache()
    items = [(conv, chunks, lean_input(conv)) for conv, chunks in _pending(store, cache, None, limit)]
    cache.close()
    items = items[:max_items] if max_items else items
    hashes = {conv.id: chunks_hash(chunks) for conv, chunks, _ in items}
    groups = lean_groups([(conv, text) for conv, _, text in items])
    halt = {"reason": ""}

    def stop_now() -> bool:
        return bool(halt["reason"]) or bool(should_stop and should_stop())

    def one(i: int, group: list) -> dict:
        if stop_now():
            return {"stopped": True}
        if progress:
            progress(f"{agent}: 第 {i}/{len(groups)} 组（{len(group)} 个对话）")
        with tempfile.TemporaryDirectory(prefix="ctxport-direct-") as tmp:
            out_file = Path(tmp) / "reply.txt"
            p = subprocess.Popen(direct_command(agent, exe, model, effort, str(out_file)), cwd=tmp,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            start = time.time()
            prompt = lean_prompt(group)
            while True:
                try:
                    out, err = p.communicate(input=prompt, timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    prompt = None   # already written
                    if stop_now() or time.time() - start > timeout:
                        p.terminate()
                        try:
                            out, err = p.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            p.kill()
                            out, err = p.communicate()
                        break
            reply = _direct_reply(agent, out or "", out_file)
        why = "timed out" if time.time() - start > timeout else (agent_error(agent, out or "", err or "")
                                                                 or f"exited {p.returncode}")
        return {"i": i, "group": group, "rc": p.returncode, "reply": reply, "usage": parse_usage(agent, out or ""),
                "stopped": stop_now() and p.returncode != 0, "why": why}

    st = {"done": 0, "failed": 0, "finished": 0, "stopped": False, "imported": 0}
    used = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "measured_batches": 0}

    def settle(r: dict) -> None:
        u = r.get("usage")
        if u:
            for k in ("input", "cache_read", "cache_write", "output"):
                used[k] += u[k]
            used["measured_batches"] += 1
            if r["rc"] == 0:
                record_usage(agent, model, effort, len(r["group"]), u, mode="direct")
            if on_usage:
                on_usage(dict(used))
        if r.get("stopped"):
            st["stopped"] = True
            return
        st["finished"] += 1
        st["done"] += 1
        got = 0
        if r["rc"] == 0:
            raw = parse_json_object(r["reply"] or "")
            by_id = {str(c.get("id")).strip().upper(): c for c in raw.get("cards") or [] if isinstance(c, dict)}
            db = open_cache()
            for k, (conv, _) in enumerate(r["group"], 1):
                c = by_id.get(f"C{k}")
                if c is None:
                    continue   # left for the next run
                card = _clean_card(c)
                card.update({"title": conv.title, "platform": conv.platform, "date": conv.updated_at or conv.created_at})
                db.execute("INSERT OR REPLACE INTO cards VALUES(?,?,?,?,?)",
                           (conv.id, hashes[conv.id], f"{agent}:{model or 'default'}", json.dumps(card, ensure_ascii=False),
                            time.time()))
                got += 1
            db.commit()
            db.close()
            st["imported"] += got
        if r["rc"] != 0 or not got:
            st["failed"] += 1
            if r["rc"] != 0 and not halt["reason"] and is_limit_error(r["why"]):
                halt["reason"] = r["why"]
                if progress:
                    progress(f"{agent}: 额度用完了，暂停：{r['why']}")
            elif progress:
                progress(f"{agent}: 第 {r['i']} 组失败：{r['why'] if r['rc'] != 0 else '回复里没有可用的卡片'}")
        if on_batch:
            on_batch(st["finished"], len(groups))

    if on_batch:
        on_batch(0, len(groups))
    if parallel == 1:
        for i, g in enumerate(groups, 1):
            settle(one(i, g))
            if st["stopped"]:
                break
    else:
        with ThreadPoolExecutor(parallel) as ex:
            for f in as_completed([ex.submit(one, i, g) for i, g in enumerate(groups, 1)]):
                settle(f.result())
    return {"batches_run": st["done"], "batches_failed": st["failed"], "stopped": st["stopped"] and not halt["reason"],
            "halted": halt["reason"], "usage": used, "imported": st["imported"], "invalid": 0}


# ---------------------------------------------------------------- web chat mode helpers (used by the app)

def web_batches(store: Store, folder: Path, per_file_chars: int = 30000, limit: Optional[int] = None) -> list:
    """(Re)write manual prompt files and return [{"name", "chars"}] for the copy/paste flow."""
    for old in (Path(folder).expanduser() / "prompts").glob("*.md"):
        old.unlink()
    r = manual_export(store, folder, limit=limit, per_file_chars=per_file_chars)
    return [{"name": Path(p).name, "chars": Path(p).stat().st_size} for p in r["prompts"]]


def web_answer(store: Store, folder: Path, name: str, reply: str) -> int:
    """Save the chat AI's reply for prompt `name` and import its cards. Returns the number of cards imported."""
    folder = Path(folder).expanduser()
    if not re.fullmatch(r"\d{4}\.md", name):
        raise ValueError("bad batch name")
    (folder / "responses").mkdir(parents=True, exist_ok=True)
    (folder / "responses" / name.replace(".md", ".json")).write_text(reply, encoding="utf-8")
    return manual_import(store, folder)


def work_dir(kind: str) -> Path:
    return default_home() / "distill-work" / kind
