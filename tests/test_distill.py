"""Coverage-guaranteed distillation, with a fake model that misbehaves on purpose."""
from __future__ import annotations

import json
import re

import pytest

from ctxport import distill as D
from ctxport.models import Conversation, MemoryItem, Message
from ctxport.store import Store

FACTS = {
    "c1": ["The user studies physics at a university in Lisbon", "The user prefers concise answers"],
    "c2": ["The user is building a todo app in React", "The user uses Neovim"],
    "c3": ["The user studies physics in Lisbon", "The user wants answers in Chinese"],  # first one duplicates c1
}


class FakeModel:
    """Cards: returns FACTS for the conversation in the prompt. Merge: drops one id on purpose. Write: omits a
    fact and invents an id on purpose. Counts calls like a real client."""
    model = "fake-model"

    def __init__(self):
        self.usage = {}
        self.calls = []

    def chat(self, messages, **kw):
        system, user = messages[0]["content"], messages[1]["content"]
        self.calls.append(system[:20])
        self.usage["calls"] = self.usage.get("calls", 0) + 1
        if system == D.CARD_PROMPT:
            for key, facts in FACTS.items():
                if f"secret-{key}" in user:
                    assert "sk-abcdefghijklmnopqrstuvwx" not in user, "secrets are masked before sending"
                    return "Here you go:\n" + json.dumps({"summary": f"conv {key}", "facts": [
                        {"text": t, "category": "study" if "physics" in t else "preference", "evidence": [1]}
                        for t in facts]})
            return '{"summary": "", "facts": []}'
        if system == D.MERGE_PROMPT:
            ids = re.findall(r"^(f\d+)", user, re.M)
            # merge the first two, drop the last id entirely
            out = [{"text": "MERGED: " + " / ".join(ids[:2]), "covers": ids[:2]}] if len(ids) >= 2 else []
            out += [{"text": f"kept {i}", "covers": [i]} for i in ids[2:-1]]
            return json.dumps({"facts": out})
        if system == D.WRITE_PROMPT:
            ids = re.findall(r"^(c\d+):", user, re.M)
            return "\n".join(f"- bullet for {i} [{i},c999]" for i in ids[1:])  # skips the first id, invents c999
        raise AssertionError(system[:40])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    for key in FACTS:
        c = Conversation(platform="test", native_id=key, title=f"t {key}", created_at=1_700_000_000 + int(key[1]))
        c.messages = [Message(role="user", text=f"hello secret-{key} my key is sk-abcdefghijklmnopqrstuvwx"),
                      Message(role="assistant", text="sure")]
        s.upsert_conversation(c)
    s.upsert_memory(MemoryItem(text="Likes green tea", status="accepted", category="preference"))
    s.commit()
    yield s
    s.close()


def test_full_coverage_even_when_the_model_drops_things(store):
    model = FakeModel()
    res = D.run(store, model)
    log = res["log"]
    assert log["cards_new"] == 3 and log["facts_l1"] == 7  # 6 card facts + 1 accepted memory
    assert log["coverage"]["full"] == 1.0
    assert log["l2_restored"] >= 1 and log["l3_appended"] >= 1  # the guarantees actually kicked in
    data = json.loads((D.out_dir() / "facts.json").read_text(encoding="utf-8"))
    covered = {x for c in data["l2"] for x in c["covers"]}
    assert covered == {f["id"] for f in data["l1"]}
    doc = (D.out_dir() / "profile.annotated.md").read_text(encoding="utf-8")
    assert "c999" not in doc  # invented ids are removed
    profile = (D.out_dir() / "profile.md").read_text(encoding="utf-8")
    assert "[c" not in profile and "c999" not in profile
    assert "bullet for" in profile and "MERGED" not in profile.split("##")[0]
    runs = list(D.out_dir().glob("run-*.json"))
    assert runs and json.loads(runs[0].read_text(encoding="utf-8"))["usage"]["calls"] == model.usage["calls"]


def test_cards_are_cached(store):
    D.run(store, FakeModel())
    m2 = FakeModel()
    log = D.run(store, m2, polish=False)["log"]
    assert log["cards_cached"] == 3 and log["cards_new"] == 0 and m2.calls == []
    # a changed conversation is re-read
    c = store.get_conversation("test:c2")
    c.messages.append(Message(role="user", text="also secret-c2 again"))
    store.upsert_conversation(c, force=True)
    store.commit()
    m3 = FakeModel()
    log = D.run(store, m3, polish=False)["log"]
    assert log["cards_new"] == 1 and log["cards_cached"] == 2
    assert D.estimate(store)["conversations"] == 0


def test_without_a_model_is_deterministic_and_complete(store):
    D.run(store, FakeModel())  # fill the card cache
    log = D.run(store, None)["log"]
    assert log["coverage"]["full"] == 1.0
    # the near-duplicate physics facts are merged without a model
    assert log["facts_l2"] < log["facts_l1"]


def test_budget_version_reports_partial_coverage(store):
    D.run(store, FakeModel(), polish=False)
    data = json.loads((D.out_dir() / "facts.json").read_text(encoding="utf-8"))
    text, cov = D.render_budget(data["l2"], [{"category": c} for c in D.CATEGORIES], 60)
    assert len(text) <= 60 and 0 < cov < 1


def test_manual_mode_roundtrip(store, tmp_path):
    folder = tmp_path / "manual"
    r = D.manual_export(store, folder)
    assert r["conversations"] == 3 and len(r["prompts"]) == 1
    prompt = open(r["prompts"][0], encoding="utf-8").read()
    assert "conversation id: test:c1" in prompt and "sk-abcdefghijklmnop" not in prompt
    (folder / "responses" / "0001.json").write_text(json.dumps({"cards": {
        "test:c1": {"summary": "s", "facts": [{"text": "The user studies physics", "category": "study"}]},
        "test:nope": {"summary": "ignored"}}}), encoding="utf-8")
    assert D.manual_import(store, folder) == 1
    assert D.manual_export(store, folder)["conversations"] == 2
    log = D.run(store, None, polish=False)["log"]
    assert log["cards_cached"] == 1 and log["coverage"]["full"] == 1.0


def test_distilled_profile_used_by_exports(store, tmp_path, monkeypatch):
    from ctxport.adapters.base import ExportOptions
    from ctxport.cli import export_and_apply
    D.run(store, FakeModel())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    r = export_and_apply(store, "claude-code", ExportOptions(out_dir=tmp_path / "o", extra={"distilled": True}))
    text = open(r["written"][0], encoding="utf-8").read()
    assert "MERGED" in text or "bullet for" in text
    r = export_and_apply(store, "claude-code", ExportOptions(out_dir=tmp_path / "o2"))
    assert "bullet for" not in open(r["written"][0], encoding="utf-8").read()


def test_llm_config_never_picks_a_model(tmp_path, monkeypatch):
    from ctxport import llm
    monkeypatch.delenv("CTXPORT_LLM_MODEL", raising=False)
    monkeypatch.delenv("CTXPORT_LLM_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        llm.client_from_config()
    llm.save_config({"preset": "deepseek", "base_url": llm.PRESETS["deepseek"]["base_url"], "model": "m-1",
                     "api_key": "k"})
    with pytest.raises(llm.RemoteNotAllowed):  # remote needs an explicit opt-in
        llm.client_from_config()
    c = llm.client_from_config(allow_remote=True)
    assert c.model == "m-1" and c.base_url.startswith("https://api.deepseek.com")
    import os
    import stat
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(llm.config_path()).st_mode) == 0o600


def _fake_agent(tmp_path, monkeypatch, drop_one=False, sleep=0.0):
    """A stand-in `claude` CLI: reads the batch files named in the -p task and writes cards/<name>.json."""
    import os
    import stat
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"""#!{sys.executable}
import json, re, sys, pathlib, time
with open("times.log", "a") as fh:
    print("start", time.time(), file=fh)
time.sleep({sleep!r})
with open("argv.log", "a") as fh:
    print(json.dumps(sys.argv[1:]), file=fh)
task = sys.argv[sys.argv.index("-p") + 1]
names = re.findall(r"([A-Za-z0-9._-]+\\.md)", task.split(":", 1)[1])
for i, n in enumerate(names):
    if {drop_one!r} and i == 0:
        continue
    text = pathlib.Path("conversations", n).read_text(encoding="utf-8")
    cid = text.splitlines()[0].split(": ", 1)[1]
    key = re.search(r"secret-(c\\d)", text).group(1)
    pathlib.Path("cards", n[:-3] + ".json").write_text(json.dumps({{"conversation_id": cid, "summary": "s",
        "facts": [{{"text": "Agent fact for " + key, "category": "general", "evidence": [1]}}]}}))
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


@pytest.mark.skipif(__import__("os").name == "nt", reason="shell-script stand-in for the claude CLI")
def test_agent_mode_runs_claude_code_in_batches(store, tmp_path, monkeypatch):
    _fake_agent(tmp_path, monkeypatch, drop_one=True)
    folder = tmp_path / "work"
    prep = D.agent_prepare(store, folder, batch=2)
    assert prep["conversations"] == 3 and len(prep["batches"]) == 2
    assert (folder / "TASK.md").exists() and "TASK.md" in (folder / "CLAUDE.md").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in "".join(p.read_text(encoding="utf-8") for p in (folder / "conversations").iterdir())
    assert "claude-code" in D.agent_commands()
    r = D.run_agent(store, folder, "claude-code")
    assert r["imported"] == 1  # the fake agent skips the first file of each batch: 3 files in batches of 2 → 1 card
    # preparing again only lists what is still missing, and the profile covers every card fact
    assert D.agent_prepare(store, folder, batch=2)["conversations"] == 2
    log = D.run(store, None, polish=False)["log"]
    assert log["cards_cached"] == 1 and log["coverage"]["full"] == 1.0


@pytest.mark.skipif(__import__("os").name == "nt", reason="shell-script stand-in for the claude CLI")
def test_agent_mode_model_effort_and_stop(store, tmp_path, monkeypatch):
    _fake_agent(tmp_path, monkeypatch)
    assert D.agent_pending(store)["conversations"] == 3
    assert D.agent_pending(store, limit=1)["conversations"] == 1
    folder = tmp_path / "work"
    D.agent_prepare(store, folder, batch=1)
    with pytest.raises(ValueError):
        D.run_agent(store, folder, "claude-code", effort="turbo")
    with pytest.raises(ValueError):
        D.run_agent(store, folder, "claude-code", model="sonnet; rm -rf ~")
    seen = []
    r = D.run_agent(store, folder, "claude-code", model="sonnet", effort="low",
                    on_batch=lambda i, n: seen.append((i, n)), should_stop=lambda: len(seen) > 1)
    assert r["stopped"] and r["batches_run"] == 1 and r["imported"] == 1
    argv = [json.loads(line) for line in (folder / "argv.log").read_text().splitlines()]
    assert len(argv) == 1 and argv[0][argv[0].index("--model") + 1] == "sonnet"
    assert argv[0][argv[0].index("--effort") + 1] == "low"
    assert D.agent_pending(store)["conversations"] == 2
    r = D.run_agent(store, folder, "claude-code")      # resumes; no flags means the agent's own defaults
    assert not r["stopped"] and r["imported"] == 3
    assert "--model" not in json.loads((folder / "argv.log").read_text().splitlines()[-1])


def test_agent_import_rejects_unknown_ids(store, tmp_path):
    folder = tmp_path / "w"
    D.agent_prepare(store, folder)
    (folder / "cards" / "x.json").write_text('{"conversation_id": "test:nope", "facts": []}', encoding="utf-8")
    (folder / "cards" / "y.json").write_text(json.dumps({"conversation_id": "test:c2", "summary": "s",
        "facts": [{"text": "The user likes chess", "category": "preference"}]}), encoding="utf-8")
    assert D.agent_import(store, folder) == {"imported": 1, "invalid": 1}


def test_web_answer_flow(store, tmp_path):
    folder = tmp_path / "web"
    batches = D.web_batches(store, folder)
    assert len(batches) == 1 and batches[0]["name"] == "0001.md"
    reply = "好的：\n```json\n" + json.dumps({"cards": {"test:c3": {"summary": "s", "facts": [
        {"text": "The user wants answers in Chinese", "category": "preference"}]}}}) + "\n```"
    assert D.web_answer(store, folder, "0001.md", reply) == 1
    with pytest.raises(ValueError):
        D.web_answer(store, folder, "../x.md", "{}")


def test_budget_lines_are_dated_newest_last():
    canon = [{"id": "c1", "text": "The user codes in Helix", "category": "tooling", "count": 1, "sources": [],
              "last": 1_700_000_000},
             {"id": "c2", "text": "The user codes in Zed", "category": "tooling", "count": 1, "sources": [],
              "last": 1_740_000_000},
             {"id": "c3", "text": "The user likes tea", "category": "preference", "count": 1, "sources": []}]
    text, cov = D.render_budget(canon, [{"category": c} for c in D.CATEGORIES], 1000)
    lines = text.splitlines()
    assert cov == 1.0 and "- The user likes tea" in lines  # undated facts stay as they were
    helix, zed = lines.index(f"- [{D._d(1_700_000_000)}] The user codes in Helix"), \
        lines.index(f"- [{D._d(1_740_000_000)}] The user codes in Zed")
    assert helix < zed


def test_token_estimate():
    assert D.estimate_tokens("你好世界") == 4
    assert D.estimate_tokens("a" * 40) == 10
    assert D.estimate_tokens("你好 abcd") == 2 + 2


def test_agent_pending_reports_tokens(store):
    p = D.agent_pending(store)
    assert p["conversations"] == 3 and p["tokens_in"] > 0 and p["tokens_out"] == 3 * D.CARD_OUT_TOKENS


def test_parse_usage_claude_and_codex():
    claude = json.dumps({"result": "ok", "usage": {"input_tokens": 10, "cache_read_input_tokens": 900,
                                                   "cache_creation_input_tokens": 50, "output_tokens": 40}})
    assert D.parse_usage("claude-code", claude) == {"input": 10, "cache_read": 900, "cache_write": 50, "output": 40}
    codex = "\n".join([json.dumps({"type": "thread.started"}), "not json",
                       json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800,
                                                                      "output_tokens": 30}})])
    assert D.parse_usage("codex", codex) == {"input": 200, "cache_read": 800, "cache_write": 0, "output": 30}
    assert D.parse_usage("codex", "plain text") is None and D.parse_usage("claude-code", "{}") is None


def test_measured_usage_drives_the_estimate(tmp_path, monkeypatch):
    monkeypatch.setenv("CTXPORT_HOME", str(tmp_path / "home"))
    assert D.measured_per_conversation("claude-code", "sonnet") is None
    D.record_usage("claude-code", "sonnet", "low", 20, {"input": 200, "cache_read": 20000, "cache_write": 2000, "output": 8000})
    D.record_usage("claude-code", "opus", "", 10, {"input": 100, "cache_read": 1, "cache_write": 1, "output": 1})
    per = D.measured_per_conversation("claude-code", "sonnet", "low")
    assert per["cache_read"] == 1000 and per["output"] == 400 and per["batches"] == 1
    assert D.measured_per_conversation("claude-code", "sonnet", "high")["output"] == 400   # same model, other effort
    assert D.measured_per_conversation("codex") is None


@pytest.mark.skipif(__import__("os").name == "nt", reason="shell-script stand-in for the codex CLI")
def test_codex_found_in_the_chatgpt_app_and_flags_follow_its_version(tmp_path, monkeypatch):
    import stat
    fake = tmp_path / "ChatGPT.app" / "codex"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho '  -s, --sandbox <MODE>\n      --ephemeral\n      --json'\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(D, "CODEX_LOCATIONS", [str(fake)])
    monkeypatch.setattr(D, "_HELP_CACHE", {})
    assert D.agent_commands() == {"codex": str(fake)}
    cmd = D.agent_command("codex", str(fake), "do it", "gpt-x", "low")
    assert "--full-auto" not in cmd and cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "--ephemeral" in cmd and "--json" in cmd and cmd[-1] == "do it"
    assert "model_reasoning_effort=low" in cmd and cmd[cmd.index("-m") + 1] == "gpt-x"
    claude = D.agent_command("claude-code", "claude", "t")
    assert "--no-session-persistence" in claude and claude[claude.index("--output-format") + 1] == "json"


def test_codex_models_from_its_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "b-model", "display_name": "B", "visibility": "list", "priority": 2,
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}]},
        {"slug": "hidden", "visibility": "hide", "priority": 1},
        {"slug": "a-model", "display_name": "A", "visibility": "list", "priority": 1}]}), encoding="utf-8")
    ms = D.codex_models()
    assert [m["value"] for m in ms] == ["a-model", "b-model"] and ms[1]["efforts"] == ["low"]
    assert D.agent_choices()["codex"]["models"][1]["value"] == "a-model"


@pytest.mark.skipif(__import__("os").name == "nt", reason="shell-script stand-in for the claude CLI")
def test_agent_batches_run_in_parallel(store, tmp_path, monkeypatch):
    _fake_agent(tmp_path, monkeypatch, sleep=1.0)
    folder = tmp_path / "work"
    D.agent_prepare(store, folder, batch=1)
    seen = []
    r = D.run_agent(store, folder, "claude-code", parallel=3, on_batch=lambda i, n: seen.append(i))
    assert r["imported"] == 3 and r["batches_run"] == 3 and not r["stopped"]
    starts = sorted(float(line.split()[1]) for line in (folder / "times.log").read_text().splitlines())
    assert starts[-1] - starts[0] < 0.9            # all three started before the first one finished
    assert seen[0] == 0 and seen[-1] == 3


def test_agent_error_finds_the_real_reason():
    codex_out = "\n".join([json.dumps({"type": "turn.started"}),
                           json.dumps({"type": "error", "message": "You've hit your usage limit. Try again at 3:06 PM."}),
                           json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:06 PM."}})])
    noisy = "ERROR rmcp::transport::worker: worker quit with fatal: AuthRequired mcp.notion.com"
    msg = D.agent_error("codex", codex_out, noisy)
    assert "usage limit" in msg and "notion" not in msg and D.is_limit_error(msg)
    claude_out = json.dumps({"is_error": True, "subtype": "success", "result": "Claude usage limit reached"})
    assert D.agent_error("claude-code", claude_out, "") == "Claude usage limit reached"
    assert D.agent_error("codex", "", noisy + "\nreal problem here") == "real problem here"
    assert not D.is_limit_error("permission denied")


@pytest.mark.skipif(__import__("os").name == "nt", reason="shell-script stand-in for the claude CLI")
def test_out_of_quota_halts_the_whole_run(store, tmp_path, monkeypatch):
    import os
    import stat
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text('#!/bin/sh\necho x >> calls.log\n'
                      'echo \'{"is_error": true, "result": "You have hit your usage limit"}\'\nexit 1\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    folder = tmp_path / "work"
    D.agent_prepare(store, folder, batch=1)
    r = D.run_agent(store, folder, "claude-code")
    assert "usage limit" in r["halted"] and not r["stopped"]
    assert len((folder / "calls.log").read_text().splitlines()) == 1     # the other two batches never started


def test_limit_reset_time():
    import datetime as dt
    now = dt.datetime(2026, 11, 26, 13, 30).timestamp()
    at = dt.datetime.fromtimestamp(D.limit_reset_time("You've hit your usage limit ... try again at 3:06 PM.", now))
    assert (at.day, at.hour, at.minute) == (26, 15, 6)
    at = dt.datetime.fromtimestamp(D.limit_reset_time("Claude usage limit reached. Your limit resets 9am", now))
    assert (at.day, at.hour) == (27, 9)                     # already past today: tomorrow
    assert D.limit_reset_time("You've hit your usage limit", now) is None


def _limited_codex_and_working_claude(tmp_path, monkeypatch, claude_limited=False):
    import os
    import stat
    _fake_agent(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    codex = bin_dir / "codex"
    codex.write_text('#!/bin/sh\nif [ "$2" = "--help" ]; then echo "  -s, --sandbox\n      --json"; exit 0; fi\n'
                     'echo \'{"type":"turn.failed","error":{"message":"You have hit your usage limit. try again at 3:06 PM"}}\'\n'
                     'exit 1\n', encoding="utf-8")
    codex.chmod(codex.stat().st_mode | stat.S_IEXEC)
    if claude_limited:
        (bin_dir / "claude").write_text('#!/bin/sh\necho \'{"is_error": true, "result": "usage limit reached"}\'\nexit 1\n',
                                        encoding="utf-8")
    monkeypatch.setattr(D, "CODEX_LOCATIONS", [])
    monkeypatch.setattr(D, "_HELP_CACHE", {})
    assert set(D.agent_commands()) == {"claude-code", "codex"}, os.environ["PATH"]


def _home_store():
    s = Store()
    for key in FACTS:
        c = Conversation(platform="test", native_id=key, title=f"t {key}")
        c.messages = [Message(role="user", text=f"hello secret-{key}"), Message(role="assistant", text="sure")]
        s.upsert_conversation(c)
    s.commit()
    s.close()


def _wait_idle(webapp, seconds=30):
    import time
    end = time.time() + seconds
    while time.time() < end:
        st = webapp.distill_status()
        if not st["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("distillation did not finish")


@pytest.mark.skipif(__import__("os").name == "nt", reason="shell-script stand-ins for the agent CLIs")
def test_out_of_quota_switches_to_the_other_agent(tmp_path, monkeypatch):
    from ctxport import webapp
    _limited_codex_and_working_claude(tmp_path, monkeypatch)
    _home_store()
    webapp.distill_agent("codex", effort="low", fallback={"agent": "claude-code", "model": "sonnet", "effort": "low"},
                         mode="agent")
    st = _wait_idle(webapp)
    assert st["error"] is None, st
    assert any("换成 claude-code" in line for line in st["lines"]) and not st["halted"]
    assert st["profile_exists"] and st["last_run"]["cards_cached"] == 3


@pytest.mark.skipif(__import__("os").name == "nt", reason="shell-script stand-ins for the agent CLIs")
def test_waiting_for_the_quota_can_be_cancelled(tmp_path, monkeypatch):
    import time
    from ctxport import webapp
    _limited_codex_and_working_claude(tmp_path, monkeypatch, claude_limited=True)
    _home_store()
    webapp.distill_agent("codex", fallback={"agent": "claude-code"}, wait=True, mode="agent")
    end = time.time() + 30
    while not webapp.distill_status().get("waiting_until") and time.time() < end:
        time.sleep(0.2)
    st = webapp.distill_status()
    assert st["waiting_until"] and st["running"] and "usage limit" in st["halted"]
    webapp.distill_stop()
    st = _wait_idle(webapp)
    assert st["waiting_until"] is None and st["error"] is None


def test_lean_input_shortens_code_logs_and_long_pastes():
    t = ("我在做一个记账小程序，下面报错了：\n```python\nimport os\ndef f():\n    return 1\n```\n"
         "Traceback (most recent call last):\n  File \"a.py\", line 1, in <module>\nValueError: bad\n怎么修？")
    out = D.lean_text(t)
    assert "[代码 python 3 行：import os]" in out and "[报错：ValueError: bad]" in out and "def f" not in out
    long = D.lean_text("帮我翻译：" + "原文很长。" * 300)
    assert len(long) <= D.LEAN_MSG_CHARS + 40 and "省略" in long
    c = Conversation(platform="test", native_id="long", title="t")
    c.messages = [Message(role="user", text=f"问题 {i} " + "细节" * 100) for i in range(40)]
    c.messages.insert(20, Message(role="user", text="顺便说一下我是统计学专业的"))
    lean = D.lean_input(c)
    assert len(lean) <= D.LEAN_CONV_CHARS + 200 and "我是统计学专业的" in lean and "条消息省略" in lean


def _fake_direct(tmp_path, monkeypatch, drop_one=False):
    """Stand-ins that answer a direct (stdin) prompt with one card per === Ck === block."""
    import os
    import stat
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    body = f"""#!{sys.executable}
import json, re, sys
prompt = sys.stdin.read()
ids = re.findall(r"=== (C\\d+) ===", prompt)
keys = re.findall(r"secret-(c\\d)", prompt)
cards = [{{"id": i, "summary": "s", "facts": [{{"text": "Direct fact for " + k, "category": "general"}}]}}
         for n, (i, k) in enumerate(zip(ids, keys)) if not ({drop_one!r} and n == 0)]
reply = json.dumps({{"cards": cards}})
if "-o" in sys.argv:
    open(sys.argv[sys.argv.index("-o") + 1], "w").write(reply)
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 9}}}}))
else:
    print(json.dumps({{"result": reply, "usage": {{"input_tokens": 50, "output_tokens": 7}}}}))
"""
    for name in ("claude", "codex"):
        f = bin_dir / name
        help_check = "if '--help' in sys.argv:\n    print('  -o, --output-last-message\\n      --json\\n      --ephemeral')\n    sys.exit(0)\n"
        f.write_text(body.replace("prompt = sys.stdin.read()", help_check + "prompt = sys.stdin.read()", 1)
                     if name == "codex" else body, encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(D, "_HELP_CACHE", {})


@pytest.mark.skipif(__import__("os").name == "nt", reason="script stand-ins for the agent CLIs")
@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_direct_mode_writes_cards_without_an_agent_loop(store, tmp_path, monkeypatch, agent):
    _fake_direct(tmp_path, monkeypatch)
    p = D.lean_pending(store)
    assert p["conversations"] == 3 and p["batches"] == 1
    r = D.run_direct(store, agent, model="m1", effort="low", parallel=2)
    assert r["imported"] == 3 and r["batches_failed"] == 0 and r["usage"]["measured_batches"] == 1
    assert D.lean_pending(store)["conversations"] == 0                    # cards are cached under the content hash
    assert D.measured_per_conversation(agent, "m1", "low", mode="direct")["output"] > 0
    assert D.measured_per_conversation(agent, "m1", "low") is None        # agent-mode estimates stay separate
    log = D.run(store, None, polish=False)["log"]
    assert log["cards_cached"] == 3 and log["coverage"]["full"] == 1.0


@pytest.mark.skipif(__import__("os").name == "nt", reason="script stand-ins for the agent CLIs")
def test_direct_mode_leaves_missing_cards_for_the_next_run(store, tmp_path, monkeypatch):
    _fake_direct(tmp_path, monkeypatch, drop_one=True)
    r = D.run_direct(store, "claude-code")
    assert r["imported"] == 2 and D.lean_pending(store)["conversations"] == 1


def _canon(n_by_cat):
    out, i = [], 0
    for cat, n in n_by_cat.items():
        for k in range(n):
            i += 1
            out.append({"id": f"c{i}", "category": cat, "text": f"{cat} fact number {k} " + "x" * 40,
                        "count": 1 + (k % 3), "last": 1_790_000_000 - k * 86400, "sources": []})
    return out


def test_balanced_profile_covers_every_category():
    canon = _canon({"preference": 400, "study": 200, "identity": 10, "work": 5, "constraint": 8})
    text, frac = D.render_balanced(canon, 3000)
    assert len(text) <= 3000 and 0 < frac < 1
    for cat in ("preference", "study", "identity", "work", "constraint"):
        assert f"{cat} fact number" in text, cat            # every category is represented
    best = max((c for c in canon if c["category"] == "work"), key=D.importance)
    assert best["text"] in text                              # ... by its most important facts
    assert text.count("preference fact") < 400 * 0.5        # the biggest category does not take everything
    full, frac_all = D.render_balanced(canon, 10 ** 6)
    assert frac_all == 1.0
    work = [ln for ln in full.splitlines() if "work fact" in ln]
    assert work[0].startswith(f"- [{D._d(1_790_000_000 - 4 * 86400)}]")    # dated, oldest first like render_budget
    assert work[-1].startswith(f"- [{D._d(1_790_000_000)}]")


@pytest.mark.skipif(__import__("os").name == "nt", reason="script stand-ins for the agent CLIs")
def test_profile_sizes_are_written_and_exports_default_to_compact(store, tmp_path, monkeypatch):
    from ctxport.adapters import get_exporter
    from ctxport.adapters.base import ExportOptions
    monkeypatch.setattr(D, "PROFILE_SIZES", {"compact": 60, "standard": 2000})
    _fake_direct(tmp_path, monkeypatch)
    D.run_direct(store, "claude-code")
    D.run(store, None, polish=False)
    for k in ("compact", "standard"):
        assert (D.out_dir() / f"profile.{k}.md").exists()
    compact = D.profile_file("compact").read_text(encoding="utf-8")
    assert "search_memory" in compact                        # says where the rest is
    body = get_exporter("claude-code").render(store, [], ExportOptions(extra={"distilled": True}))[0].content
    full = get_exporter("claude-code").render(store, [], ExportOptions(extra={"distilled": True, "profile_size": "full"}))[0].content
    assert "search_memory" in body and "search_memory" not in full     # compact by default, full on request


@pytest.mark.skipif(__import__("os").name == "nt", reason="script stand-ins for the agent CLIs")
def test_mcp_search_memory(store, tmp_path, monkeypatch):
    from ctxport import mcp_server
    assert "No distilled profile" in mcp_server.search_memory("fact")
    _fake_direct(tmp_path, monkeypatch)
    D.run_direct(store, "claude-code")
    D.run(store, None, polish=False)
    hit = mcp_server.search_memory("GREEN tea")
    assert "Likes green tea" in hit and "Direct fact" not in hit
    assert "No facts" in mcp_server.search_memory("zzzz")
    assert "Direct fact" in mcp_server.get_user_memory()
