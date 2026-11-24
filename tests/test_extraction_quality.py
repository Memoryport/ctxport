"""Masking of personal identifiers, rule precision, questionnaire coverage across categories, phrase search,
and a busy database."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from ctxport.extract import rule_candidates
from ctxport.models import Conversation, MemoryItem, Message
from ctxport.questionnaire import BY_ID, answers, excerpts
from ctxport.redact import MASK, redact
from ctxport.store import Store


@pytest.mark.parametrize("text", [
    "我的手机号是 13812345678", "+86 138-1234-5678", "身份证 11010519491231002X", "卡号 4111 1111 1111 1111",
    "password hunter2", "pwd is p@ss", "我的 token 是 7f9c2ba4e88f827d616045507605853e",
    "AccessKey ID: LTAI5tABCDEFGHIJKLMNOPQR",
])
def test_identifiers_and_loose_secrets_are_masked(text):
    assert MASK in redact(text)


@pytest.mark.parametrize("text", [
    "ts 1730000000123456789", "the token is valid", "password reset link", "版本 1.2.3", "2024-11-24 13:45:12",
    "run pwd to check", "order 4111111111111112",  # not Luhn-valid
])
def test_ordinary_numbers_and_words_are_left_alone(text):
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    "我是说这个函数有问题", "我是想问一下这个报错怎么解决", "我的问题是：为什么 React 会重复渲染", "我喜欢这个答案，谢谢！",
    "我喜欢你刚才的第二个方案", "I like it", "I love you", "I always forget how to do this",
    "I never got that error before", "I am a bit confused about this code", "I'm a little lost here.",
    "请记住我对花生过敏", "我有糖尿病，帮我规划饮食",
])
def test_not_facts_about_you(text):
    assert rule_candidates(text) == []


@pytest.mark.parametrize("text,fact,cat", [
    ("以后回答我都用英文", "以后回答我都用英文", "preference"),
    ("以后写代码请都加类型注解，不要用 print 调试。", "以后写代码请都加类型注解，不要用 print 调试", "preference"),
    ("我在北京大学读研究生，方向是计算机视觉", "我在北京大学读研究生，方向是计算机视觉", "work"),
    ("我在用 Next.js 14 + Tailwind 写官网", "我在用 Next.js 14 + Tailwind 写官网", "tooling"),
    ("作为一个产品经理，我该如何理解 RAG？", "我是一个产品经理", "identity"),
    ("我主要用 Neovim，帮我写个 Makefile", "我主要用 Neovim", "tooling"),
    ("I like Python more than Go.", "I like Python more than Go", "preference"),
])
def test_more_facts_are_found(text, fact, cat):
    assert rule_candidates(text) == [(fact, cat)]


def test_coverage_counts_answers_from_any_category(tmp_path):
    s = Store(tmp_path / "s.db")
    s.upsert_memory(MemoryItem(text="我是一名后端工程师", category="identity", status="accepted"))
    s.upsert_memory(MemoryItem(text="回答请用中文，先给结论", category="custom_instructions", status="accepted"))
    s.commit()
    got = answers(s)
    assert "role" in got and "language" in got


def test_question_keywords_are_searched_as_phrases(tmp_path):
    s = Store(tmp_path / "s.db")
    s.upsert_conversation(Conversation(platform="chatgpt", native_id="a", updated_at=1.0, messages=[
        Message(role="user", text="I mainly use Python and pandas for data work."),
        Message(role="user", text="I work as a data engineer."),
    ]))
    s.commit()
    assert [r["text"] for r in s.search(["I work as"])] == ["I work as a data engineer."]
    assert len(s.search("I work as")) == 2  # a plain string still means "all of these words"
    texts = [t for _, _, _, t in excerpts(s, BY_ID["role"])]
    assert texts == ["I work as a data engineer."]


def test_a_busy_database_is_waited_for(tmp_path):
    p = tmp_path / "s.db"
    Store(p).close()
    db = sqlite3.connect(p, check_same_thread=False)
    db.execute("BEGIN IMMEDIATE")
    threading.Timer(6.0, db.commit).start()  # longer than sqlite3's default 5 s timeout
    t = time.time()
    s = Store(p)
    s.upsert_memory(MemoryItem(text="I use Go", status="accepted"))
    s.commit()
    assert time.time() - t >= 5.5


def test_more_self_descriptions_and_fewer_task_remarks():
    from ctxport.extract import rule_candidates
    for text, cat in [("我目前是一名大三学生", "identity"), ("我住在上海浦东", "identity"), ("我毕业于浙江大学", "work"),
                      ("我在腾讯做前端开发", "work"), ("我现在用的是 MacBook Pro", "tooling"),
                      ("回答我的时候请先给结论", "preference"), ("I'm based in Berlin", "identity"),
                      ("I work on recommendation systems", "work"), ("My team uses Jira", "tooling"),
                      ("I code mostly in Rust", "tooling"), ("I'm learning Japanese", "project")]:
        assert [c for _, c in rule_candidates(text)] == [cat], text
    for text in ["我用了你的方法，还是不行", "我在想要不要换工作", "我叫了外卖", "我用 Python 跑了一下，结果是 42",
                 "I use this function in a loop and it crashes", "I use the following code:",
                 "I'm a huge fan of your explanation"]:
        assert rule_candidates(text) == [], text


def test_llm_extraction_input_drops_code_and_repeats():
    from ctxport.extract import compact_for_llm, llm_candidates
    msg = ("我是后端工程师，下面这段报错怎么办？\n```go\nfunc main() {\n  panic(err)\n}\n```\n"
           "Traceback (most recent call last):\n  File \"/app/a.py\", line 1, in <module>\n"
           "  File \"/app/b.py\", line 2, in f\n  File \"/app/c.py\", line 3, in g\nValueError: x\n谢谢")
    out = compact_for_llm(msg)
    assert out.startswith("我是后端工程师") and "panic" not in out and "/app/b.py" not in out and out.endswith("谢谢")
    assert "a single line with foo() stays" in compact_for_llm("a single line with foo() stays")

    class Fake:
        sent: list = []

        def chat(self, messages, **kw):
            self.sent.append(messages[-1]["content"])
            return "[]"
    f = Fake()
    llm_candidates(f, ["继续", "I am a nurse", "继续", "继续"])
    assert f.sent[0].count("继续") == 1 and "I am a nurse" in f.sent[0]
