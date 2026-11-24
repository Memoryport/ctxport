"""Near-duplicate merging, mention counts and dates, outdated hints, and the profile's per-category budget."""
from __future__ import annotations

import time

from ctxport import profile
from ctxport.cli import main
from ctxport.extract import apply_review, parse_review, possibly_outdated, render_review, run_extract
from ctxport.models import Conversation, MemoryItem, Message, similarity
from ctxport.store import Store

DAY = 86400.0


def cand(text, cat="tooling", ref=None, **kw):
    return MemoryItem(text=text, platform="chatgpt", category=cat, source_ref=ref, confidence=0.4,
                      status="candidate", **kw)


def test_similarity_sees_through_subject_width_and_spacing():
    assert similarity("I use VS Code.", "i use vscode") >= 0.8
    assert similarity("我用 VSCode", "我用ＶＳＣｏｄｅ") == 1.0
    assert similarity("用户使用 VS Code 写代码", "我使用VS Code写代码") >= 0.8
    assert similarity("I use Python", "I use Rust") < 0.5
    assert similarity("我喜欢猫", "我喜欢狗") < 0.8


def test_near_duplicates_merge_and_count_each_conversation_once(tmp_path):
    s = Store(tmp_path / "m.db")
    assert s.upsert_memory(cand("I use VS Code", ref="c1"), seen_at=100 * DAY) == "new"
    assert s.upsert_memory(cand("I use VSCode.", ref="c2"), seen_at=300 * DAY) == "merged"
    assert s.upsert_memory(cand("i use vs code", ref="c2"), seen_at=300 * DAY) == "kept"  # same conversation again
    assert s.upsert_memory(cand("I use VS Code", ref="c3"), seen_at=200 * DAY) == "seen"  # exact text, new source
    (m,) = s.memories(status=None)
    assert m.text == "I use VS Code" and m.seen_count == 3
    assert m.first_seen == 100 * DAY and m.last_seen == 300 * DAY
    assert s.memory_sources(m.id) == ["c1", "c2", "c3"]
    s.close()


def test_rejected_memories_stay_rejected_when_rephrased(tmp_path):
    s = Store(tmp_path / "m.db")
    s.upsert_memory(cand("I mostly use Vim"))
    s.set_memory_status(s.memories(status=None)[0].id, "rejected")
    assert s.upsert_memory(cand("I mostly use vim.")) == "kept"
    assert [m.status for m in s.memories(status=None)] == ["rejected"]
    # things you write yourself are never swallowed by a similar rejected memory
    assert s.upsert_memory(MemoryItem(text="I mostly use Vim now", status="accepted", category="tooling")) == "new"
    s.close()


def test_review_rewrite_keeps_counts_and_survives_rejected_original(tmp_path):
    s = Store(tmp_path / "m.db")
    s.upsert_memory(cand("I use VS Code", ref="c1"))
    s.upsert_memory(cand("I use VSCode", ref="c2"))
    s.commit()
    review = render_review(s.memories(status=None))
    assert "seen in 2 conversations" in review
    entries = parse_review(review.replace("[ ] I use VS Code", "[x] I use VS Code and Cursor"))
    assert entries and entries[0]["text"] == "I use VS Code and Cursor"
    apply_review(s, entries)
    (m,) = s.memories()
    assert m.text == "I use VS Code and Cursor" and m.seen_count == 2
    s.close()


def test_ranking_and_outdated_hint(tmp_path):
    s = Store(tmp_path / "m.db")
    for ref in ("a", "b", "c"):
        s.upsert_memory(cand("I write Python every day", ref=ref), seen_at=400 * DAY)
    s.upsert_memory(cand("I write code in the VS Code editor", ref="d"), seen_at=10 * DAY)
    s.upsert_memory(cand("I write code in the Cursor editor", ref="e"), seen_at=400 * DAY)
    s.commit()
    mems = s.memories(status=None)
    assert mems[0].text == "I write Python every day"  # seen most often comes first
    old = next(m for m in mems if "VS Code" in m.text)
    hints = possibly_outdated(mems)
    assert hints.get(old.id, "").startswith("I write code in the Cursor")
    assert "maybe outdated" in render_review(mems)
    s.close()


def test_extract_records_when_facts_were_said(tmp_path):
    s = Store(tmp_path / "m.db")
    for i, when in enumerate((1_600_000_000, 1_700_000_000)):
        s.upsert_conversation(Conversation(platform="chatgpt", native_id=str(i), title="x", updated_at=when,
                                           messages=[Message(role="user", text="I prefer short answers with code.")]))
    s.commit()
    run_extract(s)
    (m,) = s.memories(status=None)
    assert m.seen_count == 2 and m.first_seen == 1_600_000_000 and m.last_seen == 1_700_000_000
    s.close()


def test_dedupe_command_merges_existing_duplicates(tmp_path, capsys):
    db = tmp_path / "m.db"
    s = Store(db)
    for text in ("I use VS Code", "I use VSCode", "I use VS-Code"):  # inserted as accepted: exact match only
        s.upsert_memory(MemoryItem(text=text, category="tooling", status="accepted"))
    s.upsert_memory(MemoryItem(text="I use Rust", category="tooling", status="accepted"))
    s.commit()
    s.close()
    assert main(["--db", str(db), "memories", "dedupe"]) == 0
    assert "2 near-duplicate" in capsys.readouterr().out
    s = Store(db)
    mems = s.memories()
    assert len(mems) == 2 and max(m.seen_count for m in mems) == 3
    s.close()


def test_profile_budget_keeps_every_category(tmp_path):
    s = Store(tmp_path / "m.db")
    for i in range(40):
        s.upsert_memory(MemoryItem(text=f"Identity fact number {i} about me, fairly long to use space", category="identity",
                                   status="accepted"))
    s.upsert_memory(MemoryItem(text="I prefer short answers", category="preference", status="accepted"))
    s.upsert_memory(MemoryItem(text="I use Neovim", category="tooling", status="accepted"))
    s.upsert_conversation(Conversation(platform="chatgpt", native_id="t", title="Tuning Postgres", updated_at=time.time(),
                                       messages=[Message(role="user", text="hi")]))
    s.commit()
    text, st = profile.build_profile(s, max_chars=1000)
    assert len(text) <= 1000
    assert "short answers" in text and "Neovim" in text and "Tuning Postgres" in text  # nothing dropped wholesale
    assert st["omitted"] > 0 and "more not shown" in text
    s.close()
