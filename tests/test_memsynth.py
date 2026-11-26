"""Invariants of the synthetic persona benchmark (benchmarks/memsynth)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "memsynth"))
import generate as G  # noqa: E402
import score as S  # noqa: E402
import vocab as V  # noqa: E402


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("memsynth")
    ps = G.generate(personas=4, conversations=60, days=300, seed=5)
    manifest = G.write(ps, out, db=True)
    return ps, out, manifest


def _messages(p):
    return {m["id"]: (c, m) for c in p.conversations for m in c["messages"] if m.get("id")}


def test_every_fact_is_stated_where_the_gold_says(built):
    ps, _, _ = built
    for p in ps:
        msgs = _messages(p)
        for f in p.facts:
            assert f["mentions"], f
            for m in f["mentions"]:
                conv, msg = msgs[m["message"]]
                surface = f["zh"] if conv["meta"]["lang"] == "zh" else f["en"]
                assert msg["role"] == "user" and surface in msg["text"], (f["slot"], msg["text"])
                if V.SLOTS.get(f["slot"], {}).get("channel") == "chat":
                    assert conv["platform"] in G.CHAT
        for d in p.distractors:
            for mid in d["messages"]:
                conv, msg = msgs[mid]
                assert (d["zh"] if conv["meta"]["lang"] == "zh" else d["en"]) in msg["text"]


def test_updates_are_in_time_order(built):
    ps, _, _ = built
    for p in ps:
        for slot, vals in p.slot_values.items():
            if V.SLOTS.get(slot, {}).get("kind") != "update" or len(vals) < 2:
                continue
            for prev, nxt in zip(vals, vals[1:]):
                upd = [m["at"] for m in nxt["mentions"] if m["kind"] == "update"]
                assert len(upd) == 1
                assert max(m["at"] for m in prev["mentions"]) <= upd[0] <= min(m["at"] for m in nxt["mentions"])


def test_questions_do_not_leak_answers_and_evidence_exists(built):
    ps, _, manifest = built
    for p in ps:
        msgs = _messages(p)
        for q in p.questions:
            assert set(q["evidence"]) <= set(msgs)
            if q["answer_kind"] in ("value", "list"):
                gold = q["answer"] if q["answer_kind"] == "value" else [a for item in q["answer"] for a in item]
                for lang in ("zh", "en"):
                    assert not any(S.hit(a, S.norm(q["question"][lang])) for a in gold), q
                assert not set(q["traps"]) & set(gold)
            if q["type"] != "abstain":
                assert q["evidence"], q
    assert all(v["all"] for v in manifest["questions"].values()), manifest["questions"]


def test_oracle_scores_perfectly_and_abstaining_only_wins_abstain(built):
    _, out, _ = built
    qs = S.load_questions(out)
    perfect = S.score(qs, {q["id"]: S.oracle_answer(q) for q in qs})
    assert all(v["accuracy"] == 1.0 for k, v in perfect.items() if k != "_macro"), perfect
    shrug = S.score(qs, {q["id"]: "我不知道。" for q in qs})
    assert shrug["abstain"]["accuracy"] == 1.0
    assert all(v["accuracy"] == 0.0 for k, v in shrug.items() if k not in ("abstain", "_macro"))


def test_trap_values_are_wrong(built):
    _, out, _ = built
    q = next(q for q in S.load_questions(out) if q["type"] == "update_current")
    assert not S.judge(q, f"{q['answer'][0]}，以前是 {q['traps'][0]}")["correct"]
    assert not S.judge(q, q["traps"][0])["correct"]


def test_deterministic_and_free_of_the_authors_profile(built, tmp_path):
    _, out, _ = built
    again = G.write(G.generate(personas=4, conversations=60, days=300, seed=5), tmp_path)
    for f in ("conversations.jsonl", "questions.jsonl", "facts.json"):
        assert (out / "p00" / f).read_bytes() == (tmp_path / "p00" / f).read_bytes()
    assert again["questions"] == json.loads((out / "manifest.json").read_text())["questions"]
    text = " ".join((out / p / "conversations.jsonl").read_text().lower() for p in ("p00", "p01", "p02", "p03"))
    for canary in ("waterloo", "滑铁卢", "toronto", "racket", "ray tian", "ctxport", "nanyang", "guodong"):
        assert canary not in text


def test_store_loads(built):
    from ctxport.store import Store
    ps, out, _ = built
    s = Store(out / "p00" / "home" / "ctxport.db")
    assert s.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == len(ps[0].conversations)
    s.close()
