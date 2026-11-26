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


def _fake_agent(tmp_path, monkeypatch, drop_one=False):
    """A stand-in `claude` CLI: reads the batch files named in the -p task and writes cards/<name>.json."""
    import os
    import stat
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"""#!{sys.executable}
import json, re, sys, pathlib
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
