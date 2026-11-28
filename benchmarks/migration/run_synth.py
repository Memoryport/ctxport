"""Run the migration-loss experiment on memsynth personas.

    python benchmarks/migration/run_synth.py --data ~/.ctxport/research/synth/S --extractor claude:haiku \
        --readers claude:haiku,ollama:qwen3.5-9b-uncensored-aggressive:latest [--personas p000,p001] [--stage all]

Stages: contexts (cards, L2, every method's context, offline cost) then read (every reader on every context).
Outputs under <data>/results/<extractor>/: contexts/<persona>.json, cost.jsonl, rows-<reader>.jsonl
(one row per persona × question × method × reader, with correctness, abstention, trap and gold containment).
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as P  # noqa: E402

BUDGETS = (700, 1400, 4000)


def contexts_for(p: P.Persona, extractor: str, out: Path, rolling: bool) -> dict:
    f = out / "contexts" / f"{p.id}.json"
    old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    if old.get("_done"):
        return old
    cost = {}

    def counter():
        return {"input": 0, "output": 0, "calls": 0}

    c = cost.setdefault("l1", counter())
    cards = P.build_cards(p, extractor, log=c)
    det = P.canonical(cards)
    cost["l2_merge"] = counter()
    llm_c = P.canonical(cards, merger=extractor, log=cost["l2_merge"])
    ctx = {"none": "", "full": P.dated_text(p.user_messages())}
    ids = {}
    for name, canon, dated in (("chmd-det", det, False), ("chmd-detT", det, True), ("chmd-llm", llm_c, True)):
        ctx[f"{name}@inf"], ids[f"{name}@inf"] = P.render(canon, None, dated)
        for b in BUDGETS:
            ctx[f"{name}@{b}"], ids[f"{name}@{b}"] = P.render(canon, b, dated)
    for b in BUDGETS:
        cost[f"chmd-write@{b}"] = counter()
        ctx[f"chmd-write@{b}"] = P.chmd_write(llm_c, b, extractor, log=cost[f"chmd-write@{b}"])
        ctx[f"recent@{b}"] = P.recent(p, b)
        cost[f"summary@{b}"] = counter()
        ctx[f"summary@{b}"] = P.summary(p, b, extractor, log=cost[f"summary@{b}"])
    if rolling:
        cost["rolling@1400"] = counter()
        profs = P.rolling(p, 1400, extractor, log=cost["rolling@1400"])
        ctx["rolling@1400"] = profs[-1]
    bm = {f"bm25@{b}": P.bm25_contexts(p, b, p.questions) for b in (1400, 4000)}
    d = {"persona": p.id, "contexts": ctx, "bm25": bm, "ids": ids, "cost": cost,
         "facts": {"l1": sum(len(cd["facts"]) for cd in cards.values()), "l2_det": len(det), "l2_llm": len(llm_c),
                   "cards": len(cards), "convs": len(p.convs)},
         "_done": True}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def read_persona(p: P.Persona, d: dict, reader: str, methods: list) -> list:
    rows = []
    for m in methods:
        if m.startswith("bm25@"):
            if m not in d["bm25"]:
                continue
            ans = P.read_each(reader, d["bm25"][m], p.questions)
            zs = d["bm25"][m]
        else:
            if m not in d["contexts"]:
                continue
            ans = P.read(reader, d["contexts"][m], p.questions)
            zs = None
        base = m.split("@")[0]
        inf = (d["contexts"].get("chmd-llm@inf") if base == "chmd-write" else d["contexts"].get(f"{base}@inf")
               if base.startswith("chmd") else d["contexts"].get(f"{base}@4000"))
        for q in p.questions:
            z = zs[q["id"]] if zs is not None else d["contexts"][m]
            j = P.judge(q, ans.get(q["id"]))
            rows.append({"persona": p.id, "qid": q["id"], "type": q["type"], "split": q["split"], "method": m,
                         "reader": reader, "answer": ans.get(q["id"]), "correct": j["correct"], "trap": j["trap"],
                         "abstained": j.get("abstained", False), "missing": j.get("missing", False),
                         "in_ctx": P.contained(q, z), "in_inf": P.contained(q, inf) if inf is not None else None,
                         "ctx_chars": len(z)})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--extractor", default="claude:haiku")
    ap.add_argument("--readers", default="claude:haiku")
    ap.add_argument("--personas", default="")
    ap.add_argument("--methods", default="")
    ap.add_argument("--stage", default="all", choices=["all", "contexts", "read"])
    ap.add_argument("--rolling", action="store_true")
    ap.add_argument("--workers", type=int, default=3, help="personas in parallel")
    a = ap.parse_args(argv)
    out = a.data / "results" / P._tag(a.extractor)
    out.mkdir(parents=True, exist_ok=True)
    pdirs = sorted(x for x in a.data.glob("p*") if (x / "questions.jsonl").exists())
    if a.personas:
        keep = set(a.personas.split(","))
        pdirs = [x for x in pdirs if x.name in keep]

    def ctx_job(pd):
        p = P.Persona(pd)
        d = contexts_for(p, a.extractor, out, a.rolling)
        print(f"[ctx] {p.id} l1={d['facts']['l1']} l2={d['facts']['l2_det']}/{d['facts']['l2_llm']}", flush=True)
        return p, d

    if a.stage in ("all", "contexts"):
        with ThreadPoolExecutor(a.workers) as ex:
            done = list(ex.map(ctx_job, pdirs))
        with open(out / "cost.jsonl", "w", encoding="utf-8") as fh:
            for p, d in done:
                fh.write(json.dumps({"persona": p.id, "cost": d["cost"], "facts": d["facts"]}) + "\n")
    if a.stage in ("all", "read"):
        for reader in a.readers.split(","):
            def job(pd):
                p = P.Persona(pd)
                d = json.loads((out / "contexts" / f"{p.id}.json").read_text(encoding="utf-8"))
                methods = a.methods.split(",") if a.methods else (
                    [k for k in d["contexts"] if not k.endswith("@inf")] + list(d["bm25"]))
                rows = read_persona(p, d, reader, methods)
                acc = sum(r["correct"] for r in rows) / max(1, len(rows))
                print(f"[read] {reader} {p.id} rows={len(rows)} acc={acc:.3f}", flush=True)
                return rows
            with ThreadPoolExecutor(a.workers) as ex:
                allrows = [r for rs in ex.map(job, pdirs) for r in rs]
            f = out / f"rows-{P._tag(reader)}.jsonl"
            prev = []
            if f.exists():
                seen = {(r["persona"], r["method"]) for r in allrows}
                prev = [r for r in map(json.loads, open(f, encoding="utf-8")) if (r["persona"], r["method"]) not in seen]
            with open(f, "w", encoding="utf-8") as fh:
                for r in prev + allrows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
