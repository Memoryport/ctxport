from __future__ import annotations

from pathlib import Path

import fixtures as fx

from ctxport.adapters import get_importer
from ctxport.models import ImportResult


def load_all(platform: str, root: Path) -> tuple:
    imp = get_importer(platform)
    res = ImportResult()
    for unit in imp.discover(root):
        res.extend(imp.load(unit))
    convs = {c.native_id: c for c in res.conversations}
    return convs, res


def dialogue(conv) -> list:
    return [(m.role, m.text) for m in conv.messages if m.kind == "message"]


def kinds(conv) -> list:
    return [m.kind for m in conv.messages]


# ------------------------------------------------------------------ ChatGPT

def test_chatgpt_mainline_branches_and_memories(tmp_path):
    convs, res = load_all("chatgpt", fx.chatgpt_export(tmp_path))
    c = convs["cg-1"]
    assert dialogue(c) == [("user", "Explain induction"), ("assistant", "Induction proves P(n) for all n."),
                           ("user", "Thanks!"), ("assistant", "You're welcome.")]
    assert [m.kind for m in c.messages if m.kind in ("tool_call", "tool_result")] == ["tool_call", "tool_result"]
    hidden = [m for m in c.messages if m.kind == "system_prompt"]
    assert hidden, "custom-instruction / hidden messages must be system_prompt"
    user1 = next(m for m in c.messages if m.text == "Explain induction")
    assert user1.meta["attachments"][0]["key"] == "file-service://file-abc"
    # the regenerated answer + abandoned follow-up are kept as one branch
    assert len(c.branches) == 1
    assert [m.text for m in c.branches[0]] == ["Old answer (regenerated away)", "Abandoned follow-up"]
    assert c.model == "gpt-4o" and c.title == "Induction"
    texts = {m.text for m in res.memories}
    assert "I am a physics student in Lisbon." in texts and "Answer concisely." in texts
    assert "Prefers metric units" in texts
    assert all(m.status == "accepted" for m in res.memories)


# ------------------------------------------------------------------ Claude.ai

def test_claude_ai(tmp_path):
    convs, res = load_all("claude-ai", fx.claude_ai_export(tmp_path))
    c = convs["ca-1"]
    assert dialogue(c) == [("user", "What is a lifetime?"), ("assistant", "A lifetime is a scope")
                           , ("assistant", "for references.")]
    r = next(m for m in c.messages if m.kind == "reasoning")
    assert r.meta["encrypted"] and r.text == "" and "ENCRYPTED" not in (r.raw or "")
    assert kinds(c).count("tool_call") == 1 and kinds(c).count("tool_result") == 1
    assert c.messages[0].meta["attachments"][0]["name"] == "notes.txt"
    assert abs(c.created_at - 1735787045.123456) < 1e-3  # 7-digit fraction parsed
    proj = [m for m in res.memories if m.scope == "project:Thesis"]
    assert {m.text for m in proj if m.category == "custom_instructions"} == {"Always cite sources", "Use LaTeX"}
    # the project memory keeps its project (keyed by uuid in memories.json)
    assert {m.text for m in proj if m.category == "platform_memory"} == {"Thesis on graphs."}
    assert "project-p1" in convs
    assert any(m.text == "User studies math." for m in res.memories)


# ------------------------------------------------------------------ Claude Code

def test_claude_code(tmp_path):
    root = fx.claude_code_home(tmp_path)
    convs, res = load_all("claude-code", root)
    sid = "11111111-2222-3333-4444-555555555555"
    c = convs[sid]
    assert c.title == "Fix the parser"
    assert dialogue(c) == [("user", "Please fix the parser"), ("assistant", "Looking at it.\n\nFound the bug."),
                           ("user", "Now run the tests"), ("assistant", "All tests pass.")]
    # injected command + isMeta are system_prompt, never user dialogue
    assert [m.kind for m in c.messages[:2]] == ["system_prompt", "system_prompt"]
    assert c.messages[2].meta["attachments"][0]["mime"] == "image/png"
    # parallel tool calls stay in the main line, both results kept in order
    assert [m.text for m in c.messages if m.kind == "tool_result"] == ["A", "B"]
    assert sum(1 for m in c.messages if m.kind == "tool_call") == 2
    # api-request-blob duplicates are ignored
    assert not any("DUPLICATE" in m.text for m in c.messages)
    assert any(m.kind == "reasoning" and m.meta.get("encrypted") for m in c.messages)
    # rewound branch + sidechain preserved as branches
    btexts = [[m.text for m in b] for b in c.branches]
    assert ["Old question I rewound", "Old answer"] in btexts
    assert ["sidechain note"] in btexts
    assert c.model == "claude-sonnet-x" and c.meta["git_branch"] == "main"
    sub = convs[f"{sid}/agent-abc"]
    assert sub.meta["is_subagent"] and dialogue(sub)[0] == ("user", "Search the repo")
    mem = {m.text: m for m in res.memories}
    assert mem["I prefer pytest"].status == "accepted"
    assert mem["User is a first-year math student."].status == "candidate"
    assert mem["Use 4-space indentation."].scope.startswith("project:")
    assert not any("pointer" in t for t in mem)


# ------------------------------------------------------------------ Codex

def test_codex(tmp_path):
    root = fx.codex_home(tmp_path)
    convs, res = load_all("codex", root)
    tid = "01a00000-0000-7000-8000-000000000001"
    c = convs[tid]
    assert dialogue(c) == [("user", "Add a login page"), ("assistant", "Done: added login.")]
    u = next(m for m in c.messages if m.text == "Add a login page")
    assert u.meta["attachments"][0]["name"] == "spec.md"
    assert any(a.get("type") == "input_image" for a in u.meta["attachments"])
    sp = [m for m in c.messages if m.kind == "system_prompt"]
    assert len(sp) == 4  # developer + AGENTS.md + environment_context + compacted
    assert [m.kind for m in c.messages if m.kind in ("reasoning", "tool_call", "tool_result")] == \
        ["reasoning", "tool_call", "tool_result"]
    assert next(m for m in c.messages if m.kind == "tool_result").text == "src\n"
    assert c.model == "gpt-x" and c.project == "/work/app" and c.meta["tokens"]["total_tokens"] == 1234
    assert c.meta["git_branch"] == "dev"
    # event_msg fallback + archived flag
    c2 = convs["01a00000-0000-7000-8000-000000000002"]
    assert dialogue(c2) == [("user", "hello there"), ("assistant", "hi!")] and c2.meta["archived"]
    # legacy bare format
    assert dialogue(convs["legacy-3"]) == [("user", "legacy question"), ("assistant", "legacy answer")]
    # thread DB recovery, and the thread that still has a rollout file is not duplicated
    c3 = convs["01a00000-0000-7000-8000-000000000003"]
    assert c3.title == "Recovered thread" and c3.meta["archived"] and c3.model == "gpt-y"
    assert dialogue(c3) == [("user", "Recover me"), ("assistant", "Recovered answer")]
    assert [m.kind for m in c3.messages] == ["message", "reasoning", "tool_call", "tool_result", "message"]
    assert not any("SHOULD NOT APPEAR" in m.text for cv in convs.values() for m in cv.messages)
    texts = {m.text: m for m in res.memories}
    assert texts["Always run tests before committing"].status == "accepted"
    assert texts["The user studies physics in Lisbon."].status == "candidate"
    assert "raw, should be skipped" not in texts


# ------------------------------------------------------------------ Gemini CLI

def test_gemini_cli(tmp_path):
    convs, res = load_all("gemini-cli", fx.gemini_home(tmp_path))
    c = convs["g-json"]
    assert dialogue(c) == [("user", "list files"), ("assistant", "Here they are")]
    assert [m.kind for m in c.messages] == ["message", "reasoning", "message", "tool_call", "tool_result", "system_prompt"]
    assert c.title == "Gemini JSON session" and c.model == "gemini-x"
    j = convs["g-jsonl"]
    assert dialogue(j) == [("user", "hello jsonl"), ("assistant", "final reply")]
    assert j.title == "JSONL session"
    cp = next(v for k, v in convs.items() if k.endswith("checkpoint-mytag"))
    assert dialogue(cp) == [("user", "checkpoint question"), ("assistant", "checkpoint answer")]
    assert any(m.kind == "reasoning" for m in cp.messages)
    assert any(m.text == "My favourite editor is Vim" for m in res.memories)


# ------------------------------------------------------------------ Antigravity

def test_antigravity(tmp_path):
    g = fx.antigravity_home(tmp_path)
    imp = get_importer("antigravity")
    res = ImportResult()
    for root in (g / "antigravity", g / "config"):
        for u in imp.discover(root):
            res.extend(imp.load(u))
    convs = {c.native_id: c for c in res.conversations}
    c = convs["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    steps = [m.text for m in c.messages if m.kind == "message"]
    assert steps == ["Please build a todo app", "I created the todo app with React.", "请把按钮改成蓝色"]
    assert all(m.role == "unknown" for m in c.messages if m.kind == "message")
    assert not any("/Users/x" in s or "0f3a9c6e" in s for s in steps)
    assert {m.meta["name"] for m in c.messages if m.kind == "artifact"} == {"task.md", "walkthrough.md"}
    assert c.title == "Todo app build"
    assert "knowledge/react-style" in convs
    texts = {m.text for m in res.memories}
    assert "React style: User prefers function components." in texts
    assert "Prefer TypeScript" in texts and "Be brief" in texts


def test_protobuf_decoder_rejects_garbage():
    from ctxport.adapters.antigravity import decode_conversation_pb
    assert decode_conversation_pb(b"\x1f\x8b\x08\x00garbage-gzip") == []


# ------------------------------------------------------------------ Cherry Studio

def _check_cherry_topic(c):
    assert c.title == "Calculus help" and c.project == "Tutor"
    assert dialogue(c) == [("user", "What is a derivative?"), ("assistant", "A rate of change."), ("user", "Thanks")]
    assert any(m.kind == "reasoning" and m.text == "hmm" for m in c.messages)
    assert [[m.text for m in b if m.kind == "message"] for b in c.branches] == [["First answer"]]


def test_cherry_local(tmp_path):
    convs, res = load_all("cherry-studio", fx.cherry_home(tmp_path))
    _check_cherry_topic(convs["t1"])
    assert "t2" not in convs  # deleted topic skipped
    assert not any("deleted reply" in m.text for c in convs.values() for m in c.messages + sum(c.branches, []))
    assert dialogue(convs["agent_session/s1"]) == [("user", "agent task")]
    a = convs["agent-session/se"]
    assert dialogue(a) == [("user", "agent db question"), ("assistant", "agent db answer")] and a.project == "Coder"
    texts = {m.text for m in res.memories}                       # assistant prompts are not facts about the user
    assert not {"You are a patient math tutor.", "Write tests first"} & texts


def test_cherry_backups(tmp_path):
    convs, _ = load_all("cherry-studio", fx.cherry_backup_v7(tmp_path))
    _check_cherry_topic(convs["t1"])
    convs, res = load_all("cherry-studio", fx.cherry_backup_v1(tmp_path))
    c = convs["tp"]
    assert c.title == "Old topic" and c.project == "Default"
    assert dialogue(c) == [("user", "v1 question"), ("assistant", "v1 answer")]
    assert any(m.kind == "reasoning" for m in c.messages)
    assert not any(m.text == "Be kind" for m in res.memories)   # an assistant prompt, not a user fact


# ------------------------------------------------------------------ Chatbox

def test_chatbox_versions(tmp_path):
    convs, res = load_all("chatbox", fx.chatbox_v1(tmp_path))
    assert dialogue(convs["s1"]) == [("user", "cb question"), ("assistant", "cb answer")]
    assert [m.text for m in convs["s1"].branches[0]] == ["older thread q", "older thread a"]
    assert dialogue(convs["s0"]) == [("user", "legacy string content")]
    assert any(m.text == "User likes dark mode" for m in res.memories)
    convs, _ = load_all("chatbox", fx.chatbox_v2(tmp_path))
    assert dialogue(convs["s2"]) == [("user", "v2 question"), ("assistant", "v2 answer")]
    convs, res = load_all("chatbox", fx.chatbox_v3(tmp_path))
    c = convs["s/3"]
    assert dialogue(c) == [("user", "v3 question"), ("assistant", "v3 answer")]
    assert [m.text for m in c.branches[0]] == ["v3 old thread"]
    mems = {m.text: m for m in res.memories}
    assert {"Prefers short answers", "Studies calculus"} <= set(mems)
    assert "Friendly tutor" not in mems                          # the agent's persona is not the user's identity


# ------------------------------------------------------------------ Takeout / generic

def test_takeout(tmp_path):
    convs, _ = load_all("gemini-web", fx.takeout(tmp_path))
    cs = sorted(convs.values(), key=lambda c: c.created_at)
    assert len(cs) == 2  # 30-minute gap splits the day
    assert dialogue(cs[0]) == [("user", "What is a prime?"), ("assistant", "A prime has two divisors."),
                               ("user", "Give an example"), ("assistant", "7 is prime.")]
    assert cs[0].messages[2].meta["attachments"] == [{"name": "photo.jpg"}]
    assert dialogue(cs[1]) == [("user", "你好"), ("assistant", "你好！")]


def test_takeout_html_only(tmp_path):
    import zipfile
    z = tmp_path / "t.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Takeout/My Activity/Gemini Apps/MyActivity.html", "<html></html>")
    _, res = load_all("gemini-web", z)
    assert any("JSON" in w for w in res.warnings)


def test_openai_messages(tmp_path):
    convs, _ = load_all("openai-messages", fx.openai_messages(tmp_path))
    (c,) = convs.values()
    assert dialogue(c) == [("user", "ds question"), ("assistant", "ds answer")]
    assert [m.kind for m in c.messages] == ["system_prompt", "message", "reasoning", "message"]
