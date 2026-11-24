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
