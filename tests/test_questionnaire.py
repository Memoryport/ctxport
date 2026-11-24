"""Broader rules, branches and confirmed summaries, the 30-question questionnaire, question-driven extraction,
the interview and the self-report prompt round trip."""
from __future__ import annotations

from ctxport import questionnaire as qn
from ctxport.cli import main, run_interview
from ctxport.extract import confirmed_summaries, rule_candidates, user_texts
from ctxport.models import Conversation, MemoryItem, Message
from ctxport.store import Store


def test_questionnaire_shape():
    assert len(qn.QUESTIONS) == 30
    assert [len(qn.questions(s)) for s in (1, 2, 3)] == [10, 10, 10]
    assert all(q.keywords and q.zh and q.en for q in qn.QUESTIONS)


def test_broader_rules():
    assert rule_candidates("作为一名高中老师，我想做一套练习题。") == [("我是一名高中老师", "identity")]
    assert rule_candidates("As a data scientist, I need clean plots.") == [("I am a data scientist", "identity")]
    assert rule_candidates("我平时写 Rust 比较多。") == [("我平时写 Rust 比较多", "tooling")]
    assert rule_candidates("Our stack is FastAPI and Postgres.") == [("Our stack is FastAPI and Postgres", "tooling")]
    assert rule_candidates("I'm building a CLI for chat history.")[0][1] == "project"
    # a question that opens with a self-description keeps only that part
    assert rule_candidates("我是前端开发，怎么学 Rust？") == [("我是前端开发", "identity")]
    assert rule_candidates("Can you sort this list?") == []


def test_branches_and_confirmed_summaries():
    conv = Conversation(platform="chatgpt", native_id="b", messages=[
        Message(role="user", text="Help me pick a database."),
        Message(role="assistant", text="So your stack is Go and Postgres, deployed on Fly.io?"),
        Message(role="user", text="yes"),
    ], branches=[[Message(role="user", text="I mainly use Go for services.")]])
    assert "I mainly use Go for services." in user_texts(conv)
    (c,) = confirmed_summaries(conv)
    assert "Go and Postgres" in c and c.startswith("[An assistant said this and I confirmed it]")


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.prompts = replies, []

    def chat(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else "UNKNOWN"


def _store(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_conversation(Conversation(platform="claude-code", native_id="e1", title="setup", updated_at=1_700_000_000,
                                       messages=[Message(role="user", text="Switching my editor to Cursor today."),
                                                 Message(role="assistant", text="Cursor is a fork of VS Code.")]))
    s.commit()
    return s


def test_question_extraction_needs_citations_and_targets_gaps(tmp_path):
    s = _store(tmp_path)
    q = qn.BY_ID["editor"]
    (eid, cid, ts, text) = qn.excerpts(s, q)[0]
    assert cid == "claude-code:e1" and "Cursor" in text  # only your own messages
    llm = FakeLLM(["I use Cursor as my editor [e1]. I also like Emacs."])
    st = qn.extract_by_questions(s, llm, stage=2)
    assert st["answered"] == 1 and st["new"] == 1
    (m,) = s.memories(status=None)
    assert m.text == "I use Cursor as my editor." and m.question == "editor" and m.status == "candidate"
    assert m.source_ref == "claude-code:e1" and m.last_seen == 1_700_000_000
    assert "Emacs" not in m.text  # uncited sentence dropped
    assert len(llm.prompts) == 1  # only questions with matching excerpts were sent
    # answered now, so a second run asks nothing about it
    llm2 = FakeLLM([])
    qn.extract_by_questions(s, llm2, stage=2)
    assert all("editor" not in p.lower() or "Which editor" not in p for p in llm2.prompts)
    s.close()


def test_parse_answer_unknown_and_invalid_ids():
    assert qn.parse_answer("UNKNOWN", {"e1": "x"}) == []
    assert qn.parse_answer("I live in Berlin [e9].", {"e1": "x"}) == []
    assert qn.parse_answer("我在柏林工作[e1]。", {"e1": "c:1"}) == [("我在柏林工作。", ["c:1"])]


def test_interview_saves_answers_as_accepted(tmp_path):
    s = _store(tmp_path)
    answers = iter(["Dana", "", "q"])
    out = []
    n = run_interview(s, stage=1, max_questions=5, read=lambda prompt: next(answers), write=out.append)
    assert n == 1
    (m,) = s.memories()
    assert m.text == "Dana" and m.question == "name" and m.category == "identity"
    assert "name" in qn.answers(s) and qn.unanswered(s, 1)[0].id == "language"
    s.close()


def test_self_report_prompt_round_trip(tmp_path, capsys):
    prompt = qn.self_report_prompt("zh", stage=1)
    assert prompt.count("\n## ") == 10 and "## name:" in prompt
    reply = "## name: 怎么称呼\n- 叫我 Dana\n## role: 职业\n- 后端工程师（2025 年）\n## field: 领域\n- 未知\n## tooling\n- 用 Neovim\n"
    assert qn.parse_memory_list(reply) == [("identity", "name", "叫我 Dana"), ("work", "role", "后端工程师（2025 年）"),
                                           ("tooling", None, "用 Neovim")]
    f = tmp_path / "reply.md"
    f.write_text(reply, encoding="utf-8")
    db = str(tmp_path / "q.db")
    assert main(["--db", db, "memories", "import", str(f), "--platform", "chatgpt"]) == 0
    assert "3 new candidates" in capsys.readouterr().out
    assert main(["--db", db, "memories", "coverage"]) == 0
    out = capsys.readouterr().out
    assert "Questionnaire" in out and "stage 1" in out
    assert main(["--db", db, "prompt", "--lang", "en", "--stage", "3"]) == 0
    assert capsys.readouterr().out.count("\n## ") == 10


def test_extract_questions_refuses_remote_llm_without_flag(tmp_path):
    assert main(["--db", str(tmp_path / "q.db"), "extract", "--questions", "--base-url", "https://example.com/v1"]) == 2


def test_accepted_answer_counts_as_answered_without_keywords(tmp_path):
    s = _store(tmp_path)
    s.upsert_memory(MemoryItem(text="Short, direct answers please", category="preference", status="accepted",
                               question="length"))
    assert "length" in qn.answers(s, include_candidates=False)
    s.close()
