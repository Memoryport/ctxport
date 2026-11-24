from __future__ import annotations

import json
from pathlib import Path

import fixtures as fx
import pytest

from ctxport import pipeline
from ctxport.adapters import exporters, get_importer
from ctxport.adapters.base import ExportOptions
from ctxport.cli import main, run_export
from ctxport.models import Conversation, MemoryItem, Message
from ctxport.store import Store
from ctxport.utils import BLOCK_BEGIN


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_incremental_sync_skips_unchanged(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fx.claude_code_home(tmp_path)))
    monkeypatch.setenv("CODEX_HOME", str(fx.codex_home(tmp_path)))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(fx.gemini_home(tmp_path)))
    monkeypatch.setenv("CHERRY_STUDIO_HOME", str(fx.cherry_home(tmp_path)))
    first = pipeline.sync(store)
    assert {r.platform for r in first} >= {"claude-code", "codex", "gemini-cli", "cherry-studio"}
    assert all(r.imported == r.units and r.units > 0 for r in first)
    n_conv = store.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    n_msg = store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    second = pipeline.sync(store)
    assert all(r.skipped == r.units and r.imported == 0 for r in second)
    third = pipeline.sync(store, force=True)  # re-import replaces, never duplicates
    assert all(r.imported == r.units for r in third)
    assert store.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == n_conv
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == n_msg


def test_bad_file_does_not_stop_sync(tmp_path, store, monkeypatch):
    root = fx.claude_code_home(tmp_path)
    (root / "projects" / "-tmp-proj" / "broken.jsonl").write_bytes(b"\xff\xfe{not json\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    (r,) = pipeline.sync(store, only=["claude-code"])
    assert r.conversations >= 2


def test_branches_and_raw_roundtrip(tmp_path):
    s = Store(tmp_path / "raw.db", store_raw=True)
    imp = get_importer("chatgpt")
    pipeline.import_path(s, "chatgpt", fx.chatgpt_export(tmp_path))
    c = s.get_conversation("chatgpt:cg-1", include_raw=True)
    assert len(c.branches) == 1 and c.messages[0].raw
    s.close()
    assert imp.name == "chatgpt"


def test_search_fts_and_like(store):
    conv = Conversation(platform="test", native_id="1", title="中文测试")
    conv.messages = [Message(role="user", text="我想学习机器学习和深度学习"),
                     Message(role="assistant", text="Machine learning basics"),
                     Message(role="assistant", text="工具输出 机器学习", kind="tool_result")]
    store.upsert_conversation(conv)
    store.commit()
    assert store.fts
    r3 = store.search("机器学习")      # ≥3 chars → FTS5 trigram
    assert [x["text"] for x in r3] == ["我想学习机器学习和深度学习"]  # tool results filtered by default
    r2 = store.search("学习")          # 2 chars → LIKE
    assert len(r2) == 1
    assert len(store.search("机器学习", include_tools=True)) == 2
    assert store.search("learning")[0]["role"] == "assistant"


def test_memory_upsert_rules(store):
    m = MemoryItem(text="Likes tea", status="candidate")
    assert store.upsert_memory(m) == "new"
    assert store.upsert_memory(MemoryItem(text="likes tea.", status="accepted")) == "updated"  # normalized same id
    store.set_memory_status(m.id, "rejected")
    assert store.upsert_memory(MemoryItem(text="Likes tea", status="accepted")) == "kept"
    assert store.memories(status=("rejected",))[0].id == m.id


def _seed(store, tmp_path):
    pipeline.import_path(store, "chatgpt", fx.chatgpt_export(tmp_path))
    pipeline.import_path(store, "chatbox", fx.chatbox_v1(tmp_path))
    store.upsert_memory(MemoryItem(text="My token is sk-abcdefghijklmnopqrstuvwx123", status="accepted"))
    store.commit()


def test_every_exporter_produces_files(tmp_path, store):
    _seed(store, tmp_path)
    out = tmp_path / "out"
    for ex in exporters():
        written = run_export(store, ex.name, ExportOptions(out_dir=out))
        assert written, ex.name
        for w in written:
            assert Path(w).exists()
    md = (out / "markdown" / "INDEX.md").read_text(encoding="utf-8")
    assert "Induction" in md
    conv_md = next((out / "markdown" / "chatgpt").glob("*.md")).read_text(encoding="utf-8")
    assert "Branch 1" in conv_md and "Abandoned follow-up" in conv_md
    # redaction applies to every export
    assert "sk-abcdefghijklmnop" not in (out / "claude-ai" / "claude-memory-import.txt").read_text(encoding="utf-8")
    cb = json.loads((out / "chatbox" / "chatbox-import-v1.json").read_text(encoding="utf-8"))
    assert cb["configVersion"] == 15 and cb["__exported_items"] == ["conversations"]
    assert all(s["id"].startswith("ctxport-") for s in cb["chat-sessions-list"])
    csv_text = (out / "csv" / "conversations.csv").read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0].startswith("id,platform,title")


def test_jsonl_roundtrip_is_lossless(tmp_path, store):
    _seed(store, tmp_path)
    out = tmp_path / "o"
    run_export(store, "ctxport-jsonl", ExportOptions(out_dir=out))
    s2 = Store(tmp_path / "second.db")
    pipeline.import_path(s2, "ctxport-jsonl", out / "ctxport-jsonl" / "ctxport.jsonl")
    a = store.get_conversation("chatgpt:cg-1")
    b = s2.get_conversation("chatgpt:cg-1")
    assert a.to_dict() == b.to_dict()
    assert {m.id for m in store.memories(status=None)} == {m.id for m in s2.memories(status=None)}
    s2.close()


@pytest.mark.parametrize("target,rel", [("claude-code", "CLAUDE.md"), ("codex", "AGENTS.md"),
                                        ("gemini-cli", "GEMINI.md"), ("antigravity", "AGENTS.md")])
def test_apply_managed_block_idempotent(tmp_path, store, monkeypatch, target, rel):
    homes = {"claude-code": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "gemini-cli": "GEMINI_CLI_HOME",
             "antigravity": "GEMINI_CLI_HOME"}
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(homes[target], str(home))
    target_file = home / rel
    target_file.write_text("# My own notes\n\n- keep me\n", encoding="utf-8")
    store.upsert_memory(MemoryItem(text="User studies math", status="accepted"))
    store.commit()
    for _ in range(2):
        run_export(store, target, ExportOptions(out_dir=tmp_path / "out", apply=True))
    text = target_file.read_text(encoding="utf-8")
    assert text.count(BLOCK_BEGIN) == 1 and text.count("ctxport:end") == 1
    assert text.startswith("# My own notes\n\n- keep me\n")
    assert "User studies math" in text
    assert (home / (rel + ".ctxport.bak")).read_text(encoding="utf-8") == "# My own notes\n\n- keep me\n"
    # re-import must not turn our own block into memories
    imp_name = target if target != "gemini-cli" else "gemini-cli"
    imp = get_importer(imp_name)
    from ctxport.adapters._instruction import read_instruction_file
    mems = read_instruction_file(target_file, imp.name)
    assert [m.text for m in mems] == ["keep me"]


def test_project_flag_writes_project_file(tmp_path, store):
    proj = tmp_path / "proj"
    proj.mkdir()
    store.upsert_memory(MemoryItem(text="Global fact", status="accepted"))
    store.commit()
    run_export(store, "claude-code", ExportOptions(out_dir=tmp_path / "o", apply=True, project=proj))
    assert "Global fact" in (proj / "CLAUDE.md").read_text(encoding="utf-8")
    assert not (proj / "CLAUDE.md.ctxport.bak").exists()  # nothing to back up for a new file


def test_antigravity_truncation(tmp_path, store, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    for i in range(400):
        store.upsert_memory(MemoryItem(text=f"Fact number {i} " + "x" * 60, status="accepted"))
    store.commit()
    run_export(store, "antigravity", ExportOptions(out_dir=tmp_path / "o", apply=True))
    assert len((tmp_path / "AGENTS.md").read_text(encoding="utf-8")) < 12000


def test_cli_end_to_end(tmp_path, capsys, monkeypatch):
    db = tmp_path / "cli.db"
    zipf = fx.chatgpt_export(tmp_path)
    assert main(["--db", str(db), "import", "chatgpt", str(zipf)]) == 0
    assert main(["--db", str(db), "import", "chatgpt", str(zipf)]) == 0  # unchanged → skipped
    assert "unchanged 1" in capsys.readouterr().err
    assert main(["--db", str(db), "stats"]) == 0
    assert "chatgpt" in capsys.readouterr().out
    assert main(["--db", str(db), "search", "induction"]) == 0
    assert "cg-1" in capsys.readouterr().out
    assert main(["--db", str(db), "show", "chatgpt:cg-1", "--branches"]) == 0
    assert "Branch 1" in capsys.readouterr().out
    out = tmp_path / "handoff.md"
    assert main(["--db", str(db), "handoff", "cg-1", "--out", str(out)]) == 0
    assert "You're welcome." in out.read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(db), "migrate", "chatbox", str(fx.chatbox_v1(tmp_path)), "claude-ai"]) == 0
    assert (tmp_path / "ctxport-out" / "claude-ai" / "claude-memory-import.txt").exists()
    assert main(["--db", str(db), "mcp-config", "codex"]) == 0
    assert "[mcp_servers.ctxport]" in capsys.readouterr().out
