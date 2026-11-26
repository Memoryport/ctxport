"""Score answers to the memsynth question bank.

    python benchmarks/memsynth/score.py OUT_DIR answers.jsonl [--split test]
    python benchmarks/memsynth/score.py OUT_DIR --oracle        # sanity check: gold answers score 1.0

answers.jsonl has one {"id": question id, "answer": free text} per line. Matching is rule-based so it is free and
deterministic; an LLM judge can be layered on later and checked against it.

  value     correct if the answer names an accepted alias and no trap (distractor or outdated value)
  list      correct if every gold item is named and no other value from the same pool; item recall/precision too
  count     correct if the first number in the answer equals the gold count
  unknown   correct if the answer says it does not know and names no trap
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vocab as V  # noqa: E402

ABSTAIN = re.compile(r"不知道|不清楚|没有提到|没提到|未提及|没有提及|无法确定|无从得知|没有相关信息|没有这方面的信息|"
                     r"don'?t know|do not know|not mentioned|never mentioned|no information|not sure|unknown|"
                     r"can'?t tell|cannot tell|haven'?t (?:said|mentioned|told)|no record", re.I)
ZH_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九十")} | {"两": 2}
POOLS = {slot: spec["pool"] for slot, spec in V.SLOTS.items()}
POOLS["project_lang"] = V.LANGS
POOLS["city_country"] = [(c[2], c[3]) for c in V.CITIES]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip().lower()


def hit(alias: str, text: str) -> bool:
    a = norm(alias)
    if not a:
        return False
    if a.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9+#])", text) is not None
    return a in text


def any_hit(aliases, text) -> bool:
    return any(hit(a, text) for a in aliases)


def first_number(text: str):
    m = re.search(r"\d+|[零一二两三四五六七八九十]", text)
    if not m:
        return None
    return int(m.group()) if m.group().isdigit() else ZH_DIGITS[m.group()]


def judge(q: dict, answer: str) -> dict:
    t = norm(answer)
    trap = any_hit(q["traps"], t)
    kind = q.get("answer_kind", "value")
    if kind == "unknown":
        return {"correct": bool(ABSTAIN.search(t)) and not trap, "trap": trap}
    if kind == "count":
        return {"correct": first_number(t) == int(q["answer"][0]), "trap": False}
    if kind == "list":
        gold = q["answer"]
        found = [any_hit(item, t) for item in gold]
        gold_forms = {norm(a) for item in gold for a in item}
        extra = [p for p in POOLS.get(q["slot"], []) if not any(norm(a) in gold_forms for a in p) and any_hit(p, t)]
        recall = sum(found) / len(gold)
        named = sum(found) + len(extra)
        return {"correct": all(found) and not extra, "trap": trap, "recall": recall,
                "precision": sum(found) / named if named else 0.0}
    return {"correct": any_hit(q["answer"], t) and not trap, "trap": trap}


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if not n:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 3), round(c + h, 3))


def load_questions(out: Path, split: str = None) -> list:
    qs = []
    for f in sorted(Path(out).glob("p*/questions.jsonl")):
        qs += [json.loads(line) for line in open(f, encoding="utf-8")]
    return [q for q in qs if not split or q["split"] == split]


def oracle_answer(q: dict) -> str:
    kind = q.get("answer_kind", "value")
    if kind == "unknown":
        return "I don't know; you haven't mentioned it."
    if kind == "list":
        return ", ".join(item[0] for item in q["answer"])
    return q["answer"][0]


def score(questions: list, answers: dict) -> dict:
    by = defaultdict(list)
    for q in questions:
        a = answers.get(q["id"])
        r = judge(q, a) if a is not None else {"correct": False, "trap": False, "missing": True}
        by[q["type"]].append(r)
    out = {}
    for t, rs in sorted(by.items()):
        k = sum(r["correct"] for r in rs)
        row = {"n": len(rs), "accuracy": round(k / len(rs), 3), "ci95": wilson(k, len(rs)),
               "trap_rate": round(sum(r["trap"] for r in rs) / len(rs), 3),
               "missing": sum(bool(r.get("missing")) for r in rs)}
        if t == "aggregate_list":
            row["item_recall"] = round(sum(r.get("recall", 0) for r in rs) / len(rs), 3)
            row["item_precision"] = round(sum(r.get("precision", 0) for r in rs) / len(rs), 3)
        out[t] = row
    accs = [v["accuracy"] for v in out.values()]
    out["_macro"] = {"accuracy": round(sum(accs) / len(accs), 3) if accs else None, "types": len(accs)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("answers", type=Path, nargs="?")
    ap.add_argument("--split", choices=["dev", "test"])
    ap.add_argument("--oracle", action="store_true")
    a = ap.parse_args(argv)
    qs = load_questions(a.out, a.split)
    if a.oracle:
        answers = {q["id"]: oracle_answer(q) for q in qs}
    elif a.answers:
        answers = {d["id"]: d.get("answer") or "" for d in map(json.loads, open(a.answers, encoding="utf-8"))}
    else:
        ap.error("pass an answers file or --oracle")
    print(json.dumps(score(qs, answers), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
