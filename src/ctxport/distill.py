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

from .extract import compact_for_llm, is_sensitive, user_texts
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
Write compact Markdown bullets (group related facts into one bullet when natural). End EVERY bullet with the ids it
covers in square brackets, e.g. "- Studies maths at … [c3,c12]". EVERY id must appear at least once.
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


def content_hash(conv: Conversation) -> str:
    h = hashlib.sha256()
    for c in conversation_input(conv):
        h.update(c.encode("utf-8"))
    return h.hexdigest()[:24]


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
            listing = "\n".join(f'{c["id"]}: {c["text"]}' for c in items)
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
        extra = "\n".join(f'- {c["text"]} [{c["id"]}]' for c in missing)
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
    """Importance-ranked selection that fits max_chars. Returns (text, coverage_fraction)."""
    ranked = sorted(canon, key=lambda c: -importance(c))
    chosen, used = [], 0
    for c in ranked:
        line = f"- {c['text']}\n"
        if used + len(line) > max_chars:
            continue
        chosen.append(c)
        used += len(line)
    order = {s["category"]: i for i, s in enumerate(sections)}
    chosen.sort(key=lambda c: (order.get(c["category"], 99), -importance(c)))
    text = "".join(f"- {c['text']}\n" for c in chosen)
    return text, len(chosen) / max(1, len(canon))


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
    log.coverage = {"full": round(cov, 4), "chatgpt_1400": round(short_cov, 4)}
    log.usage = dict(getattr(client, "usage", {}) or {})
    d = ensure_private_dir(out_dir())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    private_write_text(d / "profile.md", "# My background / 我的背景\n\n" + full + "\n")
    private_write_text(d / "profile.annotated.md", doc + "\n")
    private_write_text(d / "profile.short.md", short)
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


def agent_commands() -> dict:
    """Agent CLIs found on this computer that can run a batch unattended."""
    import shutil
    out = {}
    if shutil.which("claude"):
        out["claude-code"] = shutil.which("claude")
    if shutil.which("codex"):
        out["codex"] = shutil.which("codex")
    return out


def run_agent(store: Store, folder: Path, agent: str, progress: Optional[Callable[[str], None]] = None,
              timeout: int = 1800) -> dict:
    """Run Claude Code (`claude -p`) or Codex (`codex exec`) once per batch, inside the task folder, using the
    user's own logged-in agent. Cards are imported after every batch, so an interrupted run resumes."""
    import subprocess
    folder = Path(folder).expanduser()
    exe = agent_commands().get(agent)
    if not exe:
        raise RuntimeError(f"{agent} 命令行没有安装；可以改用“打开任务文件夹”交给任意 Agent")
    batches = sorted((folder / "batches").glob("*.txt"))
    done = failed = 0
    for i, b in enumerate(batches, 1):
        names = [n for n in b.read_text(encoding="utf-8").split() if n]
        if all((folder / "cards" / (Path(n).stem + ".json")).exists() for n in names):
            continue
        task = (f"Follow TASK.md exactly. Process only these files from conversations/ (listed in {b.relative_to(folder)}): "
                f"{', '.join(names)}. Write one card per file into cards/. Then stop.")
        if agent == "claude-code":
            cmd = [exe, "-p", task, "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write", "Glob"]
        else:
            cmd = [exe, "exec", "--full-auto", "--skip-git-repo-check", task]
        if progress:
            progress(f"{agent}: batch {i}/{len(batches)} ({len(names)} conversations)")
        try:
            r = subprocess.run(cmd, cwd=str(folder), capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                failed += 1
                if progress:
                    progress(f"{agent}: batch {i} exited {r.returncode}: {(r.stderr or r.stdout)[-300:]}")
        except subprocess.TimeoutExpired:
            failed += 1
            if progress:
                progress(f"{agent}: batch {i} timed out")
        done += 1
        agent_import(store, folder)
    imp = agent_import(store, folder)
    return {"batches_run": done, "batches_failed": failed, **imp}


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
