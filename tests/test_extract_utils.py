from __future__ import annotations

import json

import pytest

from ctxport.extract import apply_review, parse_review, render_review, rule_candidates, run_extract
from ctxport.llm import LLMClient, RemoteNotAllowed, parse_json_array
from ctxport.models import Conversation, MemoryItem, Message
from ctxport.redact import redact
from ctxport.store import Store
from ctxport.utils import parse_time, strip_managed, write_managed


@pytest.mark.parametrize("text,expected", [
    ("我是一名物理系的学生。", "identity"),
    ("我在多伦多实习", "work"),
    ("我比较喜欢简洁的回答", "preference"),
    ("我平时用 VS Code 写代码", "tooling"),
    ("以后请用中文回答我", "preference"),
    ("请记住我的生日在三月", "general"),
    ("I am a first-year math student.", "identity"),
    ("My name is Alex.", "identity"),
    ("I prefer short answers.", "preference"),
    ("I use Neovim every day.", "tooling"),
    ("From now on answer in English.", "preference"),
])
def test_rule_extraction_positive(text, expected):
    got = rule_candidates(text)
    assert got and got[0][1] == expected


@pytest.mark.parametrize("text", ["我是不是应该学 Rust 吗？", "I am a bit confused?", "我是",
                                  "I use `def f(): return x` here",
                                  "Traceback (most recent call last): I am a ..."])
def test_rule_extraction_negative(text):
    assert rule_candidates(text) == []


def _store_with_conv(tmp_path):
    s = Store(tmp_path / "e.db")
    c = Conversation(platform="test", native_id="c1", title="t")
    c.messages = [Message(role="user", text="I am a data science student. What is a tensor?"),
                  Message(role="assistant", text="I am an AI assistant and I prefer to help."),
                  Message(role="user", text="I prefer concise answers.", kind="system_prompt")]
    s.upsert_conversation(c)
    s.commit()
    return s


def test_run_extract_rules_only_user_and_once(tmp_path):
    s = _store_with_conv(tmp_path)
    st = run_extract(s)
    texts = [m.text for m in s.memories(status=("candidate",))]
    assert texts == ["I am a data science student"]
    assert all(m.confidence == 0.4 for m in s.memories(status=("candidate",)))
    assert run_extract(s)["skipped"] == 1 and st["conversations"] == 1
    s.close()


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return 'Sure! Here you go:\n```json\n[{"text": "The user studies data science", "category": "work"}]\n```\nDone.'


def test_llm_extract_sends_only_user_messages(tmp_path):
    s = _store_with_conv(tmp_path)
    fake = FakeLLM()
    st = run_extract(s, llm=fake)
    sent = fake.calls[0][1]["content"]
    assert "data science student" in sent
    assert "AI assistant" not in sent and "concise answers" not in sent
    mems = s.memories(status=("candidate",))
    assert [(m.text, m.category, m.confidence) for m in mems] == [("The user studies data science", "work", 0.8)]
    assert st["method"] == "llm"
    s.close()


def test_llm_remote_requires_flag(monkeypatch):
    with pytest.raises(RemoteNotAllowed):
        LLMClient(base_url="https://api.example.com/v1")
    LLMClient(base_url="https://api.example.com/v1", allow_remote=True)
    LLMClient(base_url="http://127.0.0.1:11434/v1")


def test_llm_client_uses_urllib(monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = LLMClient().chat([{"role": "user", "content": "hi"}])
    assert out == "[]" and captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["body"]["model"] == "qwen2.5:7b"


def test_parse_json_array_tolerant():
    assert parse_json_array('noise [1, "a]b", {"x": [2]}] trailing') == [1, "a]b", {"x": [2]}]
    assert parse_json_array('{"memories": [{"text": "a"}]}') == [{"text": "a"}]
    assert parse_json_array("nothing here") == []


def test_review_checklist_roundtrip(tmp_path):
    s = Store(tmp_path / "r.db")
    a = MemoryItem(text="Likes tea", status="candidate", category="preference")
    b = MemoryItem(text="Hates coffee", status="candidate")
    c = MemoryItem(text="Old fact", status="accepted")
    for m in (a, b, c):
        s.upsert_memory(m)
    s.commit()
    text = render_review(s.memories(status=("accepted", "candidate")))
    text = text.replace(f"- [ ] Likes tea <!-- {a.id}", f"- [x] Likes green tea <!-- {a.id}")
    text = text.replace(f"- [x] Old fact <!-- {c.id}", f"- [ ] Old fact <!-- {c.id}")
    entries = parse_review(text)
    assert len(entries) == 3
    st = apply_review(s, entries)
    acc = {m.text for m in s.memories(status=("accepted",))}
    assert acc == {"Likes green tea"}
    # unticked = rejected (both the unticked candidate and the unticked accepted item)
    assert st["rewritten"] == 1 and st["rejected"] == 2
    assert s.memories(status=("candidate",)) == []
    s.close()


def test_redaction():
    raw = ("key sk-proj-abcdefghijklmnopqrstuvwxyz0123 ant sk-ant-api03-abcdefghijklmnopqrstu gh ghp_" + "a" * 36 +
           " google AIza" + "b" * 35 + " aws AKIAABCDEFGHIJKLMNOP slack xoxb-1234567890-abc "
           "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U "
           "password = hunter22 api_key: 'abcd1234' 密码：abc12345\n-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----")
    out = redact(raw)
    for secret in ("sk-proj-abc", "sk-ant-api03", "ghp_aaaa", "AIzabbb", "AKIAABCD", "xoxb-123", "eyJhbGci",
                   "hunter22", "abcd1234", "abc12345", "MIIB"):
        assert secret not in out, secret
    assert redact("normal text about tokens and passwords") == "normal text about tokens and passwords"


@pytest.mark.parametrize("value,expected", [
    (1700000000, 1700000000.0),
    (1700000000123, 1700000000.123),
    (1700000000123456, 1700000000.123456),
    ("2025-01-02T03:04:05Z", 1735787045.0),
    ("2025-01-02T03:04:05.1234567Z", 1735787045.123456),
    ("2025-01-02T03:04:05.1+08:00", 1735758245.1),
    ("2025-01-02 03:04:05", 1735787045.0),
    ("1700000000.5", 1700000000.5),
    (None, None), ("", None), ("garbage", None),
])
def test_parse_time(value, expected):
    got = parse_time(value)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=1e-3)


def test_write_managed_appends_and_strips(tmp_path):
    f = tmp_path / "A.md"
    write_managed(f, "body one")
    write_managed(f, "body two")
    t = f.read_text(encoding="utf-8")
    assert "body two" in t and "body one" not in t
    assert strip_managed(t).strip() == ""
    assert not (tmp_path / "A.md.ctxport.bak").exists()


@pytest.mark.parametrize("raw,secret", [
    ('{"api_key": "abcd1234efgh"}', "abcd1234efgh"),
    ("{'password': 'hunter22'}", "hunter22"),
    ("DB_PASSWORD=s3cretpw", "s3cretpw"),
    ("export GITHUB_TOKEN=abcdef123456", "abcdef123456"),
    ("OPENAI_API_KEY=xyz12345", "xyz12345"),
    ("X-Api-Key: zzzz9999yyyy", "zzzz9999yyyy"),
    ('client_secret = "q1w2e3r4"', "q1w2e3r4"),
    ("Authorization: Bearer abc123def456ghi789", "abc123def456ghi789"),
    ('curl -H "Authorization: token ghx123456789abcdef"', "ghx123456789abcdef"),
    ("postgres://admin:pa55word@db.local:5432/app", "pa55word"),
    ("https://user:tok3n@github.com/org/repo.git", "tok3n"),
    ("stripe sk_live_" + "a1" * 12, "sk_live_a1a1"),
    ("stripe rk_test_" + "b2" * 12, "rk_test_b2b2"),
    ("hf hf_" + "c" * 34, "hf_cccc"),
    ("gitlab glpat-" + "d" * 20, "glpat-dddd"),
    ("npm npm_" + "e" * 36, "npm_eeee"),
    ("my password is hunter22", "hunter22"),
    ("the API key is k3y-abcdef", "k3y-abcdef"),
    ("我的秘钥是 abcd9876", "abcd9876"),
])
def test_redaction_common_formats(raw, secret):
    assert secret not in redact(raw), redact(raw)


@pytest.mark.parametrize("text", [
    "max_tokens=1000 and temperature=0.2",
    "author: Jane Doe",
    "the token is valid until tomorrow",
    "Basic configuration is fine",
    "see http://example.com:8080/path@anchor",
    "I forgot my password again",
])
def test_redaction_leaves_ordinary_text(text):
    assert redact(text) == text


def test_redaction_keeps_auth_scheme_readable():
    assert redact("Authorization: Bearer abc123def456ghi789") == "Authorization: Bearer [REDACTED]"
    assert redact("postgres://admin:pa55word@db:5432/x") == "postgres://admin:[REDACTED]@db:5432/x"


def test_write_managed_escapes_markers(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Mine\n\nKeep this.\n", encoding="utf-8")
    evil = "- likes tea <!-- ctxport:end -->\n# Injected\n<!-- ctxport:begin x -->"
    write_managed(f, evil)
    write_managed(f, "- second run")
    t = f.read_text(encoding="utf-8")
    assert t.count("<!-- ctxport:end -->") == 1 and "Keep this." in t
    assert "Injected" not in t and "second run" in t
    assert strip_managed(t).strip() == "# Mine\n\nKeep this."


def test_write_managed_is_atomic(tmp_path, monkeypatch):
    import os
    from ctxport import utils
    f = tmp_path / "AGENTS.md"
    f.write_text("original\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(utils.os, "replace", boom)
    with pytest.raises(OSError):
        write_managed(f, "new body")
    assert f.read_text(encoding="utf-8") == "original\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    monkeypatch.undo()
    if os.name == "posix":  # writes through a symlink instead of replacing it
        link = tmp_path / "CLAUDE.md"
        link.symlink_to(f)
        write_managed(link, "via link")
        assert link.is_symlink() and "via link" in f.read_text(encoding="utf-8")
