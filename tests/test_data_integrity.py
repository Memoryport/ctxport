"""An older copy never overwrites a newer one, secrets never become memories, and Claude's memory metadata is
not imported as memories."""
from __future__ import annotations

import json
import os
import sqlite3
import zipfile

from ctxport.adapters.claude_ai import memory_items
from ctxport.cli import main
from ctxport.extract import render_review, rule_candidates, run_extract
from ctxport.models import MemoryItem
from ctxport.pipeline import import_path
from ctxport.questionnaire import parse_answer, parse_memory_list
from ctxport.redact import is_sensitive
from ctxport import store
from ctxport.store import Store


def _gpt(cid, texts, t0):
    mapping = {"root": {"id": "root", "parent": None, "children": [], "message": None}}
    parent, ts = "root", t0
    for i, text in enumerate(texts):
        n, ts = f"{cid}-{i}", ts + 60
        mapping[n] = {"id": n, "parent": parent, "children": [], "message": {
            "id": n, "author": {"role": "user" if i % 2 == 0 else "assistant"}, "create_time": ts,
            "content": {"content_type": "text", "parts": [text]}, "metadata": {}}}
        mapping[parent]["children"].append(n)
        parent = n
    return {"title": "t", "create_time": t0, "update_time": ts, "conversation_id": cid, "current_node": parent,
            "mapping": mapping}


def test_older_export_does_not_overwrite_newer_conversation(tmp_path):
    old = _gpt("c1", ["我在用 Go", "好的"], 1_700_000_000)
    new = _gpt("c1", ["我在用 Go", "好的", "我现在改用 Rust 了", "好的"], 1_700_000_000)
    new["update_time"] += 86400 * 30
    newer, older = tmp_path / "new.json", tmp_path / "old.zip"
    newer.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    with zipfile.ZipFile(older, "w") as z:
        z.writestr("conversations.json", json.dumps([old], ensure_ascii=False))
    s = Store(tmp_path / "s.db")
    import_path(s, "chatgpt", newer)
    r = import_path(s, "chatgpt", older)
    assert r.kept_newer == 1 and r.conversations == 0
    assert any("newer" in w for w in r.warnings)
    assert len(s.get_conversation("chatgpt:c1").messages) == 4
    # --force still overwrites on purpose
    import_path(s, "chatgpt", older, force=True)
    assert len(s.get_conversation("chatgpt:c1").messages) == 2


def test_secrets_and_identifiers_never_become_memories():
    for text in ("我的 OpenAI key 是 sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789，为什么调用一直 401？",
                 "我的数据库密码是 Hunter2_pass，连接串连不上",
                 "我的身份证号是 330102199001011234，帮我看看格式对不对",
                 "My password is hunter2!, I use it everywhere",
                 "我的手机号是 13812345678"):
        assert rule_candidates(text) == [], text
    assert rule_candidates("我是一名后端工程师，目前在杭州工作。") == [("我是一名后端工程师，目前在杭州工作", "identity")]
    assert parse_answer("I use VS Code [e1]. My token is ghp_abcdefghijklmnopqrstuvwxyz0123456789 [e1].",
                        {"e1": "c"}) == [("I use VS Code.", ["c"])]
    assert parse_memory_list("## editor\n- VS Code\n- 银行卡号 6222021234567890123\n") == [("tooling", "editor", "VS Code")]
    assert not is_sensitive("I use Python 3.12 and Node 20 since 2024-01-02")


def test_old_sensitive_candidates_are_purged_and_review_is_masked(tmp_path, monkeypatch):
    s = Store(tmp_path / "s.db")
    s.upsert_memory(MemoryItem(text="我的 OpenAI key 是 sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
                               platform="chatgpt", category="identity", status="candidate"))
    s.upsert_memory(MemoryItem(text="我的 OpenAI key 是 sk-proj-ZZZZEfGhIjKlMnOpQrStUvWxYz0123456789",
                               platform="manual", category="identity", status="accepted"))
    s.commit()
    run_extract(s)
    left = s.memories(status=None)
    assert [m.status for m in left] == ["accepted"]  # you wrote that one yourself: kept, but masked when shown
    assert "sk-proj" not in render_review(left)
    s.close()
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(tmp_path / "s.db"), "memories", "edit"]) == 0
    review = tmp_path / "ctxport-memories.md"
    assert "sk-proj" not in review.read_text(encoding="utf-8")
    if os.name == "posix":
        assert review.stat().st_mode & 0o777 == 0o600
    # leaving the masked line untouched does not rewrite the memory to the masked text
    assert main(["--db", str(tmp_path / "s.db"), "memories", "edit"]) == 0
    s = Store(tmp_path / "s.db")
    assert "sk-proj-ZZZZ" in s.memories()[0].text


def test_claude_memory_metadata_is_not_imported():
    export = [{"conversations_memory": "**Work context**\n\nThe user builds ctxport.\n\n**Personal context**\n\n"
                                       "Lives in Hangzhou.",
               "project_memories": {"p-1": "Python CLI plus a Chrome extension."},
               "account_uuid": "8f2b1c3d-1111-2222-3333-444455556666"}]
    items = memory_items(export, "claude-ai", "memories.json", {"p-1": "ctxport"})
    assert {(m.text, m.scope) for m in items} == {("The user builds ctxport.", "global"),
                                                  ("Lives in Hangzhou.", "global"),
                                                  ("Python CLI plus a Chrome extension.", "project:ctxport")}
    endpoint = {"memory": "**Work**\nThe user is a designer.", "updated_at": "2025-01-02T00:00:00Z",
                "uuid": "9a0e7c1e-aaaa-bbbb-cccc-1234567890ab"}
    assert [m.text for m in memory_items(endpoint, "claude-ai", "x")] == ["The user is a designer."]
    # unknown shape: string leaves, but no ids or timestamps
    odd = {"facts": ["Prefers tea", "2025-01-02T00:00:00Z"], "id": "8f2b1c3d-1111-2222-3333-444455556666"}
    assert [m.text for m in memory_items(odd, "claude-ai", "x")] == ["Prefers tea"]


def test_upgrade_drops_metadata_memories_imported_by_older_versions(tmp_path):
    p = tmp_path / "old.db"
    Store(p).close()
    db = sqlite3.connect(p)
    for i, text in enumerate(["8f2b1c3d-1111-2222-3333-444455556666", "2025-01-02T00:00:00Z", "**Work context**",
                              "Lives in Hangzhou."]):
        db.execute("INSERT INTO memories(id, text, platform, category, status, scope) VALUES(?,?,?,?,?,?)",
                   (f"m{i}", text, "claude-ai", "platform_memory", "accepted", "global"))
    db.execute("PRAGMA user_version = 3")
    db.commit()
    db.close()
    s = Store(p)
    assert s.schema_version() == store.SCHEMA_VERSION
    assert [m.text for m in s.memories()] == ["Lives in Hangzhou."]
