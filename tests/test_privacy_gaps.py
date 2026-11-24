"""Privacy gaps found end to end: contact handles and plates kept out of memories, more masking, 0600 exports,
italic sub-headings in Claude memory, and compaction of the extension inbox."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from ctxport import pipeline
from ctxport.adapters._instruction import parse_instruction_text
from ctxport.cli import main
from ctxport.extract import rule_candidates
from ctxport.models import Conversation, Message
from ctxport.redact import MASK, is_sensitive, redact
from ctxport.store import Store


def test_contact_handles_and_plates_never_become_memories():
    for t in ("我的邮箱是 zhang.wei@example.cn", "My email is dana@example.com", "我的微信号是 wxid_abc123def456",
              "我的车牌是京A12345", "我的 QQ号是 123456789"):
        assert is_sensitive(t), t
        assert rule_candidates(t + "。") == [], t
    for t in ("我用 Go 写服务", "我在京东工作", "I clone with git@github.com:org/repo", "React 18 和 Vue 3"):
        assert not is_sensitive(t), t


@pytest.mark.parametrize("text,masked", [
    ("用户名 dana，密码 hunter22", "用户名 dana，密码 " + MASK),
    ("护照号是 E12345678", "护照号是 " + MASK),
    ("passport no. EA1234567", "passport no. " + MASK),
    ("我住在北京市海淀区中关村大街27号", "我住在" + MASK),
    ("地址：长安路 1-3 号 5 栋 201 室", "地址：" + MASK),
    ("我家在建国路88号附近", "我家在" + MASK + "附近"),
])
def test_more_masking(text, masked):
    assert redact(text) == masked


@pytest.mark.parametrize("text", ["密码 不对吧", "我的密码是错的", "我在长安街散步", "中山路小学", "第3号选手",
                                  "我住在杭州", "version 1.2.3"])
def test_masking_leaves_ordinary_text(text):
    assert redact(text) == text


def test_italic_sub_headings_are_not_memory_text():
    t = ("**Brief history**\n*Earlier context*\nIn 2025 the user worked on a Rust CLI.\n\n_Recent months_\nUses Go.\n\n"
         "*The user likes tea and coffee very much, always.*\n* a bullet\n")
    assert [m.text for m in parse_instruction_text(t, "claude-ai")] == [
        "In 2025 the user worked on a Rust CLI.", "Uses Go.", "*The user likes tea and coffee very much, always.*",
        "a bullet"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_exports_are_private(tmp_path):
    db = tmp_path / "p.db"
    s = Store(db)
    s.upsert_conversation(Conversation(platform="chatgpt", native_id="1", title="t",
                                       messages=[Message(role="user", text="hello there")]))
    s.commit()
    s.close()
    out = tmp_path / "out"
    assert main(["--db", str(db), "export", "markdown,memory-md", "--out", str(out)]) == 0
    files = [p for p in out.rglob("*") if p.is_file()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)


def _conv(cid: str) -> dict:
    return {"title": "T" + cid, "conversation_id": cid, "create_time": 1.7e9, "update_time": 1.7e9, "current_node": "u",
            "mapping": {"root": {"id": "root", "parent": None, "children": ["u"], "message": None},
                        "u": {"id": "u", "parent": "root", "children": [], "message": {
                            "id": "u", "author": {"role": "user"}, "create_time": 1.7e9,
                            "content": {"content_type": "text", "parts": ["hi " + cid]}}}}}


def test_compact_keeps_import_records_so_the_next_sync_reads_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CTXPORT_HOME", str(home))
    inbox = home / "inbox" / "chatgpt"
    inbox.mkdir(parents=True)
    for i in range(3):
        (inbox / f"c{i}.json").write_text(json.dumps(_conv(f"c{i}")), encoding="utf-8")
    (inbox / "bad.json").write_text("{broken", encoding="utf-8")
    s = Store(home / "ctxport.db")
    (r,) = pipeline.sync(s, only=["chatgpt"])
    assert r.conversations == 3 and len(r.warnings) == 1
    assert not r.warnings[0].startswith("bad.json: bad.json:")
    s.close()
    assert main(["compact"]) == 0
    assert sorted(p.name for p in inbox.iterdir() if not p.name.startswith("_")) == [
        "bad.json.gz", "c0.json.gz", "c1.json.gz", "c2.json.gz"]
    s = Store(home / "ctxport.db")
    (r,) = pipeline.sync(s, only=["chatgpt"])
    assert (r.units, r.skipped, r.conversations) == (4, 4, 0)  # nothing is read again
    keys = [k for (k,) in s.db.execute("SELECT key FROM sources")]
    assert len(keys) == 4 and all(k.endswith(".json.gz") for k in keys)
    assert s.get_conversation("chatgpt:c0").source_path.endswith("c0.json.gz")
    s.close()
    if os.name != "nt":
        assert stat.S_IMODE((inbox / "c0.json.gz").stat().st_mode) == 0o600
