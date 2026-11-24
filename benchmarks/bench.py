"""ctxport benchmarks: read (import), store (search, near-duplicates), write (extraction, profile, export).

    python benchmarks/bench.py                 # all sections, JSON on stdout
    python benchmarks/bench.py --conversations 300 --only store

Runs against whichever ctxport is importable, so the same script measures a commit before and after a change.
Everything is synthetic and deterministic (benchmarks/synth.py, benchmarks/extract_cases.py); no network.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import synth  # noqa: E402
from extract_cases import CASES  # noqa: E402

from ctxport import extract  # noqa: E402
from ctxport.store import Store  # noqa: E402


def timed(fn, *a, **kw):
    t = time.perf_counter()
    r = fn(*a, **kw)
    return time.perf_counter() - t, r


def bench_read(tmp: Path, n: int) -> dict:
    convs = synth.conversations(n)
    s = Store(tmp / "read.db")

    def load(cs):
        for c in cs:   # one transaction per conversation, as pipeline.import_unit does per file
            s.upsert_conversation(c, "bench")
            s.commit()

    fresh, _ = timed(load, convs)
    unchanged, _ = timed(load, [synth.Conversation.from_dict(c.to_dict()) for c in convs])
    appended, _ = timed(load, [synth.append_one(c) for c in convs])
    msgs = s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    s.close()
    size = sum(os.path.getsize(p) for p in tmp.glob("read.db*"))
    return {"conversations": n, "messages": msgs, "import_fresh_s": round(fresh, 3),
            "resync_unchanged_s": round(unchanged, 3), "resync_one_new_exchange_s": round(appended, 3),
            "db_bytes": size}


QUERIES = ["机器学习", "性能优化", "前端", "索引", "前端 性能优化", "缓存 数据库", "vector databases", "cache", "Go"]


def bench_search(tmp: Path) -> dict:
    s = Store(tmp / "read.db")
    out = {}
    for q in QUERIES:
        times = []
        for _ in range(7):
            t, rows = timed(s.search, q, limit=20)
            times.append(t)
        out[q] = {"median_ms": round(statistics.median(times) * 1000, 2), "hits": len(rows),
                  "ids": sorted(f"{r['conversation_id']}:{r['branch']}:{r['idx']}" for r in rows)}
    s.close()
    return out


def bench_rules() -> dict:
    tp = fp = fn = tn = cat_ok = 0
    errors = []
    for text, gold in CASES:
        got = extract.rule_candidates(text)
        cats = {c for _, c in got}
        if gold and got:
            tp += 1
            cat_ok += gold in cats
        elif gold:
            fn += 1
            errors.append(("missed", text))
        elif got:
            fp += 1
            errors.append(("false", text, got[0][0]))
        else:
            tn += 1
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    return {"cases": len(CASES), "precision": round(p, 3), "recall": round(r, 3),
            "f1": round(2 * p * r / (p + r), 3) if p + r else 0, "category_accuracy": round(cat_ok / tp, 3) if tp else 0,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn, "errors": errors}


def bench_extract(tmp: Path) -> dict:
    s = Store(tmp / "read.db")
    s.db.execute("DELETE FROM extracted")
    s.db.execute("DELETE FROM memories")
    s.commit()
    t, stats = timed(extract.run_extract, s)
    s.close()
    return {"run_extract_rules_s": round(t, 3), "new_memories": stats["new"]}


class FakeLLM:
    def __init__(self):
        self.sent = []

    def chat(self, messages, **kw):
        self.sent.append(messages[-1]["content"])
        return "[]"


def bench_llm_payload(n: int) -> dict:
    facts: list = []
    convs = synth.conversations(n, facts=facts)
    client = FakeLLM()
    calls = 0
    for c in convs:
        texts = extract.user_texts(c)
        if texts:
            extract.llm_candidates(client, texts)
            calls += 1
    sent = "\n".join(client.sent)
    kept = sum(1 for f in facts if f in sent)
    return {"llm_requests": len(client.sent), "chars_sent": len(sent), "facts": len(facts),
            "facts_kept_in_prompt": kept}


def _mem_store(tmp: Path, name: str, n: int) -> tuple:
    s = Store(tmp / name)
    mems = synth.memories(n)
    t, res = timed(lambda: [s.upsert_memory(m, seen_at=m.last_seen) for m in mems])
    s.commit()
    return s, t, res


def bench_near_dup(tmp: Path, n: int) -> dict:
    s, t, res = _mem_store(tmp, "mem.db", n)
    kept = s.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    s.close()
    # the same candidates stored without merging (as written by an older version), then `memories dedupe`
    s2 = Store(tmp / "mem2.db")
    for m in synth.memories(n):
        s2.db.execute("INSERT OR IGNORE INTO memories(id,text,platform,category,source_ref,confidence,status,scope,"
                      "created_at,seen_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (m.id, m.text, m.platform, m.category, m.source_ref, m.confidence, m.status, m.scope,
                       m.created_at, m.seen_count))
    s2.commit()
    td, merged = timed(s2.dedupe_memories)
    left = s2.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    s2.close()
    return {"candidates": n, "upsert_all_s": round(t, 3), "stored_after_merge": kept,
            "outcomes": {k: res.count(k) for k in sorted(set(res))},
            "dedupe_s": round(td, 3), "dedupe_merged": merged, "dedupe_left": left}


def bench_write(tmp: Path, n: int) -> dict:
    from ctxport import profile
    from ctxport.adapters import get_exporter
    from ctxport.adapters.base import ExportOptions
    s, _, _ = _mem_store(tmp, "mem3.db", n)
    t, (text, info) = timed(profile.build_profile, s, include_candidates=True, max_chars=3000, topics=0)
    mems = s.memories(status=("accepted", "candidate"))
    total_seen = sum(m.seen_count or 1 for m in mems)
    cats = {m.category for m in mems}
    out = {"profile_3000_s": round(t, 3), "profile_memories": info["memories"]}
    for target in ("antigravity", "chatgpt"):
        ex = get_exporter(target)
        files = ex.render(s, mems, ExportOptions(out_dir=tmp / "out", include_candidates=True))
        body = "\n".join(f.content for f in files)
        shown = [m for m in mems if m.text in body]
        out[target] = {"files": [f.rel_path for f in files], "chars": [len(f.content) for f in files],
                       "memories_shown": len(shown), "categories_shown": len({m.category for m in shown}),
                       "categories_total": len(cats),
                       "seen_weight_shown": round(sum(m.seen_count or 1 for m in shown) / total_seen, 3)}
    s.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=600)
    ap.add_argument("--memories", type=int, default=2000)
    ap.add_argument("--only", default="read,store,write")
    a = ap.parse_args()
    only = set(a.only.split(","))
    res: dict = {}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["CTXPORT_HOME"] = str(tmp / "home")
        if only & {"read", "store", "write"}:
            res["read"] = bench_read(tmp, a.conversations)
        if "store" in only:
            res["search"] = bench_search(tmp)
            res["near_duplicates"] = bench_near_dup(tmp, a.memories)
        if "write" in only:
            res["rules"] = bench_rules()
            res["extract"] = bench_extract(tmp)
            res["llm_payload"] = bench_llm_payload(min(a.conversations, 200))
            res["write"] = bench_write(tmp, a.memories)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
