"""Zero-touch sources: local Chromium IndexedDB apps, the ingest server and the extension inbox."""
from __future__ import annotations

import json
import os
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
from ctxport.models import MemoryItem
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

EXT = "chrome-extension://abcdefghijklmnop"


def _post(port, path, body=None, origin=EXT, token=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if data is not None else "GET",
                                 data=data,
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {}),
                                          **({"X-Ctxport-Token": token} if token else {}), **(headers or {})})
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


def _pair(port, origin=EXT):
    from ctxport import ingest
    code = ingest.new_pairing_code()
    status, r = _post(port, "/pair", {"code": code.lower()}, origin=origin)  # dash and case are forgiven
    assert status == 200 and r["token"], r
    return r["token"]


@pytest.fixture
def paired(server):
    return server, _pair(server)


def _chatgpt_api_conv(tmp_path):
    import zipfile
    with zipfile.ZipFile(fx.chatgpt_export(tmp_path)) as z:
        conv = json.loads(z.read("conversations-000.json"))[0]
    conv["_ctxport_stamp"] = "2026-01-02T00:00:00Z"
    return conv


def test_ingest_server_pairing_and_import(tmp_path, server):
    from ctxport import ingest
    port = server
    assert _post(port, "/ping")[1]["paired"] is False
    # ordinary web pages / no origin are refused
    assert _post(port, "/ingest", {"platform": "chatgpt", "conversations": []}, origin="https://evil.example")[0] == 403
    assert _post(port, "/ingest", {"platform": "chatgpt", "conversations": []}, origin=None)[0] == 403
    # the first extension to connect is NOT trusted any more: it needs the pairing code
    code, r = _post(port, "/ingest", {"platform": "chatgpt", "conversations": []})
    assert code == 403 and r.get("need_pairing")
    assert _post(port, "/known?platform=chatgpt")[0] == 403
    assert not ingest.is_paired()
    # no code issued yet -> pairing is impossible
    assert _post(port, "/pair", {"code": "AAAA-AAAA"})[0] == 403
    token = _pair(port)
    ping = _post(port, "/ping", token=token)[1]
    assert ping["paired"] and ping["authorized"]
    # the code was single-use
    assert _post(port, "/pair", {"code": "whatever"})[0] == 403
    conv = _chatgpt_api_conv(tmp_path)
    code, r = _post(port, "/ingest", {"platform": "chatgpt", "conversations": [conv],
                                      "memories": [{"content": "Prefers dark roast"}],
                                      "custom_instructions": {"about_user_message": "I teach chemistry"}}, token=token)
    assert code == 200 and r["conversations"] == 1, r
    # the paired origin without the token, a wrong token, or another extension with the token: all refused
    assert _post(port, "/known?platform=chatgpt")[0] == 403
    assert _post(port, "/known?platform=chatgpt", token="nope")[0] == 403
    assert _post(port, "/known?platform=chatgpt", origin="chrome-extension://otherextensionid", token=token)[0] == 403
    code, k = _post(port, "/known?platform=chatgpt", token=token)
    assert k["known"] == {"cg-1": "2026-01-02T00:00:00Z"}
    # Chromium sends no Origin on an extension's GET requests: the token alone must be enough there
    assert _post(port, "/ping", origin=None, token=token)[1]["authorized"]
    assert _post(port, "/known?platform=chatgpt", origin=None, token=token)[0] == 200
    assert _post(port, "/known?platform=chatgpt", origin=None, token="nope")[0] == 403
    # a web page carrying a stolen token is still refused because of its Origin
    assert _post(port, "/known?platform=chatgpt", origin="https://evil.example", token=token)[0] == 403
    s = Store(tmp_path / "ctxport_home" / "ctxport.db")
    c = s.get_conversation("chatgpt:cg-1")
    assert len(c.branches) == 1 and c.title == "Induction"
    mems = {m.text for m in s.memories(status=("accepted",))}
    assert {"Prefers dark roast", "I teach chemistry"} <= mems
    s.close()
    inbox = tmp_path / "ctxport_home" / "inbox" / "chatgpt"
    assert (inbox / "cg-1.json.gz").exists() and (inbox / "_account.json").exists()
    assert not (inbox / "cg-1.json").exists()
    # the token itself is never stored, only its hash
    assert token not in (tmp_path / "ctxport_home" / "ingest-pairing.json").read_text()


def test_pairing_code_attempts_and_expiry(server, monkeypatch):
    from ctxport import ingest
    port = server
    ingest.new_pairing_code()
    for _ in range(ingest.PAIR_CODE_ATTEMPTS):
        assert _post(port, "/pair", {"code": "WRONG-CODE"})[0] == 403
    assert not (ingest.default_home() / "ingest-pairing-code.json").exists()  # burned after 5 misses
    code = ingest.new_pairing_code()
    real_time = ingest.time.time
    monkeypatch.setattr(ingest.time, "time", lambda: real_time() + ingest.PAIR_CODE_TTL + 1)
    assert _post(port, "/pair", {"code": code})[0] == 403  # expired
    monkeypatch.setattr(ingest.time, "time", real_time)
    # a web page can't pair even with the right code
    code = ingest.new_pairing_code()
    assert _post(port, "/pair", {"code": code}, origin="https://evil.example")[0] == 403
    # re-pairing replaces the old extension
    t1 = _pair(port)
    t2 = _pair(port, origin="chrome-extension://newbrowserprofile")
    assert _post(port, "/known?platform=chatgpt", token=t1)[0] == 403
    assert _post(port, "/known?platform=chatgpt", origin="chrome-extension://newbrowserprofile", token=t2)[0] == 200
    assert ingest.reset_pairing() and not ingest.is_paired()


def test_ingest_rejects_bad_content_length(paired):
    port, token = paired
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/ingest")
    for k, v in (("Origin", EXT), ("X-Ctxport-Token", token), ("Content-Length", "abc")):
        conn.putheader(k, v)
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()
    assert _post(port, "/ingest", raw=b"[1, 2]", token=token)[0] == 400


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_private_permissions(tmp_path, paired):
    port, token = paired
    home = tmp_path / "ctxport_home"
    assert _post(port, "/ingest", {"platform": "gemini-web", "conversations": [
        {"id": "g1", "title": "t", "messages": [{"role": "user", "text": "hi"}]}]}, token=token)[0] == 200
    mode = lambda p: p.stat().st_mode & 0o777  # noqa: E731
    assert mode(home) == 0o700 and mode(home / "inbox") == 0o700 and mode(home / "inbox" / "gemini-web") == 0o700
    assert mode(home / "inbox" / "gemini-web" / "g1.json.gz") == 0o600
    assert mode(home / "ctxport.db") == 0o600
    assert mode(home / "ingest-pairing.json") == 0o600
    # an existing, too-open home is tightened when the store is opened
    home.chmod(0o755)
    Store().close()
    assert mode(home) == 0o700


def test_inbox_is_picked_up_by_sync(tmp_path, paired):
    port, token = paired
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
                                   "projects": [{"uuid": "p", "name": "Thesis", "prompt_template": "- cite sources"}]},
                 token=token)[0] == 200
    assert _post(port, "/ingest", {"platform": "gemini-web", "conversations": [gem]}, token=token)[0] == 200
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


def test_claude_orgs_keep_their_own_account_data(tmp_path, paired):
    """Two Claude.ai organisations: the second sync must not overwrite the first one's projects/memories."""
    port, token = paired
    for org, name in (("org-1", "Thesis"), ("org-2", "Work")):
        assert _post(port, "/ingest", {"platform": "claude-ai", "conversations": [], "account": org,
                                       "projects": [{"uuid": f"p-{org}", "name": name,
                                                     "prompt_template": f"- rule for {name}"}]},
                     token=token)[0] == 200
    inbox = tmp_path / "ctxport_home" / "inbox" / "claude-ai"
    assert (inbox / "_account.org-1.json").exists() and (inbox / "_account.org-2.json").exists()
    # a fresh store re-imports both organisations' projects from the inbox
    s = Store(tmp_path / "fresh.db")
    pipeline.sync(s, only=["claude-ai"])
    scopes = {m.scope for m in s.memories(status=None)}
    assert {"project:Thesis", "project:Work"} <= scopes
    s.close()
    # a hostile account id can't escape the inbox directory
    assert _post(port, "/ingest", {"platform": "claude-ai", "conversations": [], "account": "../../x",
                                   "projects": [{"uuid": "p", "name": "n", "prompt_template": "- y"}]},
                 token=token)[0] == 200
    assert all(f.parent == inbox for f in (tmp_path / "ctxport_home").rglob("_account*.json"))


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("name", ["core.test.js", "background.test.js"])
def test_extension_in_node(name):
    test = Path(__file__).parent.parent / "extension" / "test" / name
    out = subprocess.run(["node", str(test)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr


def test_extension_manifest_is_local_only():
    m = json.loads((Path(__file__).parent.parent / "extension" / "manifest.json").read_text(encoding="utf-8"))
    hosts = set(m["host_permissions"])
    assert hosts == {"https://chatgpt.com/*", "https://chat.openai.com/*", "https://claude.ai/*",
                     "https://gemini.google.com/*", "http://127.0.0.1/*"}
    assert "cookies" not in m["permissions"] and "<all_urls>" not in hosts


def test_gemini_rescrape_updates_conversation(tmp_path, paired):
    """The extension re-sends recent Gemini chats; new messages must replace the stored copy, not duplicate it."""
    port, token = paired
    msgs = [{"role": "user", "text": "What is a prime?"}, {"role": "model", "text": "Two divisors."}]
    conv = {"id": "gem1", "title": "Primes", "messages": msgs}
    assert _post(port, "/ingest", {"platform": "gemini-web", "conversations": [conv]}, token=token)[0] == 200
    conv["messages"] = msgs + [{"role": "user", "text": "Example?"}, {"role": "model", "text": "7"}]
    assert _post(port, "/ingest", {"platform": "gemini-web", "conversations": [conv]}, token=token)[0] == 200
    s = Store(tmp_path / "ctxport_home" / "ctxport.db")
    assert [m.text for m in s.get_conversation("gemini-web:gem1").messages][-1] == "7"
    assert len(s.get_conversation("gemini-web:gem1").messages) == 4
    s.close()
    assert _post(port, "/known?platform=gemini-web", token=token)[1]["known"] == {"gem1": "4"}


def test_inbox_gzip_and_legacy_plain_files(tmp_path):
    """New inbox files are gzip-compressed; plain .json from older versions still import and `compact` gzips them."""
    import gzip as _gz
    from ctxport import ingest
    from ctxport.cli import main
    home = tmp_path / "ctxport_home"
    inbox = home / "inbox" / "chatgpt"
    inbox.mkdir(parents=True)
    conv = _chatgpt_api_conv(tmp_path)
    (inbox / "cg-1.json").write_text(json.dumps(conv), encoding="utf-8")  # legacy plain file
    s = Store(home / "ctxport.db")
    (r,) = pipeline.sync(s, only=["chatgpt"])
    assert r.conversations == 1 and s.get_conversation("chatgpt:cg-1").title == "Induction"
    s.close()
    assert main(["compact"]) == 0
    assert not (inbox / "cg-1.json").exists()
    s = Store(home / "ctxport.db")
    pipeline.sync(s, only=["chatgpt"])
    keys = [r[0] for r in s.db.execute("SELECT key FROM sources WHERE platform='chatgpt'")]
    assert len(keys) == 1 and keys[0].endswith("cg-1.json.gz")  # the plain file's row is gone
    s.close()
    data = json.loads(_gz.decompress((inbox / "cg-1.json.gz").read_bytes()))
    assert data["conversation_id"] == "cg-1"
    # a fresh store re-imports the compressed copy, and only once if both copies exist
    (inbox / "cg-1.json").write_text(json.dumps(conv), encoding="utf-8")
    s = Store(tmp_path / "fresh.db")
    (r,) = pipeline.sync(s, only=["chatgpt"])
    assert r.units == 1 and r.conversations == 1
    s.close()
    assert ingest.compress_inbox()[0] == 1


def test_ingest_marks_only_imported_conversations_as_known(tmp_path, server):
    port = server
    token = _pair(port)
    bad = {"uuid": "bad-1", "chat_messages": "not a list", "updated_at": "x"}
    good = {"uuid": "ok-1", "name": "t", "updated_at": "2026-01-01T00:00:00Z",
            "chat_messages": [{"uuid": "m1", "sender": "human", "content": [{"type": "text", "text": "hi"}]}]}
    code, r = _post(port, "/ingest", {"platform": "claude-ai", "conversations": [bad, good]}, token=token)
    assert code == 200 and r["conversations"] == 1
    known = _post(port, "/known?platform=claude-ai", token=token)[1]["known"]
    assert known == {"ok-1": "2026-01-01T00:00:00Z"}  # bad-1 is sent again next time


def test_official_exports_in_downloads_are_imported(tmp_path, monkeypatch):
    import zipfile as _zf
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("CTXPORT_DOWNLOADS", str(dl))
    src = fx.chatgpt_export(tmp_path)  # conversations-000.json, user.json, memories.json
    with _zf.ZipFile(src) as z, _zf.ZipFile(dl / "4f2a-2026-11-25-chatgpt.zip", "w") as out:
        for n in z.namelist():
            out.writestr(n, z.read(n))
        out.writestr("chat.html", "<html></html>")
    csrc = fx.claude_ai_export(tmp_path)
    with _zf.ZipFile(csrc) as z, _zf.ZipFile(dl / "data-2026-11-25.zip", "w") as out:
        for n in z.namelist():
            out.writestr(n, z.read(n))
        out.writestr("users.json", "[]")
    with _zf.ZipFile(dl / "holiday-photos.zip", "w") as out:
        out.writestr("conversations.json", "[]")  # no export signature → ignored
    (dl / "broken.zip").write_bytes(b"not a zip")
    s = Store(tmp_path / "s.db")
    reps = {r.platform: r for r in pipeline.sync(s, only=["chatgpt", "claude-ai"])}
    assert reps["chatgpt"].units == 1 and s.get_conversation("chatgpt:cg-1").title == "Induction"
    assert reps["claude-ai"].units == 1 and s.get_conversation("claude-ai:ca-1") is not None
    again = {r.platform: r for r in pipeline.sync(s, only=["chatgpt", "claude-ai"])}
    assert again["chatgpt"].skipped == 1 and again["claude-ai"].skipped == 1
    s.close()


def test_forget_web_deletes_extension_data_and_stops_sync(tmp_path, server, monkeypatch):
    import zipfile as _zf
    from ctxport import ingest
    port = server
    token = _pair(port)
    db = tmp_path / "ctxport_home" / "ctxport.db"
    good = {"uuid": "ok-1", "name": "t", "updated_at": "2026-01-01T00:00:00Z",
            "chat_messages": [{"uuid": "m1", "sender": "human", "content": [{"type": "text", "text": "zebra lamp"}]}]}
    code, _ = _post(port, "/ingest", {"platform": "claude-ai", "conversations": [good],
                                      "memories": [{"content": "Rides a bike"}]}, token=token)
    assert code == 200
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("CTXPORT_DOWNLOADS", str(dl))
    with _zf.ZipFile(fx.claude_ai_export(tmp_path)) as z, _zf.ZipFile(dl / "data-2026.zip", "w") as out:
        for n in z.namelist():
            out.writestr(n, z.read(n))
        out.writestr("users.json", "[]")
    s = Store(db)
    pipeline.sync(s, only=["claude-ai"])
    s.upsert_memory(MemoryItem(text="cand from web", status="candidate", source_ref="claude-ai:ok-1"))
    s.commit()
    before = ingest.web_data(s, "claude-ai")
    assert before["conversations"] == 1 and before["files"] >= 2 and before["web_sync"]
    r = ingest.forget_web(s, "claude-ai")
    assert r["conversations"] == 1
    assert s.get_conversation("claude-ai:ok-1") is None
    assert s.get_conversation("claude-ai:ca-1") is not None          # official export kept
    assert not s.search("zebra lamp")                                  # search index cleaned
    texts = {m.text for m in s.memories(status=None)}
    assert "Rides a bike" in texts and "cand from web" not in texts    # accepted kept, its candidate gone
    assert not list((tmp_path / "ctxport_home" / "inbox" / "claude-ai").iterdir())
    assert ingest.web_data(s, "claude-ai")["web_sync"] is False
    # the extension is refused from now on, so it doesn't collect everything again
    code, body = _post(port, "/known?platform=claude-ai", token=token)
    assert code == 410 and body["web_sync_off"]
    assert _post(port, "/ingest", {"platform": "claude-ai", "conversations": [good]}, token=token)[0] == 410
    assert _post(port, "/known?platform=chatgpt", token=token)[0] == 200   # other sites unaffected
    # the next sync re-imports the official export (its import record was forgotten)
    rep = {x.platform: x for x in pipeline.sync(s, only=["claude-ai"])}
    assert rep["claude-ai"].units == 1 and rep["claude-ai"].skipped == 0
    ingest.set_web_sync("claude-ai", True)
    assert _post(port, "/known?platform=claude-ai", token=token) == (200, {"known": {}})
    s.close()
