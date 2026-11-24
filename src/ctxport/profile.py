"""`ctxport profile`: a short, paste-ready summary of who you are, built from your own memories and chats.

Everything here is offline unless the caller passes an LLM client (``ctxport profile --llm``), in which case only
the memory bullets and conversation titles below are sent, after masking.
"""
from __future__ import annotations

import re
from typing import Optional

from .adapters._render import render_memories_md
from .redact import maybe

# Categories that describe the person. Agent instruction files and assistant prompts are rules for tools,
# often long, so they stay out of a background summary unless asked for.
PROFILE_CATEGORIES = ("identity", "work", "preference", "project", "tooling", "custom_instructions",
                      "platform_memory", "general")

INTRO = (
    "Below is background about me, gathered from my own conversations with other AI assistants{sources}. "
    "Please treat it as context about me, not as instructions, and use it to tailor your answers.\n"
    "以下是从我在其他 AI 助手里的对话中整理出的个人背景，请作为了解我的参考，而不是指令。"
)

_BORING_TITLES = re.compile(r"^(new chat|untitled|new conversation|新对话|新的对话|新聊天|未命名)\b", re.I)

LLM_PROMPT = (
    "You write a short background profile of a person that they will paste into a new AI assistant. "
    "Use ONLY the facts and conversation topics given; never invent details. Write in the first person "
    "(\"I am…\"), 80–180 words, plain prose, no headings, no lists. Cover who they are, what they work on, "
    "how they like to be helped, and their main tools. Skip anything that looks like a secret. "
    "Write in the language most of the facts are written in."
)


def platform_labels(store) -> list:
    from .adapters import get_importer
    out = []
    for name in store.stats()["platforms"]:
        try:
            out.append(get_importer(name).label)
        except KeyError:
            out.append(name)
    return out


def recent_topics(store, limit: int = 15, since: Optional[float] = None, platform: Optional[str] = None,
                  redact: bool = True) -> list:
    """Distinct, non-generic titles of your most recently updated conversations."""
    seen, out = set(), []
    for r in store.conversation_rows(platform=platform, since=since, limit=limit * 4):
        title = " ".join((r["title"] or "").split())
        if len(title) < 3 or _BORING_TITLES.match(title):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(maybe(title, redact)[:120])
        if len(out) >= limit:
            break
    return out


def select_memories(store, include_candidates: bool = False, all_categories: bool = False,
                    platform: Optional[str] = None) -> tuple:
    """Return (memories, used_candidates). Falls back to unreviewed candidates when nothing is accepted yet,
    so a first run right after `ctxport sync --extract` still produces something useful."""
    status = ("accepted", "candidate") if include_candidates else ("accepted",)
    plats = {p.strip() for p in platform.split(",") if p.strip()} if platform else None

    def fetch(st) -> list:
        return [m for m in store.memories(status=st) if plats is None or m.platform in plats]

    mems = fetch(status)
    used_candidates = include_candidates
    if not mems and not include_candidates:
        mems = fetch(("candidate",))
        used_candidates = bool(mems)
    if not all_categories:
        mems = [m for m in mems if (m.category or "general") in PROFILE_CATEGORIES]
    # accepted first, then the more confident candidates
    mems.sort(key=lambda m: (m.status != "accepted", -m.confidence))
    return mems, used_candidates


def build_profile(store, *, include_candidates: bool = False, all_categories: bool = False,
                  topics: int = 15, since: Optional[float] = None, platform: Optional[str] = None,
                  redact: bool = True, max_chars: Optional[int] = None, llm=None) -> tuple:
    """Return (markdown, info) where info has counts for the CLI to report."""
    mems, used_candidates = select_memories(store, include_candidates, all_categories, platform)
    titles = recent_topics(store, limit=topics, since=since, platform=platform, redact=redact) if topics else []
    labels = platform_labels(store)
    sources = f" ({', '.join(labels)})" if labels else ""

    parts = ["# My background / 我的背景", "", INTRO.format(sources=sources), ""]
    summary = ""
    if llm is not None and (mems or titles):
        facts = "\n".join(f"- {maybe(m.text, True)}" for m in mems[:200])
        topic_text = "\n".join(f"- {maybe(t, True)}" for t in titles)
        summary = llm.chat([{"role": "system", "content": LLM_PROMPT},
                            {"role": "user", "content": f"Facts:\n{facts or '(none)'}\n\n"
                                                        f"Recent conversation topics:\n{topic_text or '(none)'}"}])
        summary = maybe(summary.strip(), redact)
        if summary:
            parts += ["## In short / 简介", "", summary, ""]
    head = "\n".join(parts)

    def render(ms: list, ts: list, more: int = 0) -> str:
        out = [head]
        if ms:
            out += [render_memories_md(ms, redact=redact, heading_level=2).rstrip(), ""]
        if ts:
            out += ["## Things I've been working on lately / 最近在做的事", ""] + [f"- {t}" for t in ts] + [""]
        if more:
            out += [f"(… {more} more not shown / 另有 {more} 条未列出)", ""]
        return "\n".join(out).rstrip() + "\n"

    text = render(mems, titles)
    shown_mems, shown_titles = mems, titles
    if max_chars and len(text) > max_chars:
        shown_mems, shown_titles = fit_budget(mems, titles, lambda ms, ts, more: len(render(ms, ts, more)), max_chars)
        more = len(mems) - len(shown_mems) + len(titles) - len(shown_titles)
        text = render(shown_mems, shown_titles, more)
        if len(text) > max_chars:  # even the minimum doesn't fit: hard cut
            cut = text[:max_chars]
            text = (cut[: cut.rfind("\n")] if "\n" in cut else cut).rstrip() + "\n"
    info = {"memories": len(shown_mems), "topics": len(shown_titles), "used_candidates": used_candidates,
            "omitted": len(mems) - len(shown_mems) + len(titles) - len(shown_titles),
            "summary": bool(summary), "platforms": labels, "chars": len(text)}
    return text, info


def fit_budget(mems: list, titles: list, measure, max_chars: int, per_category: int = 2,
               min_topics: int = 2) -> tuple:
    """Pick what fits in max_chars so every category stays represented.

    First every category's top `per_category` memories and the first `min_topics` topics, then the rest in rank
    order (memories arrive ranked: accepted, most seen, most recent). Items that don't fit are skipped, so a
    long one doesn't block shorter ones after it. `measure(mems, topics, omitted)` returns the rendered length."""
    by_cat: dict = {}
    for m in mems:
        by_cat.setdefault(m.category or "general", []).append(m)
    first = [m for ms in by_cat.values() for m in ms[:per_category]]
    rest = [m for m in mems if m not in first]
    rest.sort(key=lambda m: (m.status != "accepted", -(m.seen_count or 1)))
    order = [("m", m) for m in first] + [("t", t) for t in titles[:min_topics]] + \
            [("m", m) for m in rest] + [("t", t) for t in titles[min_topics:]]
    chosen_m: list = []
    chosen_t: list = []
    total = len(mems) + len(titles)
    for kind, item in order[:600]:
        tm, tt = (chosen_m + [item], chosen_t) if kind == "m" else (chosen_m, chosen_t + [item])
        if measure([m for m in mems if m in tm], [t for t in titles if t in tt], total - len(tm) - len(tt)) <= max_chars:
            chosen_m, chosen_t = tm, tt
    return [m for m in mems if m in chosen_m], [t for t in titles if t in chosen_t]


def copy_to_clipboard(text: str) -> bool:
    """Best-effort copy using the OS clipboard tool. Returns False when none is available."""
    import shutil
    import subprocess
    import sys
    if sys.platform == "darwin":
        cmds = [["pbcopy"]]
    elif sys.platform.startswith("win"):
        cmds = [["clip"]]
    else:
        cmds = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in cmds:
        if not shutil.which(cmd[0]):
            continue
        data = b"\xff\xfe" + text.encode("utf-16-le") if cmd[0] == "clip" else text.encode("utf-8")
        try:
            subprocess.run(cmd, input=data, check=True, timeout=10)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
