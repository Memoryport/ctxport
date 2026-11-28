"""Migration-loss experiment pipeline: build each method's context Z for a persona, ask a reader, score.

Works on memsynth personas (<dir>/home/ctxport.db + questions.jsonl). All model calls go through llm.call, so they
are isolated and cached; everything else is deterministic.

Methods (B = character budget):
  none            empty context (floor)
  recent@B        newest user messages that fit
  bm25@B          per question: BM25 over dated user sentences, filled to B (needs query-time retrieval)
  summary@B       one model call over all user text (chunked hierarchically above SUMMARY_CHUNK chars)
  rolling@B       summary updated slice by slice (old summary + new slice -> new summary), as an assistant's
                  memory would be maintained over time
  chmd-det@B      L1 cards by the extractor, deterministic L2, budget render without dates
  chmd-detT@B     same, every line dated [YYYY-MM]
  chmd-llm@B      L2 merged by the extractor model, dated budget render
  full            all user text (ceiling; infeasible at real scale)
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "memsynth"))

import llm  # noqa: E402
import score as MS  # noqa: E402  (memsynth scorer)
from ctxport import distill as D  # noqa: E402
from ctxport.extract import user_texts  # noqa: E402
from ctxport.store import Store  # noqa: E402

SUMMARY_CHUNK = 120_000
PARALLEL = 6


# ------------------------------------------------------------------ persona data

class Persona:
    def __init__(self, d: Path):
        self.dir = Path(d)
        self.id = self.dir.name
        self.questions = [json.loads(x) for x in open(self.dir / "questions.jsonl", encoding="utf-8")]
        st = Store(self.dir / "home" / "ctxport.db")
        rows = st.conversation_rows(order="created_at ASC")
        self.convs = [c for c in (st.get_conversation(r["id"]) for r in rows) if c is not None]
        st.close()
        self._msgs = None

    def user_messages(self) -> list:
        """[(ts, platform, lean text)] oldest first; code and long pastes shortened as for the L1 input."""
        if self._msgs is None:
            out = []
            for c in self.convs:
                mine = set(user_texts(c))
                for m in c.messages:
                    if m.role == "user" and m.kind == "message" and m.text in mine:
                        t = " ".join(D.lean_text(m.text).split())
                        if t:
                            out.append((m.created_at or c.created_at or 0, c.platform, t))
            out.sort(key=lambda x: x[0])
            self._msgs = out
        return self._msgs


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def dated_text(msgs: list) -> str:
    return "\n".join(f"[{_day(ts)}] {t}" for ts, _, t in msgs)


# ------------------------------------------------------------------ L1 cards (grouped, cached per extractor)

def _tag(backend: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "_", backend)


def build_cards(p: Persona, extractor: str, convs=None, log=None) -> dict:
    convs = p.convs if convs is None else convs
    path = p.dir / f"cards-{_tag(extractor)}.json"
    cards = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = [(c, D.lean_input(c)) for c in convs if c.id not in cards]
    todo = [(c, t) for c, t in todo if "USER:" in t]
    groups = D.lean_groups(todo)
    system = llm.STRICT + " " + D.LEAN_SYSTEM

    def one(group):
        try:
            r = llm.call(extractor, D.lean_prompt(group), system=system)
            raw = llm.parse_json(r["text"])
        except Exception as e:  # noqa: BLE001
            return group, {}, {"error": str(e)[:200]}
        by = {str(c.get("id")).strip().upper(): c for c in raw.get("cards") or [] if isinstance(c, dict)}
        return group, by, r

    for attempt in range(2):
        with ThreadPoolExecutor(PARALLEL) as ex:
            results = list(ex.map(one, groups))
        missing = []
        for group, by, r in results:
            if log is not None and isinstance(r, dict) and "usage" in r and not r.get("cached"):
                log["input"] += r["usage"]["input"]
                log["output"] += r["usage"]["output"]
                log["calls"] += 1
            for k, (conv, text) in enumerate(group, 1):
                c = by.get(f"C{k}")
                if c is None:
                    missing.append((conv, text))
                    continue
                card = D._clean_card(c)
                card.update({"title": conv.title, "platform": conv.platform,
                             "date": conv.updated_at or conv.created_at})
                cards[conv.id] = card
        path.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
        if not missing:
            break
        groups = [[x] for x in missing]   # retry one conversation per call
    return cards


def canonical(cards: dict, merger: str = None, log=None) -> list:
    facts = D.collect_facts(None, cards, include_memories=False)
    chat = None
    if merger:
        def chat(system, user):
            r = llm.call(merger, user, system=llm.STRICT + "\n\n" + system)
            if log is not None and not r.get("cached"):
                log["input"] += r["usage"]["input"]
                log["output"] += r["usage"]["output"]
                log["calls"] += 1
            return r["text"]
    return D.merge_facts(facts, chat, D.RunLog())


def render(canon: list, budget, dated: bool = True) -> tuple:
    """Importance-greedy selection (as distill.render_budget) with optional date tags. Returns (text, ids)."""
    def line(c):
        return (f"- [{D._d(c['last'])}] {c['text']}" if dated and c.get("last") else f"- {c['text']}") + "\n"
    ranked = sorted(canon, key=lambda c: -D.importance(c))
    chosen, used = [], 0
    for c in ranked:
        n = len(line(c))
        if budget is not None and used + n > budget:
            continue
        chosen.append(c)
        used += n
    order = {k: i for i, k in enumerate(D.CATEGORIES)}
    chosen.sort(key=lambda c: (order.get(c["category"], 99), c.get("last") or 0))
    return "".join(line(c) for c in chosen), [c["id"] for c in chosen]


# ------------------------------------------------------------------ baselines

def recent(p: Persona, budget: int) -> str:
    out, used = [], 0
    for ts, _, t in reversed(p.user_messages()):
        s = f"[{_day(ts)}] {t}\n"
        if used + len(s) > budget:
            if used:
                break
            continue
        out.append(s)
        used += len(s)
    return "".join(reversed(out))


def _tokens(s: str) -> list:
    s = s.lower()
    words = re.findall(r"[a-z0-9+#.]+", s)
    han = re.findall(r"[一-鿿]+", s)
    return words + [h[i:i + 2] for h in han for i in range(max(1, len(h) - 1))]


class BM25:
    def __init__(self, docs: list, k1: float = 1.2, b: float = 0.75):
        self.docs = docs
        self.toks = [Counter(_tokens(d)) for d in docs]
        self.len = [sum(t.values()) for t in self.toks]
        self.avg = sum(self.len) / max(1, len(self.len))
        df = Counter(w for t in self.toks for w in t)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self.k1, self.b = k1, b

    def rank(self, query: str) -> list:
        q = set(_tokens(query))
        scores = []
        for i, t in enumerate(self.toks):
            s = 0.0
            for w in q:
                f = t.get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                scores.append((s, i))
        return [i for _, i in sorted(scores, reverse=True)]


def bm25_contexts(p: Persona, budget: int, questions: list) -> dict:
    sents = []
    for ts, _, t in p.user_messages():
        for s in re.split(r"(?<=[。！？!?.])\s*", t):
            if len(s.strip()) > 3:
                sents.append(f"[{_day(ts)}] {s.strip()}")
    idx = BM25(sents)
    out = {}
    for q in questions:
        text, used = [], 0
        for i in idx.rank(q["question"]["en"] + " " + q["question"]["zh"]):
            s = sents[i] + "\n"
            if used + len(s) > budget:
                continue
            text.append(s)
            used += len(s)
        out[q["id"]] = "".join(text)
    return out


SUMMARY_PROMPT = """Below is everything a user wrote to AI assistants over a long period, one message per line with
its date. Write a profile of this user that a NEW assistant will receive as its only memory of them. Hard limit:
{budget} characters (count them; stay under). Keep concrete, specific facts (names, places, tools, projects,
preferences, plans). When something changed over time, give the current value and the previous one with dates
("now X (since 2025-03); before: Y"). Do not invent anything. Output only the profile text."""

CHUNK_PROMPT = """Below is one part of everything a user wrote to AI assistants, one message per line with its date.
Write dense notes of every fact about the user in it (identity, work, study, places, people, tools, projects,
preferences, plans), with the date each was stated; when a fact changes, keep both values with dates. At most
{budget} characters. Do not invent anything. Output only the notes."""

UPDATE_PROMPT = """You maintain a short memory profile of a user for an AI assistant. Below is the CURRENT PROFILE
and then NEW MESSAGES the user wrote since (one per line, dated). Write the updated profile: add new facts, update
values that changed (keep the previous value with dates), keep what is still true. Hard limit: {budget} characters.
Do not invent anything. Output only the profile text."""


def _fit(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[:budget]


def summary(p: Persona, budget: int, backend: str, log=None, msgs=None) -> str:
    text = dated_text(p.user_messages() if msgs is None else msgs)

    def ask(prompt, body):
        r = llm.call(backend, body, system=llm.STRICT + "\n\n" + prompt)
        if log is not None and not r.get("cached"):
            log["input"] += r["usage"]["input"]
            log["output"] += r["usage"]["output"]
            log["calls"] += 1
        return r["text"].strip()

    if len(text) > SUMMARY_CHUNK:
        lines, parts, cur = text.split("\n"), [], []
        size = 0
        for ln in lines:
            if size + len(ln) > SUMMARY_CHUNK and cur:
                parts.append("\n".join(cur))
                cur, size = [], 0
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            parts.append("\n".join(cur))
        per = max(2000, min(8000, 4 * budget // max(1, len(parts)) * 2))
        with ThreadPoolExecutor(PARALLEL) as ex:
            notes = list(ex.map(lambda b: ask(CHUNK_PROMPT.format(budget=per), b), parts))
        text = "\n\n".join(f"(notes, part {i + 1})\n{n}" for i, n in enumerate(notes))
    return _fit(ask(SUMMARY_PROMPT.format(budget=budget), text), budget)


def rolling(p: Persona, budget: int, backend: str, slices: int = 4, log=None) -> list:
    """Profiles after each slice; the last is what the assistant holds at the end."""
    msgs = p.user_messages()
    cut = [msgs[len(msgs) * i // slices:len(msgs) * (i + 1) // slices] for i in range(slices)]
    prof, out = "", []
    for k, part in enumerate(cut):
        if k == 0:
            prof = summary(p, budget, backend, log, msgs=part)
        else:
            body = f"CURRENT PROFILE:\n{prof}\n\nNEW MESSAGES:\n{dated_text(part)}"
            if len(body) > SUMMARY_CHUNK * 1.5:
                body = body[: int(SUMMARY_CHUNK * 1.5)]
            r = llm.call(backend, body, system=llm.STRICT + "\n\n" + UPDATE_PROMPT.format(budget=budget))
            if log is not None and not r.get("cached"):
                log["input"] += r["usage"]["input"]
                log["output"] += r["usage"]["output"]
                log["calls"] += 1
            prof = _fit(r["text"].strip(), budget)
        out.append(prof)
    return out


# ------------------------------------------------------------------ reader

READER_PROMPT = """Below are notes about a person, then questions about that person. Answer every question from the
notes only, as briefly as possible (a name, value or short list). If the notes do not say, answer exactly UNKNOWN.
If the notes give an older and a newer value, "current"/"now" means the newer one. Return ONLY a JSON object mapping
each question id to its answer string."""


def read(backend: str, context: str, questions: list, lang: str = "en", batch: int = 60) -> dict:
    out = {}
    for s in range(0, len(questions), batch):
        part = questions[s:s + batch]
        qs = "\n".join(f'{q["id"]}: {q["question"][lang]}' for q in part)
        body = f"NOTES:\n<<<\n{context or '(no notes)'}\n>>>\n\nQUESTIONS:\n{qs}"
        for attempt in range(2):
            try:
                r = llm.call(backend, body + ("" if attempt == 0 else "\n\n(Return valid JSON only.)"),
                             system=llm.STRICT + "\n\n" + READER_PROMPT)
                d = llm.parse_json(r["text"])
                out.update({str(k): str(v) for k, v in d.items()})
                break
            except Exception:  # noqa: BLE001
                continue
    return out


def read_each(backend: str, contexts: dict, questions: list, lang: str = "en", batch: int = 8) -> dict:
    """Per-question contexts (retrieval): several (context, question) blocks per call; a batch whose reply does not
    parse is split in half and retried."""
    out = {}
    pending = [questions[s:s + batch] for s in range(0, len(questions), batch)]
    while pending:
        part = pending.pop(0)
        blocks = "\n\n".join(f'### {q["id"]}\nNOTES:\n<<<\n{contexts[q["id"]] or "(no notes)"}\n>>>\nQUESTION: '
                             f'{q["question"][lang]}' for q in part)
        for attempt in range(2):
            try:
                r = llm.call(backend, blocks, system=llm.STRICT + "\n\n" + READER_PROMPT +
                             " Each question has its own notes; use only those.")
                d = llm.parse_json(r["text"])
                out.update({str(k): str(v) for k, v in d.items()})
                break
            except Exception:  # noqa: BLE001
                if attempt == 1 and len(part) > 1:
                    pending += [part[:len(part) // 2], part[len(part) // 2:]]
    return out


WRITE_PROMPT = """Below is a list of canonical facts about a user, each with the month it was last mentioned and how
many times it came up (xN). Write the profile a NEW assistant will receive as its only memory of this user. Hard
limit: {budget} characters (stay under). Prioritise who the user is, their current situation, people, places,
tools, projects and stated preferences over one-off task details. When facts conflict, the latest is current: say
so and keep the previous value with dates ("now X (2026-01); before: Y"). Do not invent anything. Output only the
profile text."""


def chmd_write(canon: list, budget: int, backend: str, log=None) -> str:
    """CHMD L3/L4 by the model: write a budgeted profile from the L2 facts (reads |L2| chars, not the history)."""
    ranked = sorted(canon, key=lambda c: -D.importance(c))
    listing, used = [], 0
    for c in ranked:
        ln = f"- [{D._d(c.get('last'))}] (x{c.get('count', 1)}) {c['text']}"
        if used + len(ln) > SUMMARY_CHUNK:
            break
        listing.append(ln)
        used += len(ln) + 1
    r = llm.call(backend, "\n".join(listing), system=llm.STRICT + "\n\n" + WRITE_PROMPT.format(budget=budget))
    if log is not None and not r.get("cached"):
        log["input"] += r["usage"]["input"]
        log["output"] += r["usage"]["output"]
        log["calls"] += 1
    return _fit(r["text"].strip(), budget)


# ------------------------------------------------------------------ scoring helpers

def contained(q: dict, z: str):
    """Is the gold answer in the context (normalised alias containment)? None for count/abstain questions."""
    kind = q.get("answer_kind", "value")
    if kind in ("unknown", "count") or not q.get("answer"):
        return None
    t = MS.norm(z)
    if kind == "list":
        return all(MS.any_hit(item, t) for item in q["answer"])
    return MS.any_hit(q["answer"], t)


def judge(q: dict, answer) -> dict:
    if answer is None:
        return {"correct": False, "trap": False, "missing": True}
    r = MS.judge(q, answer)
    t = MS.norm(answer)
    r["abstained"] = bool(MS.ABSTAIN.search(t))
    return r
