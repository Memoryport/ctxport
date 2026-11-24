"""Database layout versioning (PRAGMA user_version) and migrations."""
import sqlite3

import pytest

from ctxport import cli, store
from ctxport.models import MemoryItem
from ctxport.store import NewerDatabase, Store


def test_new_database_gets_current_version(tmp_path):
    s = Store(tmp_path / "x.db")
    assert s.schema_version() == store.SCHEMA_VERSION
    s.close()


def test_unversioned_database_from_0_1_0_is_stamped_and_kept(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    s.upsert_memory(MemoryItem(text="Prefers tea", platform="chatgpt"))
    s.commit()
    s.db.execute("PRAGMA user_version = 0")  # what 0.1.0 left behind
    s.close()
    s = Store(db)
    assert s.schema_version() == store.SCHEMA_VERSION  # stamped as 1, then migrated
    assert [m.text for m in s.memories(status=None)] == ["Prefers tea"]
    s.close()


def test_migrations_run_in_order_and_keep_data(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    s = Store(db)
    s.upsert_memory(MemoryItem(text="Prefers tea", platform="chatgpt"))
    s.commit()
    s.close()
    base = store.SCHEMA_VERSION
    monkeypatch.setattr(store, "SCHEMA_VERSION", base + 2)
    monkeypatch.setattr(store, "MIGRATIONS", {
        **store.MIGRATIONS,
        base + 1: "ALTER TABLE memories ADD COLUMN pinned INTEGER DEFAULT 0;\nCREATE INDEX mem_pinned ON memories(pinned);",
        base + 2: "UPDATE memories SET pinned = 1;",
    })
    s = Store(db)
    assert s.schema_version() == base + 2
    assert s.db.execute("SELECT text, pinned FROM memories").fetchall()[0][:] == ("Prefers tea", 1)
    s.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    Store(db).close()
    base = store.SCHEMA_VERSION
    monkeypatch.setattr(store, "SCHEMA_VERSION", base + 1)
    monkeypatch.setattr(store, "MIGRATIONS", {**store.MIGRATIONS,
                                              base + 1: "ALTER TABLE memories ADD COLUMN a INTEGER;\nTHIS IS NOT SQL;"})
    with pytest.raises(sqlite3.OperationalError):
        Store(db)
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == base
    assert "a" not in [r[1] for r in con.execute("PRAGMA table_info(memories)")]
    con.close()


def test_newer_database_is_refused_with_a_clear_message(tmp_path, capsys):
    db = tmp_path / "x.db"
    Store(db).close()
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(NewerDatabase):
        Store(db)
    assert cli.main(["--db", str(db), "stats"]) == 2
    assert "newer ctxport" in capsys.readouterr().err


def test_v4_database_gets_bigram_sizes_and_still_merges(tmp_path):
    # a version-4 memories table: no sim_n column
    db = tmp_path / "v4.db"
    con = sqlite3.connect(db)
    con.executescript(store.SCHEMA.replace(", sim_n INTEGER", ""))
    con.execute("INSERT INTO memories(id, text, platform, category, status, scope) VALUES('a', 'I use VS Code',"
                " 'chatgpt', 'tooling', 'candidate', 'global')")
    con.execute("PRAGMA user_version = 4")
    con.commit()
    con.close()
    s = Store(db)
    assert s.schema_version() == store.SCHEMA_VERSION
    assert s.db.execute("SELECT sim_n FROM memories").fetchone()[0] > 0
    cand = MemoryItem(text="i use vscode.", platform="chatgpt", category="tooling", source_ref="c2")
    assert s.upsert_memory(cand) == "merged"
    s.close()


def test_size_filter_finds_the_same_duplicates_as_a_full_scan(tmp_path):
    from ctxport.models import similarity
    texts = ["I use VS Code", "I use VSCode daily", "我平时用 VS Code 写代码", "用户平时用VS Code写代码", "I like tea",
             "I like green tea a lot", "x", "我喜欢猫", "我喜欢狗", "I write Python every day at work"]
    s = Store(tmp_path / "x.db")
    for i, t in enumerate(texts):
        s.db.execute("INSERT INTO memories(id, text, category, status, scope, sim_n) VALUES(?,?,?,?,?,NULL)",
                     (f"m{i}", t, "tooling", "accepted", "global"))
    for t in texts + ["i use vs code", "我平时用VSCode写代码", "I like tea!"]:
        m = MemoryItem(text=t, category="tooling", status="candidate")
        got = s.find_near_duplicate(m)
        full = max(((similarity(t, r[1]), r[0]) for r in s.db.execute("SELECT id, text FROM memories WHERE id!=?",
                                                                       (m.id,))), default=(0, None))
        assert (got["id"] if got else None) == (full[1] if full[0] >= store.NEAR_DUPLICATE else None), t
    s.close()
