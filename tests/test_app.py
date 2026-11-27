"""The local app (`ctxport app`) and one-click MCP connection."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from ctxport import connect
from ctxport.models import MemoryItem
from ctxport.store import Store


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("APPDATA", str(h / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.setenv("CODEX_HOME", str(h / ".codex"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(h / ".gemini"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h / ".claude"))
    return h


# ---------------------------------------------------------------- MCP connect

def test_connect_json_keeps_other_servers_and_backs_up(fake_home):
    p = connect.TARGETS["cursor"].path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}), encoding="utf-8")
    assert connect.status("cursor")["connected"] is False
    r = connect.connect("cursor")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["keep"] == 1 and "other" in data["mcpServers"]
    assert data["mcpServers"]["ctxport"]["args"][-1] == "serve-mcp"
    assert r["backup"] and json.loads(open(r["backup"], encoding="utf-8").read())["mcpServers"] == {"other": {"command": "x"}}
    assert connect.status("cursor")["connected"] is True
    connect.connect("cursor")  # idempotent
    assert list(json.loads(p.read_text(encoding="utf-8"))["mcpServers"]) == ["other", "ctxport"]
    connect.disconnect("cursor")
    assert json.loads(p.read_text(encoding="utf-8"))["mcpServers"] == {"other": {"command": "x"}}


def test_connect_creates_missing_and_empty_files(fake_home):
    p = connect.TARGETS["gemini-cli"].path()
    assert not p.exists()
    connect.connect("gemini-cli")
    assert "ctxport" in json.loads(p.read_text(encoding="utf-8"))["mcpServers"]
    a = fake_home / ".gemini" / "config" / "mcp_config.json"
    a.parent.mkdir(parents=True)
    a.write_text("", encoding="utf-8")  # observed on a real machine: an empty file
    assert connect.antigravity_config() == a
    connect.connect("antigravity")
    assert "ctxport" in json.loads(a.read_text(encoding="utf-8"))["mcpServers"]


def test_connect_refuses_to_overwrite_broken_json(fake_home):
    p = connect.claude_desktop_config()
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        connect.connect("claude-desktop")
    assert p.read_text(encoding="utf-8") == "{not json"
    assert "error" in connect.status("claude-desktop")


def test_connect_codex_toml(fake_home, monkeypatch):
    # as when ctxport is installed; a source checkout without it on PATH uses `python -m ctxport` instead
    monkeypatch.setattr(connect.shutil, "which", lambda name: "/usr/local/bin/ctxport" if name == "ctxport" else None)
    p = connect.TARGETS["codex"].path()
    p.parent.mkdir(parents=True)
    original = 'model = "gpt-x"\n\n[mcp_servers.other]\ncommand = "o"\n'
    p.write_text(original, encoding="utf-8")
    connect.connect("codex")
    text = p.read_text(encoding="utf-8")
    assert text.startswith(original) and "[mcp_servers.ctxport]" in text and 'args = ["serve-mcp"]' in text
    assert connect.status("codex")["connected"]
    connect.connect("codex")
    assert p.read_text(encoding="utf-8").count("[mcp_servers.ctxport]") == 1
    connect.disconnect("codex")
    assert p.read_text(encoding="utf-8").strip() == original.strip()


def test_connect_claude_code_needs_cli(fake_home, monkeypatch):
    monkeypatch.setattr(connect.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        connect.connect("claude-code")


# ---------------------------------------------------------------- web app

@pytest.fixture
def app(tmp_path, fake_home):
    from ctxport import webapp
    s = Store()
    s.upsert_memory(MemoryItem(text="User studies physics", status="accepted", category="identity"))
    s.upsert_memory(MemoryItem(text="Likes green tea", status="candidate", category="preference"))
    s.commit()
    s.close()
    srv = webapp.make_server(0, token="tok")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, path, body=None, token="tok", headers=None):
    h = {"Content-Type": "application/json", **({"X-Ctxport-App": token} if token else {}), **(headers or {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_app_security(app):
    code, page = call(app, "/", token=None)
    assert code == 200 and b'name="ctxport-app"' in page
    assert call(app, "/api/overview", token=None)[0] == 403
    assert call(app, "/api/overview", token="wrong")[0] == 403
    # another web page cannot drive the API, even with the token
    assert call(app, "/api/overview", headers={"Origin": "https://evil.example"})[0] == 403
    # DNS rebinding: a foreign Host header is refused
    assert call(app, "/api/overview", headers={"Host": "evil.example"})[0] == 403
    assert call(app, "/api/open-url", {"url": "https://evil.example"})[0] == 400


def test_app_memories_and_agent_import(app, fake_home):
    code, o = call(app, "/api/overview")
    assert code == 200 and o["memories"] == {"accepted": 1, "candidate": 1}
    cand = call(app, "/api/memories?status=candidate")[1]["memories"]
    assert [m["text"] for m in cand] == ["Likes green tea"]
    assert call(app, "/api/memories/update", {"id": cand[0]["id"], "status": "accepted"})[1]["updated"] == 1
    assert call(app, "/api/overview")[1]["memories"] == {"accepted": 2, "candidate": 0}

    claude_md = fake_home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# mine\n", encoding="utf-8")
    code, r = call(app, "/api/agent/apply", {"key": "claude-code"})
    assert code == 200, r
    text = claude_md.read_text(encoding="utf-8")
    assert text.startswith("# mine\n") and "Likes green tea" in text and "ctxport:begin" in text
    agents = {a["key"]: a for a in call(app, "/api/targets")[1]["agents"]}
    assert agents["claude-code"]["imported"] is True

    proj = fake_home / "proj"
    proj.mkdir()
    assert call(app, "/api/agent/apply", {"key": "cursor"})[0] == 400  # needs a project
    code, r = call(app, "/api/agent/apply", {"key": "cursor", "project": str(proj)})
    rule = (proj / ".cursor" / "rules" / "ctxport.mdc").read_text(encoding="utf-8")
    assert code == 200 and rule.startswith("---\n") and "alwaysApply: true" in rule and "User studies physics" in rule


def test_app_mcp_and_text(app, fake_home):
    code, r = call(app, "/api/mcp/connect", {"key": "cursor"})
    assert code == 200 and (fake_home / ".cursor" / "mcp.json").exists()
    mcp = {m["key"]: m for m in call(app, "/api/targets")[1]["mcp"]}
    assert mcp["cursor"]["connected"] is True
    assert call(app, "/api/mcp/connect", {"key": "nope"})[0] == 400
    code, t = call(app, "/api/text?target=chatgpt")
    assert code == 200 and "User studies physics" in t["text"] and len(t["text"]) <= 1500
    assert call(app, "/api/text?target=evil")[0] == 400


def test_receiver_memory_text_requires_pairing(tmp_path):
    from ctxport import ingest
    s = Store()
    s.upsert_memory(MemoryItem(text="Prefers concise answers", status="accepted"))
    s.commit()
    s.close()
    srv = ingest.make_server(0, quiet=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        ext = "chrome-extension://abcdefghijklmnop"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/memory-text?target=chatgpt", headers={"Origin": ext})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 403
        code = ingest.new_pairing_code()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/pair", data=json.dumps({"code": code}).encode(),
                                     headers={"Origin": ext, "Content-Type": "application/json"}, method="POST")
        token = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/memory-text?target=chatgpt",
                                     headers={"X-Ctxport-Token": token})
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert "Prefers concise answers" in d["text"] and d["accepted"] == 1
        req = urllib.request.Request(f"http://127.0.0.1:{port}/memory-text?target=../etc",
                                     headers={"X-Ctxport-Token": token})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=10)
    finally:
        srv.shutdown()
        srv.server_close()


def test_app_distill_without_api_key(app, monkeypatch):
    import time as _t
    from ctxport import webapp
    from ctxport.models import Conversation, Message
    monkeypatch.setattr(webapp, "reveal", lambda path: None)
    s = Store()
    c = Conversation(platform="test", native_id="d1", title="t")
    c.messages = [Message(role="user", text="I am a chemistry teacher"), Message(role="assistant", text="ok")]
    s.upsert_conversation(c)
    s.commit()
    s.close()
    assert "agents" in call(app, "/api/distill/options")[1]
    code, r = call(app, "/api/distill/folder", {})
    assert code == 200 and r["conversations"] == 1
    code, w = call(app, "/api/distill/web")
    assert code == 200 and w["batches"][0]["name"] == "0001.md"
    code, p = call(app, "/api/distill/web/prompt?name=0001.md")
    assert "test:d1" in p["text"]
    assert call(app, "/api/distill/web/prompt?name=../../etc/passwd")[0] == 400
    reply = json.dumps({"cards": {"test:d1": {"summary": "s", "facts": [
        {"text": "The user is a chemistry teacher", "category": "work"}]}}})
    assert call(app, "/api/distill/web/answer", {"name": "0001.md", "reply": reply})[1]["cards"] == 1
    assert call(app, "/api/distill/agent", {"agent": "claude-code"})[0] == 400  # needs confirmation
    assert call(app, "/api/distill/build", {})[0] == 200
    for _ in range(100):
        st = call(app, "/api/distill/status")[1]
        if not st["running"]:
            break
        _t.sleep(0.05)
    assert st["profile_exists"] and st["last_run"]["coverage"]["full"] == 1.0
    assert "chemistry teacher" in call(app, "/api/text?target=claude-ai")[1]["text"]


def test_app_import_downloads(app, tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    dl.mkdir()
    monkeypatch.setenv("CTXPORT_DOWNLOADS", str(dl))
    code, r = call(app, "/api/import-downloads", {})
    assert code == 200 and r["chatgpt"]["zips"] == 0 and r["claude-ai"]["zips"] == 0


def test_app_web_data(app, fake_home):
    sites = {w["platform"]: w for w in call(app, "/api/web-data")[1]["sites"]}
    assert set(sites) == {"chatgpt", "claude-ai", "gemini-web"} and sites["chatgpt"]["web_sync"]
    assert call(app, "/api/web-data/forget", {"platform": "chatgpt"})[0] == 400       # needs confirmation
    assert call(app, "/api/web-data/forget", {"platform": "evil", "confirm": True})[0] == 400
    code, r = call(app, "/api/web-data/forget", {"platform": "chatgpt", "confirm": True})
    assert code == 200 and r["conversations"] == 0 and r["web_sync"] is False
    sites = {w["platform"]: w for w in call(app, "/api/web-data/sync", {"platform": "chatgpt", "on": True})[1]["sites"]}
    assert sites["chatgpt"]["web_sync"] is True


def test_second_launch_reopens_the_running_app(tmp_path, fake_home, monkeypatch):
    from ctxport import webapp
    opened = []
    monkeypatch.setattr(webapp.webbrowser, "open", opened.append)
    t = threading.Thread(target=webapp.serve, kwargs={"port": 0, "open_browser": False}, daemon=True)
    t.start()
    for _ in range(100):
        if webapp._url_file().exists():
            break
        threading.Event().wait(0.02)
    url = webapp._url_file().read_text().strip()
    port = int(url.split(":")[2].split("/")[0])
    second = threading.Thread(target=webapp.serve, args=(port,), kwargs={"open_browser": True}, daemon=True)
    second.start()
    second.join(10)
    assert not second.is_alive(), "a second server bound the same port"   # it must reopen the first one
    assert opened == [url]
    webapp._url_file().write_text("http://127.0.0.1:1/#t=stale")
    with pytest.raises(OSError):
        webapp.make_server(port)                    # the port is taken: binding fails on every platform


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS launcher")
def test_desktop_launcher(tmp_path, fake_home):
    from ctxport import webapp
    app = webapp.make_launcher(tmp_path)
    assert (app / "Contents" / "Resources" / "ctxport-launcher").exists()
    assert webapp.make_launcher(tmp_path) == app     # our own launcher is replaced
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "ctxport.app").mkdir()
    with pytest.raises(RuntimeError):
        webapp.make_launcher(tmp_path / "other")    # someone else's app is left alone


def test_app_distill_options_preview_and_stop(app):
    code, o = call(app, "/api/distill/options")
    assert code == 200 and set(o["choices"]) == {"claude-code", "codex"}
    assert "max" in o["choices"]["claude-code"]["efforts"]
    code, p = call(app, "/api/distill/preview?limit=50")
    assert code == 200 and {"conversations", "batches", "chars", "cards", "total"} <= set(p)
    assert call(app, "/api/distill/stop", {})[0] == 200          # nothing running: harmless
    bad = call(app, "/api/distill/agent", {"agent": "claude-code", "effort": "turbo", "confirm": True})
    assert bad[0] == 400
    assert call(app, "/api/distill/profile")[1]["text"] == ""
    assert call(app, "/api/distill/reveal", {})[0] == 400        # no profile yet
