"""Deterministic synthetic chat history (Chinese + English) for the benchmarks."""
from __future__ import annotations

import random

from ctxport.models import Conversation, MemoryItem, Message

ZH_Q = ["这个报错怎么解决？", "帮我解释一下这段代码在做什么", "{t} 和 {u} 有什么区别？", "怎么用 {t} 实现分页查询",
        "为什么我的 {t} 服务启动很慢", "能不能帮我优化一下这个 SQL", "前端性能优化有哪些常见手段？", "机器学习模型怎么做特征工程",
        "写一个 {t} 的单元测试", "这段代码有内存泄漏吗", "数据库索引应该怎么设计", "如何在 {t} 里处理并发", "帮我把这段话翻译成英文",
        "前端 打包 体积太大怎么办", "解释一下 {t} 的事件循环"]
EN_Q = ["Why does {t} throw this error?", "How do I paginate results in {t}?", "What's the difference between {t} and {u}?",
        "Can you refactor this function?", "Explain how vector databases work", "Write a unit test for this {t} module",
        "How should I structure a {t} project?", "Is there a memory leak here?", "How do I speed up this query?",
        "What does this stack trace mean?"]
TECH = ["Python", "Go", "Rust", "React", "Vue", "Django", "PostgreSQL", "Redis", "Kubernetes", "Docker", "Node.js",
        "TypeScript", "Flutter", "Spark", "PyTorch"]
FACTS_ZH = ["我是一名后端工程师，主要写 {t}。", "我在一家金融科技公司工作。", "我平时用 {t} 写代码。", "我比较喜欢简洁的回答。",
            "我最近在做一个记账小程序。", "我们团队主要用 {t} 部署服务。", "以后回答都用中文。"]
FACTS_EN = ["I am a data engineer at a logistics company.", "I mainly use {t} at work.", "I prefer concise answers.",
            "I'm currently building a habit tracker in {t}.", "Our stack is {t} and {u}."]
ZH_ANS = ["首先需要确认配置是否正确，然后检查日志中的错误信息。", "这里的关键是理解数据是如何在组件之间流动的。",
          "可以考虑使用缓存来减少数据库的查询次数，同时注意缓存失效的策略。", "建议把这个函数拆分成更小的部分，便于测试和维护。",
          "前端性能优化通常包括减少请求数量、压缩资源、懒加载图片和代码分割。", "机器学习中特征工程往往比模型选择更重要。",
          "并发处理时要特别注意共享状态，必要时加锁或者使用消息队列。", "索引能加快查询，但会拖慢写入，需要根据读写比例权衡。"]
EN_ANS = ["First check the configuration, then look at the logs for the actual error.",
          "The key is understanding how data flows between the components.",
          "Consider caching to cut database round trips, and plan how the cache is invalidated.",
          "Split this function into smaller pieces so each can be tested on its own.",
          "Vector databases store embeddings and answer nearest-neighbour queries with approximate indexes.",
          "Indexes speed up reads but slow down writes, so weigh them against your read/write ratio."]
CODE_LINES = ["def handler(event, context):", "    items = load_items(event['ids'])", "    for item in items:",
              "        if item.status == 'ready':", "            process(item)", "    return {'ok': True}",
              "const res = await fetch(url, { method: 'POST' });", "SELECT id, name FROM users WHERE created_at > ?;",
              "fmt.Println(err)", "  File \"/app/main.py\", line 42, in <module>", "Traceback (most recent call last):",
              "ERROR 2025-01-02 10:00:00 connection refused", "    at Object.<anonymous> (/srv/index.js:12:5)"]


def _fill(rng, s):
    return s.format(t=rng.choice(TECH), u=rng.choice(TECH))


def _code(rng, lines):
    return "```\n" + "\n".join(rng.choice(CODE_LINES) for _ in range(lines)) + "\n```"


def user_message(rng, zh: bool, facts: list) -> str:
    q = _fill(rng, rng.choice(ZH_Q if zh else EN_Q))
    r = rng.random()
    if r < 0.30:
        text = q + "\n\n" + _code(rng, rng.randint(15, 80))       # pasted code / logs
    elif r < 0.38:
        text = q + "\n\n" + "\n".join(rng.choice(CODE_LINES) for _ in range(rng.randint(40, 200)))  # bare log
    else:
        text = q
    if rng.random() < 0.12:
        fact = _fill(rng, rng.choice(FACTS_ZH if zh else FACTS_EN))
        facts.append(fact)
        text = fact + ("" if zh else " ") + text
    if rng.random() < 0.05:   # the same short message sent again (common: "继续", retries)
        text = "继续" if zh else "continue"
    return text


def assistant_message(rng, zh: bool) -> str:
    parts = [rng.choice(ZH_ANS if zh else EN_ANS) for _ in range(rng.randint(4, 14))]
    if rng.random() < 0.4:
        parts.insert(rng.randint(0, len(parts)), _code(rng, rng.randint(5, 30)))
    return ("" if zh else " ").join(parts)


def conversations(n: int = 600, seed: int = 7, facts: list = None) -> list:
    rng = random.Random(seed)
    facts = facts if facts is not None else []
    out = []
    t0 = 1.70e9
    for i in range(n):
        zh = rng.random() < 0.6
        platform = rng.choice(["chatgpt", "claude-ai", "claude-code"])
        conv = Conversation(platform=platform, native_id=f"c{i:05d}", title=_fill(rng, rng.choice(ZH_Q if zh else EN_Q)))
        t = t0 + i * 3600
        for j in range(rng.randint(6, 40)):
            conv.messages.append(Message(role="user", text=user_message(rng, zh, facts), id=f"{i}-{j}u", created_at=t))
            t += 30
            if platform == "claude-code" and rng.random() < 0.5:
                conv.messages.append(Message(role="assistant", kind="tool_call", text="Read(src/app.py)", created_at=t))
                conv.messages.append(Message(role="tool", kind="tool_result",
                                             text="\n".join(rng.choice(CODE_LINES) for _ in range(rng.randint(50, 400))),
                                             created_at=t))
            conv.messages.append(Message(role="assistant", text=assistant_message(rng, zh), id=f"{i}-{j}a", created_at=t))
            t += 30
        out.append(conv)
    return out


def append_one(conv: Conversation) -> Conversation:
    """The same conversation after one more exchange (what the extension sends on the next sync)."""
    c = Conversation.from_dict(conv.to_dict())
    last = c.messages[-1].created_at or 0
    c.messages.append(Message(role="user", text="再详细一点 / more detail please", id="new-u", created_at=last + 60))
    c.messages.append(Message(role="assistant", text="好的。" * 50, id="new-a", created_at=last + 90))
    c.updated_at = None
    return c


MEM_BASE_ZH = ["我平时用 {t} 写代码", "我在做一个 {t} 项目", "我们团队用 {t} 部署", "我喜欢 {t} 风格的回答", "我是 {t} 工程师",
               "我在学习 {t}", "我负责公司的 {t} 迁移", "我用 {t} 做数据分析"]
MEM_BASE_EN = ["I use {t} every day", "I'm building a {t} side project", "Our team deploys with {t}",
               "I'm a {t} engineer", "I'm learning {t}", "I maintain the {t} service at work", "I like {t} for scripting"]
CATS = ["identity", "work", "preference", "project", "tooling", "general"]


def memories(n: int = 2000, seed: int = 11) -> list:
    """Memory candidates where about a third are rewordings of an earlier one (case, width, spacing, subject)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        if out and rng.random() < 0.33:
            base = rng.choice(out)
            text = rng.choice([base.text.lower(), base.text + "。", "用户" + base.text.lstrip("我"),
                               base.text.replace(" ", ""), base.text.upper()])
            cat = base.category
        else:
            tmpl = rng.choice(MEM_BASE_ZH + MEM_BASE_EN)
            text = tmpl.format(t=f"{rng.choice(TECH)}{rng.randint(1, 400)}")
            cat = rng.choice(CATS)
        out.append(MemoryItem(text=text, platform="chatgpt", category=cat, source_ref=f"chatgpt:c{i}", confidence=0.4,
                              status="candidate", seen_count=rng.randint(1, 6), last_seen=1.70e9 + i))
    return out
