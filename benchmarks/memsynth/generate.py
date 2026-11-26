"""Synthetic personas with multi-platform chat histories, gold facts and a question bank.

    python benchmarks/memsynth/generate.py --personas 8 --conversations 200 --out /tmp/memsynth
    python benchmarks/memsynth/generate.py --personas 8 --db      # also write <out>/<persona>/home/ctxport.db

Each persona gets a fictional profile whose facts are planted in user messages across chat apps (ChatGPT, Claude.ai,
Gemini) and coding agents (Claude Code, Codex), among filler conversations. Facts can be mentioned once (long tail)
or many times, can change over time (moved city, new job), and are surrounded by distractors: other people's facts,
hypotheticals, wishes and negations that use values from the same pool.

Outputs per persona:
    conversations.jsonl   ctxport Conversation dicts (loadable with Store.upsert_conversation)
    facts.json            gold facts with validity windows and the exact message ids that state them, plus distractors
    questions.jsonl       questions with gold answers, aliases, trap values (distractors / outdated values), evidence
and manifest.json with per-type question counts and the 95% CI half-width those counts allow.

Everything is deterministic from --seed. No network, no model calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vocab as V  # noqa: E402

START = 1735689600  # 2025-01-01 UTC
DAY = 86400
CHAT = ["chatgpt", "claude-ai", "gemini-web"]
CODING = ["claude-code", "codex"]
MODEL = {"chatgpt": "gpt-4o", "claude-ai": "claude-sonnet-4", "gemini-web": "gemini-2.5-pro",
         "claude-code": "claude-sonnet-4", "codex": "gpt-5-codex"}
ABSENT_POOL = ["hometown", "university", "major", "pet", "sibling", "allergy", "instrument", "favorite_food", "os"]
QTYPES = ["single_fact", "long_tail", "update_current", "update_previous", "preference", "aggregate_list",
          "aggregate_count", "multi_hop", "abstain"]
ZH_NUM = "零一二三四五六七八九十"


def forms(item) -> list:
    """Every accepted surface form of a pool value."""
    out = []
    for x in item:
        if isinstance(x, str) and x and x not in out:
            out.append(x)
    return out


def say(item, zh: bool) -> str:
    return item[1] if zh else item[0]


def freq(rng) -> int:
    r = rng.random()
    return 1 if r < 0.35 else rng.randint(2, 4) if r < 0.8 else rng.randint(5, 9)


class Persona:
    def __init__(self, idx: int, seed: int, n_conv: int, days: int, exchanges: tuple):
        self.id = f"p{idx:02d}"
        self.rng = random.Random(f"{seed}-{idx}")
        self.n_conv, self.exchanges = n_conv, exchanges
        self.end = START + days * DAY
        self.zh_ratio = round(self.rng.uniform(0.2, 0.85), 2)
        self.platforms = self.rng.sample(CHAT, self.rng.randint(1, 3))
        if self.rng.random() < 0.75:
            self.platforms += self.rng.sample(CODING, self.rng.randint(1, 2))
        self.facts, self.distractors, self.plan, self.questions = [], [], [], []
        self.slot_values = {}     # slot -> list of fact dicts in time order (or set members)
        self.absent = set()

    # ---------- planning: which facts exist and when they are mentioned ----------
    def fact(self, slot, item, lo, hi, kind="static", **fields) -> dict:
        f = {"id": f"{self.id}-f{len(self.facts):03d}", "slot": slot, "kind": kind, "en": item[0], "zh": item[1],
             "aliases": forms(item), "valid_from": lo, "valid_to": hi, "mentions": [], "fields": fields}
        self.facts.append(f)
        self.slot_values.setdefault(slot, []).append(f)
        return f

    def mention(self, f, lo, hi, kind="mention", **fields):
        self.plan.append({"fact": f, "slot": f["slot"] if f else fields.pop("slot"), "kind": kind, "lo": lo, "hi": hi,
                          "fields": fields})

    def build_facts(self):
        rng, s, e = self.rng, START, self.end
        span = e - s
        self.species = rng.choice(V.SPECIES)
        absent = rng.sample(ABSENT_POOL, 3)
        self.absent = set(absent) | ({"sibling_job"} if "sibling" in absent or rng.random() < 0.1 else set())
        distract_only = set(absent[:2])
        used = {}   # slot -> values the persona really has (distractors must differ)

        for slot, spec in V.SLOTS.items():
            pool = spec["pool"]
            if slot in self.absent:
                continue
            if spec["kind"] == "static":
                item = rng.choice(pool)
                fields = {}
                if slot == "pet":
                    fields["species"] = self.species
                if slot == "sibling_job":
                    fields["sib"] = self.slot_values["sibling"][0]["item"]
                f = self.fact(slot, item, s, None, **fields)
                f["item"] = item
                for _ in range(freq(rng)):
                    self.mention(f, s, e, **fields)
                used[slot] = [item]
            elif spec["kind"] == "update":
                changes = 0 if rng.random() < 0.4 else (2 if spec.get("max_changes", 1) > 1 and rng.random() < 0.3 else 1)
                items = rng.sample(pool, changes + 1)
                cuts = [s] + sorted(s + int(span * rng.uniform(0.25, 0.85)) for _ in range(changes)) + [e]
                for i, item in enumerate(items):
                    lo, hi = cuts[i], cuts[i + 1]
                    f = self.fact(slot, item, lo, hi if i < changes else None, kind="update")
                    f["item"] = item
                    n = freq(rng)
                    if i == 0:
                        for _ in range(n):
                            self.mention(f, lo, hi)
                    else:
                        early = lo + max(1, (hi - lo) // 10)
                        self.mention(f, lo, early, kind="update", old=items[i - 1])
                        for _ in range(n - 1):
                            self.mention(f, early, hi)
                used[slot] = items
            else:   # set
                k = rng.randint(*spec["size"])
                items = rng.sample(pool, k)
                used[slot] = list(items)
                for item in items:
                    f = self.fact(slot, item, s, None, kind="set")
                    f["item"] = item
                    for _ in range(1 if rng.random() < 0.5 else 2):
                        self.mention(f, s, e)

        # each project is written in a language; that language also counts as one the user has used
        langs = self.slot_values.setdefault("prog_langs", [])
        for p in self.slot_values["project"]:
            known = [f["item"] for f in langs]
            item = rng.choice([x for x in V.LANGS if x not in known] or V.LANGS)
            f = self.fact("project_lang", item, p["valid_from"], p["valid_to"], kind="project_lang", proj=p["item"])
            f["item"], f["project"] = item, p["id"]
            for _ in range(rng.randint(1, 2)):
                self.mention(f, p["valid_from"], p["valid_to"] or e, proj=p["item"])
            if item not in known:
                langs.append(f)
                used.setdefault("prog_langs", []).append(item)

        # distractors: same pool, different value, never about the user
        for slot, spec in V.SLOTS.items():
            if not spec.get("dzh"):
                continue
            if slot in self.absent:
                n = 1 + (rng.random() < 0.4) if slot in distract_only else 0
            else:
                n = rng.choices([0, 1, 2], [0.4, 0.4, 0.2])[0]
            for _ in range(n):
                choices = [x for x in spec["pool"] if x not in used.get(slot, [])]
                d = rng.choice(choices)
                self.distractors.append({"slot": slot, "en": d[0], "zh": d[1], "aliases": forms(d), "messages": []})
                self.mention(None, s, e, kind="distractor", slot=slot, d=d, species=self.species,
                             ref=len(self.distractors) - 1)

    # ---------- conversations ----------
    def build_conversations(self):
        rng = self.rng
        times = sorted(rng.uniform(START, self.end) for _ in range(self.n_conv))
        self.convs = []
        for i, t in enumerate(times):
            plat = rng.choice(self.platforms)
            self.convs.append({"i": i, "t": t, "platform": plat, "zh": rng.random() < self.zh_ratio, "mentions": []})
        chat_idx = [c["i"] for c in self.convs if c["platform"] in CHAT]
        any_idx = [c["i"] for c in self.convs]
        for m in self.plan:
            idx = any_idx if V.SLOTS.get(m["slot"], {}).get("channel", "any") == "any" else chat_idx
            ts = [self.convs[i]["t"] for i in idx]
            a, b = bisect_left(ts, m["lo"]), bisect_right(ts, m["hi"])
            if a >= b:   # no conversation in the window: use the nearest allowed one
                j = min(range(len(ts)), key=lambda k: min(abs(ts[k] - m["lo"]), abs(ts[k] - m["hi"])))
            else:
                j = rng.randrange(a, b)
            self.convs[idx[j]]["mentions"].append(m)
        # an update must come after every earlier mention of the old value and before later mentions of the new one;
        # windows guarantee that across conversations, and within one conversation keep plan order by kind
        for c in self.convs:
            c["mentions"].sort(key=lambda m: (m["fact"]["valid_from"] if m["fact"] else 0, m["kind"] != "update"))
        self.conversations = [self.render(c) for c in self.convs]

    def sentence(self, m, zh: bool) -> str:
        rng = self.rng
        slot = m["slot"]
        spec = V.SLOTS.get(slot)
        if m["kind"] == "distractor":
            tmpl = rng.choice(spec["dzh"] if zh else spec["den"])
            return tmpl.format(d=say(m["fields"]["d"], zh), species=say(m["fields"]["species"], zh))
        f = m["fact"]
        if slot == "project_lang":
            tmpl = rng.choice(V.PROJECT_LANG["zh" if zh else "en"])
            return tmpl.format(v=say(f["item"], zh), proj=say(m["fields"]["proj"], zh))
        key = ("uzh" if zh else "uen") if m["kind"] == "update" else ("zh" if zh else "en")
        tmpl = rng.choice(spec[key])
        flds = f["fields"]
        return tmpl.format(v=say(f["item"], zh), old=say(m["fields"]["old"], zh) if "old" in m["fields"] else "",
                           species=say(flds["species"], zh) if "species" in flds else "",
                           sib=say(flds["sib"], zh) if "sib" in flds else "")

    def filler_user(self, platform, zh) -> str:
        rng = self.rng
        coding = platform in CODING or rng.random() < 0.4
        pool = (V.CODE_ZH if zh else V.CODE_EN) if coding else (V.GEN_ZH if zh else V.GEN_EN)
        t, u = rng.sample(V.TOOLS, 2)
        text = rng.choice(pool).format(t=t, u=u)
        if coding and rng.random() < 0.3:
            text += "\n\n```\n" + "\n".join(rng.choice(V.CODE_LINES) for _ in range(rng.randint(5, 30))) + "\n```"
        return text

    def assistant(self, zh) -> str:
        rng = self.rng
        parts = [rng.choice(V.ANS_ZH if zh else V.ANS_EN) for _ in range(rng.randint(2, 6))]
        if rng.random() < 0.3:
            parts.append("\n```\n" + "\n".join(rng.choice(V.CODE_LINES) for _ in range(rng.randint(4, 15))) + "\n```")
        return ("" if zh else " ").join(parts)

    def render(self, c) -> dict:
        rng, zh, plat = self.rng, c["zh"], c["platform"]
        n = max(rng.randint(*self.exchanges), len(c["mentions"]) + 1)
        slots = sorted(rng.sample(range(n), len(c["mentions"])))
        at = dict(zip(slots, c["mentions"]))
        native = f"{self.id}-{c['i']:04d}"
        msgs, t = [], c["t"]
        for j in range(n):
            mid = f"{native}-{j}u"
            m = at.get(j)
            if m:
                text = self.sentence(m, zh)
                if text[-1] in "。." and rng.random() < 0.5:
                    text += ("" if zh else " ") + self.filler_user(plat, zh)
                if m["kind"] == "distractor":
                    self.distractors[m["fields"]["ref"]]["messages"].append(mid)
                else:
                    m["fact"]["mentions"].append({"message": mid, "kind": m["kind"], "at": round(t)})
            else:
                text = self.filler_user(plat, zh)
            msgs.append({"role": "user", "text": text, "id": mid, "created_at": round(t), "kind": "message"})
            if plat in CODING and rng.random() < 0.5:
                msgs.append({"role": "assistant", "kind": "tool_call", "text": "Read(src/app.py)", "created_at": round(t) + 10})
                msgs.append({"role": "tool", "kind": "tool_result", "created_at": round(t) + 11,
                             "text": "\n".join(rng.choice(V.CODE_LINES) for _ in range(rng.randint(10, 60)))})
            msgs.append({"role": "assistant", "text": self.assistant(zh), "id": f"{native}-{j}a",
                         "created_at": round(t) + 30, "model": MODEL[plat], "kind": "message"})
            t += 120
        return {"id": f"{plat}:{native}", "platform": plat, "native_id": native, "title": "", "created_at": round(c["t"]),
                "updated_at": round(t), "project": None, "model": MODEL[plat], "source_path": None,
                "meta": {"synthetic": True, "persona": self.id, "lang": "zh" if zh else "en"}, "messages": msgs,
                "branches": []}

    # ---------- questions ----------
    def ask(self, qtype, slot, q, answer=None, traps=(), evidence=(), facts=(), **extra):
        qid = f"{self.id}-q{len(self.questions):03d}"
        split = "dev" if int(hashlib.sha256(qid.encode()).hexdigest(), 16) % 10 < 3 else "test"
        trap_forms = []
        for t in traps:
            for a in t:
                if a not in trap_forms and (answer is None or a not in answer):
                    trap_forms.append(a)
        self.questions.append({"id": qid, "persona": self.id, "type": qtype, "slot": slot,
                               "question": {"zh": q[0], "en": q[1]}, "answer": answer, "traps": trap_forms,
                               "evidence": sorted(set(evidence)), "facts": list(facts), "split": split, **extra})

    def distractor_forms(self, slot) -> list:
        return [d["aliases"] for d in self.distractors if d["slot"] == slot and d["messages"]]

    def build_questions(self):
        for slot, spec in V.SLOTS.items():
            if slot in self.absent:
                qtype = "abstain"
                self.ask(qtype, slot, spec["q"], None, self.distractor_forms(slot),
                         [m for d in self.distractors if d["slot"] == slot for m in d["messages"]],
                         answer_kind="unknown", subtype="distractor_only" if self.distractor_forms(slot) else "never")
                continue
            vals = [f for f in self.slot_values.get(slot, []) if f["slot"] == slot]
            if spec["kind"] == "set":
                members = self.slot_values[slot]
                ev = [m["message"] for f in members for m in f["mentions"]]
                self.ask("aggregate_list", slot, spec["q"], [f["aliases"] for f in members],
                         self.distractor_forms(slot), ev, [f["id"] for f in members], answer_kind="list")
                n = len(members)
                self.ask("aggregate_count", slot, spec["qcount"], [str(n), ZH_NUM[n] if n <= 10 else str(n)],
                         [], ev, [f["id"] for f in members], answer_kind="count")
                continue
            cur = vals[-1]
            older = [f["aliases"] for f in vals[:-1]]
            ev = [m["message"] for m in cur["mentions"]]
            nm = len(cur["mentions"])
            if spec.get("qtype"):
                qtype = spec["qtype"]
            elif len(vals) > 1:
                qtype = "update_current"
            else:
                qtype = "long_tail" if nm == 1 else "single_fact"
            self.ask(qtype, slot, spec["q"], cur["aliases"], older + self.distractor_forms(slot), ev, [cur["id"]],
                     answer_kind="value", mentions=nm)
            if len(vals) > 1 and qtype == "update_current":
                for i in range(1, len(vals)):
                    nxt, prev = vals[i], vals[i - 1]
                    q = (spec["qprev"][0].format(v=nxt["zh"]), spec["qprev"][1].format(v=nxt["en"]))
                    others = [f["aliases"] for f in vals if f is not prev and f is not nxt]
                    self.ask("update_previous", slot, q, prev["aliases"], others + self.distractor_forms(slot),
                             [m["message"] for m in prev["mentions"]]
                             + [m["message"] for m in nxt["mentions"] if m["kind"] == "update"],
                             [prev["id"], nxt["id"]], answer_kind="value")

        # multi-hop: current city -> country (world knowledge); current project -> its language (two facts)
        cities = self.slot_values["city"]
        cur = cities[-1]
        country = next(c for c in V.CITIES if c[0] == cur["en"])
        ans = [country[2], country[3]] + V.COUNTRY_ALIASES.get(country[2], [])
        traps = []
        for f in cities[:-1]:
            c = next(c for c in V.CITIES if c[0] == f["en"])
            if c[2] != country[2]:
                traps.append([c[2], c[3]])
        self.ask("multi_hop", "city_country", V.MULTI_HOP_Q["city_country"], ans, traps,
                 [m["message"] for m in cur["mentions"]], [cur["id"]], answer_kind="value", hop="world")
        projs = self.slot_values["project"]
        pl = {f["project"]: f for f in self.slot_values["project_lang"]}
        cur_lang = pl[projs[-1]["id"]]
        traps = [pl[p["id"]]["aliases"] for p in projs[:-1] if pl[p["id"]]["en"] != cur_lang["en"]]
        self.ask("multi_hop", "project_lang", V.MULTI_HOP_Q["project_lang"], cur_lang["aliases"], traps,
                 [m["message"] for m in projs[-1]["mentions"] + cur_lang["mentions"]], [projs[-1]["id"], cur_lang["id"]],
                 answer_kind="value", hop="link")

    def stats(self) -> dict:
        msgs = [m for c in self.conversations for m in c["messages"]]
        user = [m for m in msgs if m["role"] == "user"]
        chars = sum(len(m["text"]) for m in msgs)
        return {"persona": self.id, "zh_ratio": self.zh_ratio, "platforms": self.platforms,
                "conversations": len(self.conversations), "messages": len(msgs), "user_messages": len(user),
                "chars": chars, "user_chars": sum(len(m["text"]) for m in user),
                "facts": len(self.facts), "fact_mentions": sum(len(f["mentions"]) for f in self.facts),
                "distractor_mentions": sum(len(d["messages"]) for d in self.distractors),
                "absent_slots": sorted(self.absent), "questions": dict(Counter(q["type"] for q in self.questions))}


def generate(personas: int = 8, conversations: int = 200, days: int = 540, seed: int = 2026,
             exchanges: tuple = (2, 10)) -> list:
    out = []
    for i in range(personas):
        p = Persona(i, seed, conversations, days, exchanges)
        p.build_facts()
        p.build_conversations()
        p.build_questions()
        out.append(p)
    return out


def ci_half_width(n: int, p: float = 0.5) -> float:
    return round(1.96 * math.sqrt(p * (1 - p) / n), 3) if n else None


def write(ps: list, out: Path, db: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for p in ps:
        d = out / p.id
        d.mkdir(exist_ok=True)
        with open(d / "conversations.jsonl", "w", encoding="utf-8") as fh:
            for c in p.conversations:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        facts = [{k: v for k, v in f.items() if k not in ("item",)} for f in p.facts]
        for f in facts:
            f["fields"] = {k: list(v) if isinstance(v, tuple) else v for k, v in f["fields"].items()}
        (d / "facts.json").write_text(json.dumps({"persona": p.id, "absent": sorted(p.absent), "facts": facts,
                                                  "distractors": p.distractors}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
        with open(d / "questions.jsonl", "w", encoding="utf-8") as fh:
            for q in p.questions:
                fh.write(json.dumps(q, ensure_ascii=False) + "\n")
        if db:
            load_store(d / "conversations.jsonl", d / "home" / "ctxport.db", p.id)
    counts = Counter(q["type"] for p in ps for q in p.questions)
    test = Counter(q["type"] for p in ps for q in p.questions if q["split"] == "test")
    manifest = {"generator": "benchmarks/memsynth/generate.py", "personas": [p.stats() for p in ps],
                "questions": {t: {"all": counts[t], "test": test[t], "ci95_half_width_at_50pct": ci_half_width(test[t])}
                              for t in QTYPES}}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def load_store(conversations: Path, db_path: Path, persona: str) -> int:
    """Write the persona's conversations into a fresh ctxport store, as an import would."""
    from ctxport.models import Conversation
    from ctxport.store import Store
    s = Store(db_path)
    n = 0
    for line in open(conversations, encoding="utf-8"):
        s.upsert_conversation(Conversation.from_dict(json.loads(line)).finalize(), f"memsynth:{persona}")
        n += 1
    s.commit()
    s.close()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--personas", type=int, default=8)
    ap.add_argument("--conversations", type=int, default=200, help="conversations per persona")
    ap.add_argument("--days", type=int, default=540, help="length of each history")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", type=Path, default=Path("memsynth-out"))
    ap.add_argument("--db", action="store_true", help="also write a ctxport store per persona (<out>/<id>/home)")
    a = ap.parse_args(argv)
    m = write(generate(a.personas, a.conversations, a.days, a.seed), a.out, a.db)
    tot = {k: sum(p[k] for p in m["personas"]) for k in ("conversations", "messages", "chars", "fact_mentions",
                                                          "distractor_mentions")}
    print(json.dumps({"out": str(a.out), **tot}, ensure_ascii=False))
    for t, v in m["questions"].items():
        print(f"  {t:16} all={v['all']:4}  test={v['test']:4}  ±{v['ci95_half_width_at_50pct']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
