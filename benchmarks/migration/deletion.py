"""Auditability experiment: the user asks the assistant to forget specific facts.

    python benchmarks/migration/deletion.py --data ~/.ctxport/research/synth/S --extractor claude:haiku \
        --reader claude:haiku [--per-persona 3]

For each persona pick up to K single-fact / long-tail questions as "forget" targets. Then:
  chmd    drop every L2 fact whose text names the value (an id-level operation: the L1 ids it covers and their
          source conversations are known, so the deletion is logged and survives re-runs), re-render at B
  summary ask the extractor model to rewrite summary@B without that fact (the only operation a free-text
          profile allows)
Measured: residual (value still in Z), leak (reader still answers the forgotten value), and collateral
(accuracy on every other question before vs after). Output: <data>/results/<extractor>/deletion-<reader>.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
import pipeline as P  # noqa: E402

FORGET_PROMPT = """You maintain a memory profile of a user for an AI assistant. The user has asked you to FORGET
the fact described below: remove it and anything that reveals it, keep everything else unchanged and in the same
style. Stay under {budget} characters. Output only the updated profile."""


def run_persona(pd: Path, out: Path, extractor: str, reader: str, k: int, budget: int) -> list:
    p = P.Persona(pd)
    d = json.loads((out / "contexts" / f"{p.id}.json").read_text(encoding="utf-8"))
    cards = json.loads((pd / f"cards-{P._tag(extractor)}.json").read_text(encoding="utf-8"))
    canon = P.canonical(cards, merger=extractor)            # replayed from the call cache
    rng = random.Random(p.id)
    cands = [q for q in p.questions if q["type"] in ("single_fact", "long_tail") and q.get("answer")]
    rng.shuffle(cands)
    targets = cands[:k]
    rows = []
    for t in targets:
        aliases = t["answer"]
        others = [q for q in p.questions if q["id"] != t["id"] and q["slot"] != t["slot"]]
        kept = [c for c in canon if not P.MS.any_hit(aliases, P.MS.norm(c["text"]))]
        dropped = [c for c in canon if c not in kept]
        chmd_before, _ = P.render(canon, budget, True)
        chmd_after, _ = P.render(kept, budget, True)
        summ_before = d["contexts"][f"summary@{budget}"]
        desc = f'{t["question"]["en"]} -> "{aliases[0]}"'
        r = llm.call(extractor, f"FACT TO FORGET: {desc}\n\nPROFILE:\n{summ_before}",
                     system=llm.STRICT + "\n\n" + FORGET_PROMPT.format(budget=budget))
        summ_after = P._fit(r["text"].strip(), budget)
        for method, before, after in (("chmd", chmd_before, chmd_after), ("summary", summ_before, summ_after)):
            ans_t = P.read(reader, after, [t]).get(t["id"])
            acc_b = P.read(reader, before, others)
            acc_a = P.read(reader, after, others)
            ok_b = sum(P.judge(q, acc_b.get(q["id"]))["correct"] for q in others)
            ok_a = sum(P.judge(q, acc_a.get(q["id"]))["correct"] for q in others)
            rows.append({"persona": p.id, "target": t["id"], "method": method, "budget": budget,
                         "residual": P.MS.any_hit(aliases, P.MS.norm(after)),
                         "in_before": P.MS.any_hit(aliases, P.MS.norm(before)),
                         "leak": P.MS.any_hit(aliases, P.MS.norm(ans_t or "")),
                         "others_n": len(others), "others_before": ok_b, "others_after": ok_a,
                         "l2_dropped": len(dropped) if method == "chmd" else None,
                         "l1_ids_dropped": sum(len(c["covers"]) for c in dropped) if method == "chmd" else None})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--extractor", default="claude:haiku")
    ap.add_argument("--reader", default="claude:haiku")
    ap.add_argument("--per-persona", type=int, default=3)
    ap.add_argument("--budget", type=int, default=4000)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args(argv)
    out = a.data / "results" / P._tag(a.extractor)
    pdirs = sorted(x for x in a.data.glob("p*") if (out / "contexts" / f"{x.name}.json").exists())
    with ThreadPoolExecutor(a.workers) as ex:
        rows = [r for rs in ex.map(lambda pd: run_persona(pd, out, a.extractor, a.reader, a.per_persona, a.budget),
                                   pdirs) for r in rs]
    f = out / f"deletion-{P._tag(a.reader)}.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    for m in ("chmd", "summary"):
        rs = [r for r in rows if r["method"] == m]
        if not rs:
            continue
        base = [r for r in rs if r["in_before"]]
        print(f"{m:8} targets={len(rs)} present_before={len(base)} residual={sum(r['residual'] for r in base)}/{len(base)}"
              f" leak={sum(r['leak'] for r in base)}/{len(base)} others: {sum(r['others_before'] for r in rs)}"
              f" -> {sum(r['others_after'] for r in rs)} of {sum(r['others_n'] for r in rs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
