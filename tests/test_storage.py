"""Smaller stores: trimmed tool output, dialogue-only search index, compact, size report, coverage."""
from __future__ import annotations

import sqlite3

from ctxport import store as store_mod
from ctxport.cli import main
from ctxport.extract import coverage_report, run_extract
from ctxport.models import Conversation, Message
from ctxport.store import TOOL_KEEP, Store, trim_tool_text

LOG = "".join(f"line {i} processed batch {i * 37} in {i % 500}ms\n" for i in range(3000))


def _conv(n: int, tool_text: str = LOG) -> Conversation:
    return Conversation(platform="codex", native_id=str(n), title=f"t{n}", messages=[
        Message(role="user", text="我是后端工程师，主要用 Go 写服务"),
        Message(role="assistant", kind="tool_call", text='{"cmd": "pytest"}'),
        Message(role="tool", kind="tool_result", text=tool_text),
        Message(role="assistant", text="The vector index rebuild finished without errors."),
    ])


def test_trim_tool_text_keeps_head_and_tail():
    assert trim_tool_text("short") == "short"
    t = trim_tool_text(LOG)
    assert t.startswith(LOG[:TOOL_KEEP]) and t.endswith(LOG[-TOOL_KEEP:]) and "characters omitted" in t
    assert len(t) < 2 * TOOL_KEEP + 100


def test_tool_output_is_trimmed_unless_asked(tmp_path, monkeypatch):
    s = Store(tmp_path / "a.db")
    s.upsert_conversation(_conv(1))
    s.commit()
    (text,) = s.db.execute("SELECT text FROM messages WHERE kind='tool_result'").fetchone()
    assert "characters omitted" in text
    s.close()
    monkeypatch.setenv("CTXPORT_KEEP_FULL_TOOLS", "1")
    s = Store(tmp_path / "b.db")
    s.upsert_conversation(_conv(1))
    s.commit()
    assert s.db.execute("SELECT text FROM messages WHERE kind='tool_result'").fetchone()[0] == LOG
    s.close()


def test_index_holds_dialogue_only_and_tool_search_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("CTXPORT_KEEP_FULL_TOOLS", "1")
    s = Store(tmp_path / "a.db")
    s.upsert_conversation(_conv(1))
    s.commit()
    n_idx = s.db.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0]
    assert n_idx == 2  # the user message and the assistant reply, not the tool call or result
    assert s.search("vector index")
    assert not s.search("processed batch")
    assert s.search("processed batch", include_tools=True)
    s.close()


def test_migration_from_v1_rebuilds_index_without_tools(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    monkeypatch.setenv("CTXPORT_KEEP_FULL_TOOLS", "1")
    s = Store(db)
    s.upsert_conversation(_conv(1))
    s.commit()
    s.close()
    # make it look like a version-1 database: old triggers that index everything, everything indexed
    con = sqlite3.connect(db)
    for t in ("messages_ai", "messages_ad", "messages_au_del", "messages_au_ins"):
        con.execute(f"DROP TRIGGER {t}")
    con.execute("CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN "
                "INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text); END")
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == 4
    con.close()
    s = Store(db)
    assert s.schema_version() == store_mod.SCHEMA_VERSION
    assert s.db.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == 2
    names = {r[0] for r in s.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert names == {"messages_ai", "messages_ad", "messages_au_del", "messages_au_ins"}
    s.upsert_conversation(_conv(1))  # re-import replaces messages through the new triggers without errors
    s.commit()
    assert s.search("vector index")
    s.close()


def test_compact_trims_old_tool_output_and_shrinks_the_file(tmp_path, monkeypatch, capsys):
    db = tmp_path / "big.db"
    monkeypatch.setenv("CTXPORT_KEEP_FULL_TOOLS", "1")
    s = Store(db)
    for i in range(40):
        s.upsert_conversation(_conv(i))
    s.commit()
    rep = s.size_report()
    assert rep["trimmable_tool_messages"] == 40 and rep["text_bytes"]["codex"]["tools"] > 40 * len(LOG)
    s.close()
    monkeypatch.delenv("CTXPORT_KEEP_FULL_TOOLS")
    before = db.stat().st_size
    assert main(["--db", str(db), "compact"]) == 0
    assert "40 tool outputs trimmed" in capsys.readouterr().out
    assert db.stat().st_size < before / 3
    assert main(["--db", str(db), "stats", "--size"]) == 0
    out = capsys.readouterr().out
    assert "codex" in out and "dialogue" in out


def test_coverage_report_finds_empty_categories_and_silent_conversations(tmp_path, capsys):
    s = Store(tmp_path / "c.db")
    s.upsert_conversation(_conv(1))  # one self-description → a work/identity candidate
    s.upsert_conversation(Conversation(platform="chatgpt", native_id="q", title="q", messages=[
        Message(role="user", text=f"How do I sort a list number {i}?") for i in range(6)]))
    s.commit()
    run_extract(s)
    cov = coverage_report(s, min_user_messages=1)
    assert cov["silent_conversations"]["chatgpt"] == 1 and cov["silent_conversations"]["codex"] == 0
    assert "preference" in cov["empty_categories"]
    s.close()
    assert main(["--db", str(tmp_path / "c.db"), "memories", "coverage"]) == 0
    assert "empty" in capsys.readouterr().out


def _chat(texts) -> Conversation:
    return Conversation(platform="chatgpt", native_id="inc", title="t", messages=[
        Message(role="user" if i % 2 == 0 else "assistant", text=t, id=i, created_at=1000.0 + i)
        for i, t in enumerate(texts)])


def test_resync_rewrites_only_changed_messages(tmp_path):
    s = Store(tmp_path / "a.db")
    s.upsert_conversation(_chat(["我在做向量数据库", "好的，向量索引可以用 HNSW", "继续"]))
    s.commit()
    before = dict(s.db.execute("SELECT idx, id FROM messages").fetchall())
    # one more exchange, and the last message edited: the first two rows stay as they are
    s.upsert_conversation(_chat(["我在做向量数据库", "好的，向量索引可以用 HNSW", "再详细一点", "量化可以减小内存"]))
    s.commit()
    after = dict(s.db.execute("SELECT idx, id FROM messages").fetchall())
    assert after[0] == before[0] and after[1] == before[1]
    assert [m.text for m in s.get_conversation("chatgpt:inc").messages] == [
        "我在做向量数据库", "好的，向量索引可以用 HNSW", "再详细一点", "量化可以减小内存"]
    assert s.search("量化可以") and s.search("再详细") and not s.search("继续")
    # a shorter copy (forced) drops the extra rows
    s.upsert_conversation(_chat(["我在做向量数据库"]), force=True)
    s.commit()
    assert s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert not s.search("向量索引")
    s.db.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
    assert s.db.execute("SELECT message_count FROM conversations").fetchone()[0] == 1
    s.close()


def test_resync_unchanged_keeps_rows_and_extraction_state(tmp_path):
    s = Store(tmp_path / "a.db")
    c = _chat(["I am a backend engineer", "ok"])
    s.upsert_conversation(c)
    s.mark_extracted("chatgpt:inc", "rules")
    s.commit()
    ids = s.db.execute("SELECT id FROM messages ORDER BY id").fetchall()
    s.upsert_conversation(_chat(["I am a backend engineer", "ok"]))
    s.commit()
    assert s.db.execute("SELECT id FROM messages ORDER BY id").fetchall() == ids
    assert s.is_extracted("chatgpt:inc", "rules")
    s.close()
