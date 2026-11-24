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
