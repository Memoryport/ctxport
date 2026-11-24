"""The background questionnaire: 30 questions in three stages.

Stage 1 is who you are and how to talk to you, stage 2 what you work on and with, stage 3 how you like to be
helped. It drives three things: the coverage report (which questions your memories already answer), the
question-driven LLM pass that searches your history only for unanswered questions (`ctxport extract --questions`),
and the short interview for whatever is still unknown (`ctxport ask`).

Sensitive topics (ID numbers, street addresses, health, politics, religion, sexuality, finances, passwords) are
deliberately not asked.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .redact import is_sensitive


@dataclass(frozen=True)
class Question:
    id: str
    stage: int
    category: str
    en: str
    zh: str
    keywords: tuple  # search terms for the user's own messages (FTS needs 3+ characters; shorter ones use LIKE)


Q = Question
QUESTIONS: tuple = (
    # ---- stage 1: who you are, how to talk to you
    Q("name", 1, "identity", "What should assistants call you?", "助手该怎么称呼你？",
      ("叫我", "我叫", "my name is", "call me")),
    Q("language", 1, "identity", "Which languages do you use, and which should answers be in?",
      "你用哪些语言？回答用哪种语言？", ("用中文", "用英文", "in English", "in Chinese", "reply in")),
    Q("timezone", 1, "identity", "Which city or time zone are you in (no street address)?",
      "你在哪个城市或时区（不需要具体地址）？", ("时区", "我在北京", "我在上海", "time zone", "based in", "I live in")),
    Q("role", 1, "work", "What is your job or role (student, engineer, researcher, founder …)?",
      "你的职业或身份是什么（学生、工程师、研究者、创业者…）？", ("我是", "工作是", "I am a", "I'm a", "I work as")),
    Q("field", 1, "work", "Which field or industry are you in?", "你在哪个领域或行业？",
      ("行业", "领域", "industry", "my field")),
    Q("organization", 1, "work", "What kind of organization or team are you in, and what is your part in it?",
      "你在什么样的组织或团队里，负责什么？", ("我们公司", "我们团队", "our team", "our company", "at work")),
    Q("education", 1, "work", "What did or do you study?", "你学的是什么专业？",
      ("专业", "本科", "硕士", "博士", "major in", "PhD", "studying")),
    Q("expert", 1, "identity", "Where are you an expert, so answers can skip the basics?",
      "你在哪些方面是专家，回答可以跳过基础？", ("很熟悉", "多年经验", "years of experience", "I know")),
    Q("beginner", 1, "identity", "Where are you a beginner and want things explained?",
      "你在哪些方面是新手，需要解释？", ("新手", "刚开始学", "不太懂", "beginner", "new to", "just started")),
    Q("one_liner", 1, "preference", "If a new assistant should remember one thing about you, what is it?",
      "如果新助手只能记住关于你的一件事，是什么？", ("请记住", "以后", "from now on", "always remember")),
    # ---- stage 2: what you work on and with
    Q("projects", 2, "project", "What are your main 1–3 projects or tasks right now?",
      "你现在最主要的 1–3 个项目或任务是什么？", ("项目", "在做", "正在开发", "working on", "I'm building", "my project")),
    Q("project_goals", 2, "project", "What are those projects trying to achieve, and by when?",
      "这些项目的目标和时间点是什么？", ("目标是", "截止", "上线", "deadline", "launch", "goal is")),
    Q("topics", 2, "project", "Which subjects do you most often ask about?", "你最常问哪些主题？",
      ("怎么", "如何", "how to", "how do")),
    Q("languages_code", 2, "tooling", "Which programming languages do you use?", "你用哪些编程语言？",
      ("Python", "TypeScript", "JavaScript", "Rust", "Golang", "Java", "C++")),
    Q("frameworks", 2, "tooling", "Which frameworks and libraries do you use most?", "你最常用哪些框架和库？",
      ("React", "Django", "FastAPI", "Spring", "PyTorch", "框架", "framework")),
    Q("editor", 2, "tooling", "Which editor, IDE and AI tools do you use?", "你用什么编辑器、IDE 和 AI 工具？",
      ("VS Code", "VSCode", "Cursor", "JetBrains", "PyCharm", "Vim", "编辑器")),
    Q("os_hardware", 2, "tooling", "Which operating system and hardware do you work on?",
      "你用什么操作系统和硬件？", ("macOS", "Windows", "Linux", "Ubuntu", "MacBook", "GPU")),
    Q("infra", 2, "tooling", "Where do you deploy or run things (cloud, servers, databases)?",
      "你在哪里部署或运行（云、服务器、数据库）？", ("AWS", "GCP", "Azure", "Docker", "Kubernetes", "服务器", "数据库")),
    Q("other_tools", 2, "tooling", "Which non-coding tools do you rely on (docs, design, data, office)?",
      "你常用哪些非编程工具（文档、设计、数据、办公）？", ("Excel", "Notion", "Figma", "飞书", "Word", "PowerPoint")),
    Q("formats", 2, "tooling", "Which kinds of files or data do you handle most?", "你最常处理哪类文件或数据？",
      ("CSV", "PDF", "JSON", "表格", "spreadsheet", "dataset")),
    # ---- stage 3: how you like to be helped
    Q("length", 3, "preference", "Do you prefer short or detailed answers?", "你喜欢简短还是详细的回答？",
      ("简洁", "简短", "详细", "concise", "brief", "detailed", "shorter")),
    Q("format", 3, "preference", "Which format helps you most (steps, tables, code first, summary first)?",
      "哪种格式对你最有用（步骤、表格、先给代码、先给结论）？", ("表格", "分步骤", "先给", "bullet", "table", "step by step")),
    Q("depth", 3, "preference", "Do you want the reasoning and trade-offs, or just the answer?",
      "你想看推理和取舍，还是只要答案？", ("解释一下", "为什么", "只要答案", "explain why", "just the answer")),
    Q("tone", 3, "preference", "What tone do you like (formal, casual, direct, encouraging)?",
      "你喜欢什么语气（正式、随意、直接、鼓励）？", ("语气", "直接一点", "tone", "be direct", "casual")),
    Q("code_style", 3, "preference", "Any conventions for code (style, comments, tests, typing)?",
      "写代码有什么约定（风格、注释、测试、类型）？", ("注释", "代码风格", "type hints", "comments", "unit test", "style guide")),
    Q("avoid", 3, "preference", "What should assistants never do in answers?", "回答里有什么是你不希望出现的？",
      ("不要", "别再", "don't", "do not", "stop", "never")),
    Q("goals", 3, "general", "What are your longer-term goals (learning, career, a product)?",
      "你的长期目标是什么（学习、职业、产品）？", ("我想", "希望", "目标", "I want to", "my goal", "plan to")),
    Q("constraints", 3, "general", "What limits should answers respect (budget, company rules, compliance, time)?",
      "回答需要遵守哪些限制（预算、公司规定、合规、时间）？", ("预算", "公司规定", "合规", "budget", "compliance", "not allowed")),
    Q("workflows", 3, "general", "Which recurring tasks do you use AI for?", "你经常用 AI 做哪些重复性的事？",
      ("每次", "经常", "每周", "every time", "usually", "weekly")),
    Q("interests", 3, "general", "Any interests outside work that help with examples or context?",
      "工作以外有什么兴趣，能帮助举例或理解你？", ("爱好", "喜欢玩", "hobby", "in my free time")),
)

assert len(QUESTIONS) == 30 and len({q.id for q in QUESTIONS}) == 30

BY_ID = {q.id: q for q in QUESTIONS}
STAGE_TITLES = {1: ("Who you are", "你是谁"), 2: ("What you work on and with", "你在做什么、用什么"),
                3: ("How you like to be helped", "你希望怎样被帮助")}


def questions(stage=None) -> list:
    return [q for q in QUESTIONS if stage is None or q.stage == stage]


# ---------------------------------------------------------------- coverage

def answers(store, include_candidates: bool = True) -> dict:
    """{question id: [memory texts]} for every question your memories answer.

    A memory answers a question when it was saved for it (from `ctxport ask` or `extract --questions`), or when it
    is in the question's category and mentions one of its keywords. The second rule is a heuristic, so the report
    calls those questions "likely answered"."""
    status = ("accepted", "candidate") if include_candidates else ("accepted",)
    mems = store.memories(status=status)
    out: dict = {}
    for q in QUESTIONS:
        hits = [m.text for m in mems if m.question == q.id]
        if not hits:
            kws = [k.lower() for k in q.keywords]
            hits = [m.text for m in mems if m.category == q.category and m.question is None
                    and any(k in m.text.lower() for k in kws)]
        if hits:
            out[q.id] = hits
    return out


def unanswered(store, stage=None, include_candidates: bool = True) -> list:
    got = answers(store, include_candidates)
    return [q for q in questions(stage) if q.id not in got]


def format_question_coverage(store) -> str:
    got = answers(store)
    lines = [f"Questionnaire / 背景问卷: {len(got)} of {len(QUESTIONS)} answered"]
    for st in (1, 2, 3):
        qs = questions(st)
        missing = [q.id for q in qs if q.id not in got]
        en, zh = STAGE_TITLES[st]
        lines.append(f"  stage {st} {en} / {zh}: {len(qs) - len(missing)}/{len(qs)}"
                     + (f" · missing: {', '.join(missing)}" if missing else ""))
    if len(got) < len(QUESTIONS):
        lines.append("  next: `ctxport extract --questions` searches your history for the missing ones (local LLM), "
                     "`ctxport ask` asks you directly.")
    return "\n".join(lines)


# ---------------------------------------------------------------- question-driven extraction

EXTRACT_PROMPT = """You answer ONE question about the user, using ONLY the excerpts below.
The excerpts are the user's own messages from past chats, each tagged with an id like [e3].
Question: {question}
Rules:
- Answer in 1-3 short sentences, first person ("I ..."), in the language most excerpts are written in.
- After each sentence, cite the excerpt ids it relies on, e.g. [e2][e5]. A sentence without a citation is dropped.
- If the excerpts do not clearly answer the question, reply exactly: UNKNOWN
- When excerpts disagree, prefer the most recent (higher dates) and add "(earlier: ...)" for the older fact.
- Never include passwords, keys, ID or account numbers, street addresses, health, political, religious, sexual or
  financial details.
Excerpts:
{excerpts}"""

_CITE = re.compile(r"\[(e\d+)\]")


def excerpts(store, q: Question, limit: int = 20, width: int = 400) -> list:
    """Up to `limit` of your own messages that mention the question's keywords, newest first:
    [(excerpt id, conversation id, created_at, text)]."""
    from .redact import redact
    seen, out = set(), []
    for kw in q.keywords:
        for r in store.search(kw, limit=limit * 3):
            if r["role"] != "user" or r["id"] in seen:
                continue
            seen.add(r["id"])
            text = redact(r["text"] or "")
            pos = max(0, text.lower().find(kw.lower()) - width // 3)
            out.append((r["conversation_id"], r["created_at"] or r["conv_updated_at"],
                        " ".join(text[pos:pos + width].split())))
        if len(out) >= limit * 2:
            break
    out.sort(key=lambda x: -(x[1] or 0))
    return [(f"e{i}", cid, ts, text) for i, (cid, ts, text) in enumerate(out[:limit], 1)]


def parse_answer(reply: str, valid: dict) -> list:
    """[(sentence without citations, [conversation ids])]; sentences without a valid citation are dropped."""
    reply = (reply or "").strip()
    if not reply or reply.upper().startswith("UNKNOWN"):
        return []
    out = []
    for sent in re.split(r"(?<=[.。!！?？\]])\s+|\n+", reply):
        ids = [valid[c] for c in _CITE.findall(sent) if c in valid]
        text = re.sub(r"\s+([.。,，;；!！?？])", r"\1", " ".join(_CITE.sub("", sent).split())).strip(" -*•")
        if ids and len(text) >= 4 and "UNKNOWN" not in text.upper() and not is_sensitive(text):
            out.append((text, list(dict.fromkeys(ids))))
    return out


def extract_by_questions(store, llm, stage=None, progress=None, redo: bool = False) -> dict:
    """Ask the LLM only the unanswered questions (or every question with redo=True), each over the excerpts that
    mention its keywords. Answers become candidates tied to the question and to the conversations they cite."""
    from .models import MemoryItem
    todo = questions(stage) if redo else unanswered(store, stage)
    stats = {"asked": 0, "answered": 0, "unknown": 0, "no_excerpts": 0, "new": 0}
    for q in todo:
        ex = excerpts(store, q)
        if not ex:
            stats["no_excerpts"] += 1
            continue
        when = {cid: ts for _, cid, ts, _ in ex}
        body = "\n".join(f"[{eid}] ({time.strftime('%Y-%m-%d', time.localtime(ts)) if ts else 'undated'}) {text}"
                         for eid, _, ts, text in ex)
        stats["asked"] += 1
        reply = llm.chat([{"role": "user", "content": EXTRACT_PROMPT.format(question=q.en, excerpts=body)}])
        facts = parse_answer(reply, {eid: cid for eid, cid, _, _ in ex})
        if not facts:
            stats["unknown"] += 1
            continue
        stats["answered"] += 1
        for text, cids in facts:
            platform = cids[0].split(":", 1)[0]
            for cid in cids:
                mem = MemoryItem(text=text, platform=platform, category=q.category, source_ref=cid,
                                 confidence=0.7, status="candidate", question=q.id)
                if store.upsert_memory(mem, seen_at=when.get(cid)) == "new":
                    stats["new"] += 1
        store.commit()
        if progress:
            progress(f"questions: {q.id} → {len(facts)} fact(s)")
    return stats


# ---------------------------------------------------------------- paste prompt for another assistant

SELF_REPORT_PROMPT = {
    "zh": ("请根据你对我的记忆和我们以前的对话，回答下面关于我的问题。\n"
           "只写你有把握的内容；不确定就写“未知”，不要推测或编造。每条后面用括号写上大概是什么时候知道的。\n"
           "不要写密码、证件号、具体住址、健康、政治、宗教、财务等敏感信息。\n"
           "严格按下面的格式输出（保留每个 ## 标题），不要输出别的内容：\n"),
    "en": ("Based on your memory of me and our past conversations, answer the questions below about me.\n"
           "Only write what you are confident about; write \"unknown\" otherwise. Never guess or invent. After each "
           "item, add roughly when you learned it in brackets.\n"
           "Leave out passwords, ID numbers, street addresses, and health, political, religious or financial details.\n"
           "Use exactly this format (keep every ## heading) and output nothing else:\n"),
}


def self_report_prompt(lang: str = "zh", stage=None) -> str:
    lang = "en" if lang == "en" else "zh"
    lines = [SELF_REPORT_PROMPT[lang]]
    for q in questions(stage):
        lines.append(f"## {q.id}: {q.en if lang == 'en' else q.zh}\n- ...")
    return "\n".join(lines) + "\n"


_UNKNOWN = re.compile(r"^(未知|不知道|unknown|n/?a|none|\.\.\.|…)[。.\s]*$", re.I)


def parse_memory_list(text: str) -> list:
    """[(category, question id or None, fact)] from a "## heading" + "- fact" list, as the self-report prompt
    asks for. A heading may be a question id ("## editor: …") or a category ("## tooling")."""
    from .extract import CATEGORIES
    out, cat, qid = [], "general", None
    for line in (text or "").splitlines():
        h = re.match(r"^\s*#{1,6}\s*([\w-]+)", line)
        if h:
            key = h.group(1).lower()
            q = BY_ID.get(key)
            qid, cat = (q.id, q.category) if q else (None, key if key in CATEGORIES else "general")
            continue
        b = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if b:
            fact = b.group(1).strip()
            if fact and not _UNKNOWN.match(fact) and len(fact) >= 3 and not is_sensitive(fact):
                out.append((cat, qid, fact))
    return out
