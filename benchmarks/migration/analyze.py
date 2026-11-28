"""Analyse migration-loss rows: accuracy with persona-clustered CIs, paired tests, TOST, loss decomposition.

    python benchmarks/migration/analyze.py ROWS.jsonl [ROWS2.jsonl ...] [--split test] [--out report.md]
        [--compare "chmd-llm@1400:summary@1400,chmd-write@1400:summary@1400"] [--delta 5]

Statistics (personas are the clustering unit; questions within a persona are not independent):
  CI       95% percentile interval over 10,000 persona-level bootstrap resamples of pooled accuracy
  p        paired sign-flip permutation test on the sum of per-persona accuracy differences
           (exact up to 2^20 sign patterns, otherwise 200,000 random flips), Holm-corrected over the comparisons
  TOST     equivalence at margin ±delta points: the 90% bootstrap interval of the difference lies inside (-d, d)
  decomposition (eq. 2' of docs/paper/revision-math.md), on value/list questions only:
           E = gold in Z_inf or in Z (definition (a)), K = gold in Z, C = correct
           l_ext = P(~E & ~C), l_bud = P(E & ~K & ~C), l_read = P(K & ~C), gamma = P(~K & C)
           These three losses partition the errors exactly. nesting_violations = #(K & gold not in Z_inf).
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
from collections import defaultdict
from pathlib import Path

TYPES = ["single_fact", "long_tail", "update_current", "update_previous", "preference", "aggregate_list",
         "aggregate_count", "multi_hop", "abstain"]
SHORT = {"single_fact": "single", "long_tail": "tail", "update_current": "cur", "update_previous": "prev",
         "preference": "pref", "aggregate_list": "list", "aggregate_count": "count", "multi_hop": "hop",
         "abstain": "abst"}


def load(paths, split=None):
    rows = []
    for p in paths:
        rows += [json.loads(x) for x in open(p, encoding="utf-8")]
    return [r for r in rows if not split or r["split"] == split]


def by_persona(rows):
    d = defaultdict(lambda: [0, 0])
    for r in rows:
        d[r["persona"]][0] += bool(r["correct"])
        d[r["persona"]][1] += 1
    return d


def boot_ci(per: dict, n_boot=10_000, seed=0, level=0.95):
    keys = list(per)
    rng = random.Random(seed)
    stats = []
    for _ in range(n_boot):
        s = [per[rng.choice(keys)] for _ in keys]
        stats.append(100 * sum(a for a, _ in s) / max(1, sum(b for _, b in s)))
    stats.sort()
    lo = stats[int((1 - level) / 2 * n_boot)]
    hi = stats[int((1 + level) / 2 * n_boot) - 1]
    return lo, hi


def paired(rows_a, rows_b, n_boot=10_000, seed=1):
    """Per-persona accuracy difference A-B on the same questions."""
    ka = {(r["persona"], r["qid"]): r["correct"] for r in rows_a}
    kb = {(r["persona"], r["qid"]): r["correct"] for r in rows_b}
    common = ka.keys() & kb.keys()
    per = defaultdict(lambda: [0, 0, 0])
    disc = [0, 0]
    for k in common:
        per[k[0]][0] += bool(ka[k])
        per[k[0]][1] += bool(kb[k])
        per[k[0]][2] += 1
        if ka[k] and not kb[k]:
            disc[0] += 1
        elif kb[k] and not ka[k]:
            disc[1] += 1
    diffs = {p: 100 * (a - b) / n for p, (a, b, n) in per.items() if n}
    keys = list(diffs)
    if not keys:
        return None
    delta = 100 * sum(a - b for a, b, _ in per.values()) / sum(n for *_, n in per.values())
    rng = random.Random(seed)
    bs = []
    for _ in range(n_boot):
        s = [per[rng.choice(keys)] for _ in keys]
        bs.append(100 * sum(a - b for a, b, _ in s) / max(1, sum(n for *_, n in s)))
    bs.sort()
    ci95 = (bs[int(0.025 * n_boot)], bs[int(0.975 * n_boot) - 1])
    ci90 = (bs[int(0.05 * n_boot)], bs[int(0.95 * n_boot) - 1])
    obs = abs(sum(diffs.values()))
    vals = list(diffs.values())
    n = len(vals)
    if n <= 20:
        hits = total = 0
        for signs in itertools.product((1, -1), repeat=n):
            total += 1
            hits += abs(sum(s * v for s, v in zip(signs, vals))) >= obs - 1e-9
        p = hits / total
    else:
        trials = 200_000
        hits = sum(abs(sum(v if rng.random() < 0.5 else -v for v in vals)) >= obs - 1e-9 for _ in range(trials))
        p = (hits + 1) / (trials + 1)
    wins = sum(v > 0 for v in vals)
    return {"delta": delta, "ci95": ci95, "ci90": ci90, "p": p, "n_personas": n, "wins": wins,
            "losses": sum(v < 0 for v in vals), "discordant": disc, "min_p": 2 / 2 ** n if n <= 30 else 0.0}


def holm(ps: list) -> list:
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, running = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = running
    return adj


def decompose(rows):
    rs = [r for r in rows if r["in_ctx"] is not None]
    n = len(rs)
    if not n:
        return None
    E = lambda r: bool(r["in_ctx"]) or bool(r["in_inf"])  # noqa: E731
    out = {
        "n": n,
        "acc": sum(r["correct"] for r in rs) / n,
        "l_ext": sum(not E(r) and not r["correct"] for r in rs) / n,
        "l_bud": sum(E(r) and not r["in_ctx"] and not r["correct"] for r in rs) / n,
        "l_read": sum(bool(r["in_ctx"]) and not r["correct"] for r in rs) / n,
        "gamma": sum(not r["in_ctx"] and r["correct"] for r in rs) / n,
        "rho": sum(E(r) for r in rs) / n,
        "contain": sum(bool(r["in_ctx"]) for r in rs) / n,
        "nesting_violations": sum(bool(r["in_ctx"]) and r["in_inf"] is False for r in rs),
    }
    k = [r for r in rs if r["in_ctx"]]
    out["upsilon"] = sum(r["correct"] for r in k) / len(k) if k else float("nan")
    assert abs(out["l_ext"] + out["l_bud"] + out["l_read"] - (1 - out["acc"])) < 1e-9
    return out


def fmt(x, d=1):
    return "–" if x is None else f"{x:.{d}f}"


def report(rows, compares, delta, title=""):
    lines = [f"# {title}".rstrip(), ""]
    readers = sorted({r["reader"] for r in rows})
    for reader in readers:
        R = [r for r in rows if r["reader"] == reader]
        methods = sorted({r["method"] for r in R}, key=lambda m: (m.split("@")[0], int(m.split("@")[1]) if "@" in m
                                                                   and m.split("@")[1].isdigit() else 10 ** 9))
        lines += [f"## Reader: {reader}", "", f"personas: {len({r['persona'] for r in R})}, "
                  f"questions/method: {len(R) // max(1, len(methods))}", ""]
        head = "| method | chars | acc | 95% CI | " + " | ".join(SHORT[t] for t in TYPES) + " | wrong | stale |"
        lines += [head, "|" + "---|" * (head.count("|") - 1)]
        by_m = defaultdict(list)
        for r in R:
            by_m[r["method"]].append(r)
        for m in methods:
            rs = by_m[m]
            acc = 100 * sum(r["correct"] for r in rs) / len(rs)
            lo, hi = boot_ci(by_persona(rs))
            cells = []
            for t in TYPES:
                ts = [r for r in rs if r["type"] == t]
                cells.append(fmt(100 * sum(r["correct"] for r in ts) / len(ts)) if ts else "–")
            nonab = [r for r in rs if r["type"] != "abstain"]
            wrong = 100 * sum(not r["correct"] and not r["abstained"] for r in nonab) / max(1, len(nonab))
            cur = [r for r in rs if r["type"] == "update_current"]
            stale = sum(r["trap"] for r in cur)
            chars = sum(r["ctx_chars"] for r in rs) / len(rs)
            lines.append(f"| {m} | {chars:.0f} | {acc:.1f} | [{lo:.1f}, {hi:.1f}] | " + " | ".join(cells)
                         + f" | {wrong:.1f} | {stale}/{len(cur)} |")
        lines += ["", "### Loss decomposition (eq. 2', value/list questions)", "",
                  "| method | acc | l_ext | l_bud | l_read | gamma | rho | contain | upsilon | nest.viol |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for m in methods:
            d = decompose(by_m[m])
            if d:
                lines.append(f"| {m} | {100*d['acc']:.1f} | {100*d['l_ext']:.1f} | {100*d['l_bud']:.1f} | "
                             f"{100*d['l_read']:.1f} | {100*d['gamma']:.1f} | {100*d['rho']:.1f} | "
                             f"{100*d['contain']:.1f} | {100*d['upsilon']:.1f} | {d['nesting_violations']} |")
        res = []
        for a, b in compares:
            if a in by_m and b in by_m:
                res.append((a, b, paired(by_m[a], by_m[b])))
        res = [x for x in res if x[2]]
        if res:
            adj = holm([x[2]["p"] for x in res])
            lines += ["", f"### Paired comparisons (Holm over {len(res)}; TOST margin ±{delta})", "",
                      "| A − B | Δ | 95% CI | 90% CI | p_Holm | wins/losses | discordant A/B | TOST |",
                      "|---|---|---|---|---|---|---|---|"]
            for (a, b, s), pa in zip(res, adj):
                eq = "equivalent" if -delta < s["ci90"][0] and s["ci90"][1] < delta else "not shown"
                lines.append(f"| {a} − {b} | {s['delta']:+.1f} | [{s['ci95'][0]:+.1f}, {s['ci95'][1]:+.1f}] | "
                             f"[{s['ci90'][0]:+.1f}, {s['ci90'][1]:+.1f}] | {pa:.3g} | {s['wins']}/{s['losses']} | "
                             f"{s['discordant'][0]}/{s['discordant'][1]} | {eq} |")
        # proposition 1: current/previous pairs with both values in context
        pairs = defaultdict(dict)
        for r in R:
            if r["type"] in ("update_current", "update_previous"):
                pairs[(r["method"], r["persona"], r["qid"])] = r
        lines.append("")
    return "\n".join(lines)


DEFAULT_COMPARE = ("chmd-detT@1400:chmd-det@1400,chmd-llm@1400:chmd-detT@1400,chmd-write@1400:summary@1400,"
                   "chmd-llm@1400:summary@1400,chmd-write@1400:chmd-llm@1400,summary@1400:rolling@1400,"
                   "chmd-write@1400:rolling@1400,chmd-write@1400:bm25@1400,full:chmd-write@1400")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rows", nargs="+", type=Path)
    ap.add_argument("--split", default="test")
    ap.add_argument("--compare", default=DEFAULT_COMPARE)
    ap.add_argument("--delta", type=float, default=5.0)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--title", default="Migration-loss results")
    a = ap.parse_args(argv)
    rows = load(a.rows, None if a.split == "all" else a.split)
    compares = [tuple(x.split(":")) for x in a.compare.split(",") if ":" in x]
    text = report(rows, compares, a.delta, a.title)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
