from __future__ import annotations

from ctxport import profile
from ctxport.cli import main
from ctxport.models import Conversation, MemoryItem, Message
from ctxport.store import Store


def _seed(db):
    s = Store(db)
    s.upsert_conversation(Conversation(native_id="a", platform="chatgpt", title="Tuning a Postgres index",
                                       updated_at=200.0, messages=[Message(role="user", text="hi")]))
    s.upsert_conversation(Conversation(native_id="b", platform="chatgpt", title="New chat", updated_at=300.0,
                                       messages=[Message(role="user", text="hi")]))
    s.upsert_conversation(Conversation(native_id="c", platform="claude-code", title="Refactor the CLI parser",
                                       updated_at=100.0, messages=[Message(role="user", text="hi")]))
    s.commit()
    return s


def test_profile_falls_back_to_candidates_then_prefers_accepted(tmp_path):
    s = _seed(tmp_path / "p.db")
    s.upsert_memory(MemoryItem(text="I am a backend engineer", platform="chatgpt", category="identity",
                               status="candidate", confidence=0.4))
    s.commit()
    text, st = profile.build_profile(s)
    assert st["used_candidates"] and "backend engineer" in text
    assert "Tuning a Postgres index" in text and "Refactor the CLI parser" in text
    assert "New chat" not in text  # generic titles are skipped
    assert text.index("Tuning a Postgres") < text.index("Refactor the CLI")  # most recent first

    s.upsert_memory(MemoryItem(text="I prefer short answers", platform="manual", category="preference",
                               status="accepted"))
    s.upsert_memory(MemoryItem(text="Always run ruff before commit", platform="claude-code",
                               category="agent_instructions", status="accepted"))
    s.commit()
    text, st = profile.build_profile(s)
    assert not st["used_candidates"] and "short answers" in text and "backend engineer" not in text
    assert "ruff" in text  # agent instructions are in, as in exports to CLAUDE.md / AGENTS.md
    text, _ = profile.build_profile(s, all_categories=True, include_candidates=True, topics=0)
    assert "ruff" in text and "backend engineer" in text and "Postgres" not in text
    s.close()


def test_profile_masks_secrets_and_trims(tmp_path):
    s = _seed(tmp_path / "p.db")
    s.upsert_memory(MemoryItem(text="My api_key = sk-abcdefghijklmnopqrstuvwxyz123456", category="general",
                               status="accepted"))
    s.commit()
    text, _ = profile.build_profile(s)
    assert "sk-abcdefghijklmnop" not in text
    text, st = profile.build_profile(s, max_chars=200)
    assert len(text) <= 200 and st["chars"] == len(text)
    s.close()


class FakeLLM:
    def __init__(self):
        self.sent = None

    def chat(self, messages, **kw):
        self.sent = messages
        return "I am a backend engineer who likes short answers."


def test_profile_llm_summary_gets_only_facts_and_titles(tmp_path):
    s = _seed(tmp_path / "p.db")
    s.upsert_memory(MemoryItem(text="I prefer short answers", category="preference", status="accepted"))
    s.commit()
    llm = FakeLLM()
    text, st = profile.build_profile(s, llm=llm)
    assert st["summary"] and "## In short" in text
    sent = llm.sent[1]["content"]
    assert "short answers" in sent and "Tuning a Postgres index" in sent and "hi" not in sent.split()
    s.close()


def test_profile_cli(tmp_path, capsys):
    db = tmp_path / "cli.db"
    assert main(["--db", str(db), "profile"]) == 1  # empty store: tells the user what to do first
    _seed(db).close()
    assert main(["--db", str(db), "memories", "add", "I live in Berlin", "--category", "identity"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "profile"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# My background") and "Berlin" in out
    dest = tmp_path / "me.md"
    assert main(["--db", str(db), "profile", "--out", str(dest), "--topics", "0"]) == 0
    assert "Berlin" in dest.read_text(encoding="utf-8") and "Postgres" not in dest.read_text(encoding="utf-8")
    assert main(["--db", str(db), "profile", "--llm", "--base-url", "https://example.com/v1"]) == 2
