"""Real-data study, round 2: compare at the size of ChatGPT's own export (~16k chars) and add CHMD variants that
keep old years.

    python benchmarks/migration/real_extra.py contexts [--model claude:haiku]
    then: realdata.py read --methods <the names printed> ; realdata.py judge ; realdata.py report

Variants (all built from the same L2 facts, ~/.ctxport/distilled/facts.json):
  chmd-balanced@B   distill.render_balanced: every category gets a share of B, then importance
  chmd-years@B      B split across the years facts were FIRST stated (share ~ sqrt(#facts that year), floor 8%),
                    each year rendered category-balanced. Motivation (decided before looking at any real-data
                    accuracy): importance decays with recency, so a pure importance cut drops whole early years.
  chmd-write-years@B  the model writes each year's section from that year's facts within its share
  chmd+bm25@B       chmd-years@(B-4000) as the standing profile + 4000 chars retrieved per question, i.e. how
                    ctxport is used with its MCP server (profile in the rules file, search_history on demand)
  summary@B         hierarchical summary at the same size
  chmd-full         the whole profile.md (ceiling for static CHMD; only for long-context readers)
Questions get a fixed 30/70 dev/test split by id hash; report test numbers when choosing between variants.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
import pipeline as P  # noqa: E402
import realdata as RD  # noqa: E402
from ctxport import distill as D  # noqa: E402
from ctxport.store import Store  # noqa: E402

B = 16000


def split(qid: str) -> str:
    return "dev" if int(hashlib.sha256(qid.encode()).hexdigest(), 16) % 10 < 3 else "test"


def by_year(canon: list) -> dict:
    out = defaultdict(list)
    for c in canon:
        out[RD._year(c.get("first") or c.get("last"))].append(c)
    return dict(sorted(out.items()))


def shares(groups: dict, budget: int, floor: float = 0.08) -> dict:
    w = {y: math.sqrt(len(v)) for y, v in groups.items()}
    tot = sum(w.values()) or 1
    raw = {y: max(floor, w[y] / tot) for y in groups}
    norm = sum(raw.values())
    return {y: int(budget * raw[y] / norm) for y in groups}


def years_render(canon: list, budget: int) -> str:
    groups = by_year(canon)
    parts = []
    for y, share in shares(groups, budget).items():
        head = f"## {y}\n"
        text, _ = D.render_balanced(groups[y], max(0, share - len(head) - 2), headers=False)
        parts.append(head + text)
    return "\n".join(parts)[:budget]


YEAR_PROMPT = """Below are facts about a user that they first stated in {year}, each with the month last mentioned
and how often it came up (xN). Write the {year} section of a long-term profile for a NEW assistant. Hard limit:
{budget} characters. Keep the most identifying and useful facts: who they were then, school/work, places, people,
devices, projects, preferences, plans. If a fact was later superseded, say "(as of {year})". Do not invent anything.
Output only the section text."""


def write_years(canon: list, budget: int, model: str) -> str:
    groups = by_year(canon)
    parts = []
    for y, share in shares(groups, budget).items():
        items = sorted(groups[y], key=lambda c: -D.importance(c))
        listing, used = [], 0
        for c in items:
            ln = f"- [{D._d(c.get('last'))}] (x{c.get('count', 1)}) {c['text']}"
            if used + len(ln) > P.SUMMARY_CHUNK:
                break
            listing.append(ln)
            used += len(ln) + 1
        r = llm.call(model, "\n".join(listing), system=llm.STRICT + "\n\n" + YEAR_PROMPT.format(year=y, budget=share - 12))
        parts.append(f"## {y}\n" + P._fit(r["text"].strip(), share - 12))
    return "\n".join(parts)[:budget]


def cmd_contexts(a) -> None:
    out = RD.OUT
    d = json.loads((out / "contexts.json").read_text(encoding="utf-8"))
    canon = json.loads((RD.HOME / "distilled" / "facts.json").read_text(encoding="utf-8"))["l2"]
    store = Store(RD.HOME / "ctxport.db")
    msgs = RD.real_messages(store)
    store.close()

    class Fake:
        def user_messages(self):
            return [(ts, plat, t) for ts, plat, _, t in msgs]

    ctx = d["contexts"]
    ctx[f"chmd-balanced@{B}"], _ = D.render_balanced(canon, B)
    ctx[f"chmd-years@{B}"] = years_render(canon, B)
    ctx[f"chmd-write-years@{B}"] = write_years(canon, B, a.model)
    ctx[f"summary@{B}"] = P.summary(Fake(), B, a.model)
    full = (RD.HOME / "distilled" / "profile.md").read_text(encoding="utf-8")
    ctx["chmd-full"] = full
    # profile + per-question retrieval
    sents = []
    for ts, _, _, t in msgs:
        for s in re.split(r"(?<=[。！？!?.])\s*", t):
            if len(s.strip()) > 3:
                sents.append(f"[{P._day(ts)}] {s.strip()}")
    idx = P.BM25(sents)
    base = years_render(canon, B - 4000)
    hyb = {}
    for q in RD._gold():
        text, used = [], 0
        for i in idx.rank(q["question"]):
            s = sents[i] + "\n"
            if used + len(s) > 4000:
                continue
            text.append(s)
            used += len(s)
        hyb[q["id"]] = base + "\n\n## Retrieved from history for this question\n" + "".join(text)
    d["bm25"][f"chmd+bm25@{B}"] = hyb
    (out / "contexts.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    names = [f"chmd-balanced@{B}", f"chmd-years@{B}", f"chmd-write-years@{B}", f"summary@{B}", f"chmd+bm25@{B}"]
    print(json.dumps({n: len(ctx.get(n, "")) for n in names + ["chmd-full", "b1-chatgpt"]}))
    print(",".join(names))


def cmd_split_report(a) -> None:
    """Accuracy on the test split (and dev) per method/reader, with paired comparison against b1-chatgpt."""
    bank = {q["id"]: q for q in RD._gold() if q["labeled"]}
    rows = [r for r in map(json.loads, open(out_f(), encoding="utf-8")) if r["qid"] in bank and r.get("grade")]
    import random
    rng = random.Random(0)
    by = defaultdict(dict)
    for r in rows:
        by[(r["reader"], r["method"])][r["qid"]] = r["grade"] == "correct"
    years = RD.YEARS
    for part in ("test", "dev"):
        print(f"\n### {part} split")
        for reader in sorted({k[0] for k in by}):
            ref = by.get((reader, "b1-chatgpt"), {})
            print(f"\n{reader}\n| method | acc | " + " | ".join(years) + " | Δ vs b1 [95% CI] |")
            for (rd, m), res in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
                if rd != reader:
                    continue
                qs = [q for q in res if split(q) == part]
                if not qs:
                    continue
                acc = 100 * sum(res[q] for q in qs) / len(qs)
                cells = []
                for y in years:
                    ys = [q for q in qs if bank[q]["year"] == y]
                    cells.append(f"{100 * sum(res[q] for q in ys) / len(ys):.0f}" if ys else "–")
                common = [q for q in qs if q in ref]
                delta = ""
                if common and m != "b1-chatgpt":
                    diffs = [res[q] - ref[q] for q in common]
                    bs = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(4000))
                    delta = f"{100 * sum(diffs) / len(diffs):+.1f} [{100 * bs[100]:+.1f}, {100 * bs[3899]:+.1f}]"
                print(f"| {m} | {acc:.1f} | " + " | ".join(cells) + f" | {delta} |")


def out_f():
    return RD.OUT / "graded.jsonl"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("contexts")
    c.add_argument("--model", default="claude:haiku")
    sub.add_parser("split-report")
    a = ap.parse_args(argv)
    {"contexts": cmd_contexts, "split-report": cmd_split_report}[a.cmd](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
