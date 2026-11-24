"""Zero-touch sources: local Chromium IndexedDB apps, the ingest server and the extension inbox."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import chromium_fixtures as cf
import fixtures as fx
import pytest

from ctxport import pipeline
from ctxport.chromium.indexeddb import ChromiumIndexedDB, read_local_storage
from ctxport.chromium.leveldb import read_db, snappy_decompress
from ctxport.chromium.v8 import deserialize
from ctxport.store import Store


def test_snappy_with_copies():
    # literal "abcd" then copy(offset=4, len=8) → "abcdabcdabcd"
    data = cf.varint(12) + bytes([(4 - 1) << 2]) + b"abcd" + bytes([((8 - 4) << 2) | 1 | (0 << 5)]) + bytes([4])
    assert snappy_decompress(data) == b"abcdabcdabcd"


def test_v8_roundtrip_values():
    v = {"a": 1, "neg": -5, "f": 1.5, "s": "latin", "u": "中文", "l": [1, "x", None, True], "n": {"k": False}}
    assert deserialize(cf.blink_value(v)[1:]) == v


def test_leveldb_log_and_table_newest_wins(tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    cf.write_table(d / "000005.ldb", [(b"k1", b"old"), (b"k2", b"keep")], seq=1)
    cf.write_log(d / "000006.log", [(b"k1", b"new"), (b"k3", b"x" * 70000), (b"k2", None)], seq=50)
    kv = read_db(d)
    assert kv == {b"k1": b"new", b"k3": b"x" * 70000}


def test_indexeddb_reader(tmp_path):
    d = cf.make_idb(tmp_path, "file://", "mydb", {"store": {"x": {"v": 1}, 7: ["a"]}})
    with ChromiumIndexedDB(d) as idb:
        assert idb.summary() == {"mydb": {"store": 2}}
        recs = {r.key: r.value for r in idb.records(database="mydb", store="store")}
        assert recs == {"x": {"v": 1}, 7: ["a"]}
        assert not idb.warnings
    assert d.exists()  # original untouched (reader works on a temp copy)


def test_local_storage_reader(tmp_path):
    d = cf.make_local_storage(tmp_path, "file://", {"k": "值"})
    assert read_local_storage(d) == {("file://", "k"): "值"}


def test_chatbox_local_auto_detected(tmp_path, monkeypatch):
    root = cf.chatbox_app(tmp_path)
    monkeypatch.setenv("CHATBOX_HOME", str(root))
    s = Store(tmp_path / "s.db")
    (r,) = pipeline.sync(s, only=["chatbox"])
    assert r.units == 2 and not r.warnings
    c = s.get_conversation("chatbox:loc1")
    assert c.title == "本地会话"
    assert [(m.role, m.text) for m in c.messages] == [("user", "本地问题 local question"), ("assistant", "local answer")]
    texts = {m.text: m for m in s.memories(status=None)}
    assert texts["Likes green tea"].status == "accepted"
    assert texts["You are a calm tutor"].status == "candidate"
    assert s.search("本地问题")[0]["conversation_id"] == "chatbox:loc1"
    s.close()


def test_cherry_local_auto_detected(tmp_path, monkeypatch):
    root = cf.cherry_app(tmp_path)
    monkeypatch.setenv("CHERRY_STUDIO_HOME", str(root))
    s = Store(tmp_path / "s.db")
    (r,) = pipeline.sync(s, only=["cherry-studio"])
    assert not r.warnings
    c = s.get_conversation("cherry-studio:tp1")
    assert c.title == "樱桃话题" and c.project == "默认助手"
    assert [(m.role, m.kind, m.text) for m in c.messages] == [
        ("user", "message", "樱桃问题"), ("assistant", "reasoning", "hmm"), ("assistant", "message", "樱桃回答")]
    assert c.model == "qwen-x"
    assert any(m.text == "Be precise" for m in s.memories(status=None))
    s.close()


# ---------------------------------------------------------------- ingest server

def _post(port, path, body=None, origin="chrome-extension://abcdefghijklmnop"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def server(tmp_path):
    from ctxport import ingest
    srv = ingest.make_server(0, tmp_path / "ctxport_home" / "ctxport.db", quiet=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _chatgpt_api_conv(tmp_path):
    import zipfile
    with zipfile.ZipFile(fx.chatgpt_export(tmp_path)) as z:
        conv = json.loads(z.read("conversations-000.json"))[0]
    conv["_ctxport_stamp"] = "2026-01-02T00:00:00Z"
    return conv


def test_ingest_server_pairing_and_import(tmp_path, server):
    port = server
    assert _post(port, "/ping")[1]["ok"]
    # ordinary web pages / no origin are refused
    assert _post(port, "/ingest", {"platform": "chatgpt", "conversations": []}, origin="https://evil.example")[0] == 403
    assert _post(port, "/ingest", {"platform": "chatgpt", "conversations": []}, origin=None)[0] == 403
    conv = _chatgpt_api_conv(tmp_path)
    code, r = _post(port, "/ingest", {"platform": "chatgpt", "conversations": [conv],
                                      "memories": [{"content": "Prefers dark roast"}],
                                      "custom_instructions": {"about_user_message": "I teach chemistry"}})
    assert code == 200 and r["conversations"] == 1, r
    # a second, different extension is refused after pairing
    assert _post(port, "/known?platform=chatgpt", origin="chrome-extension://otherextensionid")[0] == 403
    code, k = _post(port, "/known?platform=chatgpt")
    assert k["known"] == {"cg-1": "2026-01-02T00:00:00Z"}
    s = Store(tmp_path / "ctxport_home" / "ctxport.db")
    c = s.get_conversation("chatgpt:cg-1")
    assert len(c.branches) == 1 and c.title == "Induction"
    mems = {m.text for m in s.memories(status=("accepted",))}
    assert {"Prefers dark roast", "I teach chemistry"} <= mems
    s.close()
    inbox = tmp_path / "ctxport_home" / "inbox" / "chatgpt"
    assert (inbox / "cg-1.json").exists() and (inbox / "_account.json").exists()


def test_inbox_is_picked_up_by_sync(tmp_path, server):
    port = server
    claude_conv = {"uuid": "web-1", "name": "From the web", "created_at": "2026-01-01T00:00:00Z",
                   "updated_at": "2026-01-01T00:01:00Z", "current_leaf_message_uuid": "m2",
                   "chat_messages": [
                       {"uuid": "m1", "sender": "human", "text": "hi", "content": [{"type": "text", "text": "hi"}],
                        "parent_message_uuid": "00000000-0000-4000-8000-000000000000", "created_at": "2026-01-01T00:00:00Z"},
                       {"uuid": "m2", "sender": "assistant", "content": [{"type": "text", "text": "hello"}],
                        "parent_message_uuid": "m1", "created_at": "2026-01-01T00:00:01Z"},
                       {"uuid": "m3", "sender": "assistant", "content": [{"type": "text", "text": "retry answer"}],
                        "parent_message_uuid": "m1", "created_at": "2026-01-01T00:00:02Z"}]}
    gem = {"id": "abc123", "title": "Primes", "url": "https://gemini.google.com/app/abc123",
           "scrapedAt": "2026-03-01T00:00:00Z",
           "messages": [{"role": "user", "text": "What is a prime?"}, {"role": "model", "kind": "reasoning", "text": "think"},
                        {"role": "model", "text": "Two divisors."}]}
    assert _post(port, "/ingest", {"platform": "claude-ai", "conversations": [claude_conv],
                                   "projects": [{"uuid": "p", "name": "Thesis", "prompt_template": "- cite sources"}]})[0] == 200
    assert _post(port, "/ingest", {"platform": "gemini-web", "conversations": [gem]})[0] == 200
    # a fresh store re-imports everything from the inbox with a plain `sync`
    s = Store(tmp_path / "fresh.db")
    reps = {r.platform: r for r in pipeline.sync(s, only=["claude-ai", "gemini-web", "chatgpt"])}
    assert reps["claude-ai"].conversations == 1 and reps["gemini-web"].conversations == 1
    c = s.get_conversation("claude-ai:web-1")
    assert [m.text for m in c.messages] == ["hi", "hello"] and [m.text for b in c.branches for m in b] == ["retry answer"]
    g = s.get_conversation("gemini-web:abc123")
    assert [(m.role, m.kind) for m in g.messages] == [("user", "message"), ("assistant", "reasoning"), ("assistant", "message")]
    assert any(m.scope == "project:Thesis" for m in s.memories(status=None))
    s.close()


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_extension_core_in_node():
    test = Path(__file__).parent.parent / "extension" / "test" / "core.test.js"
    out = subprocess.run(["node", str(test)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr


def test_extension_manifest_is_local_only():
    m = json.loads((Path(__file__).parent.parent / "extension" / "manifest.json").read_text(encoding="utf-8"))
    hosts = set(m["host_permissions"])
    assert hosts == {"https://chatgpt.com/*", "https://chat.openai.com/*", "https://claude.ai/*",
                     "https://gemini.google.com/*", "http://127.0.0.1/*"}
    assert "cookies" not in m["permissions"] and "<all_urls>" not in hosts
