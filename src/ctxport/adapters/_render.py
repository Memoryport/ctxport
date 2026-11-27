"""Render memories and conversations as Markdown / plain text."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..models import Conversation, MemoryItem, Message
from ..redact import maybe
from ..utils import fmt_time

CATEGORY_TITLES = {
    "identity": "About me / 关于我",
    "work": "Work & study / 工作与学习",
    "preference": "Preferences / 偏好",
    "project": "Projects / 项目",
    "tooling": "Tools & environment / 工具与环境",
    "custom_instructions": "Custom instructions / 自定义指令",
    "agent_instructions": "Agent instructions / 代理指令",
    "assistant_prompt": "Assistant prompts / 助手提示词",
    "platform_memory": "Saved memories / 平台记忆",
    "general": "General / 其他",
}
CATEGORY_ORDER = list(CATEGORY_TITLES)



def project_name(scope: str) -> str:
    """The project's folder name only: exports go to other services, which don't need the local directory layout."""
    path = scope.split(":", 1)[-1].rstrip("/\\")
    return re.split(r"[/\\]", path)[-1] or path

def _cat_key(c: str) -> int:
    return CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else len(CATEGORY_ORDER)


def group_memories(memories: Iterable[MemoryItem]) -> dict:
    groups: dict = {}
    for m in memories:
        groups.setdefault(m.scope or "global", {}).setdefault(m.category or "general", []).append(m)
    return groups


def render_memories_md(memories: list, redact: bool = True, heading_level: int = 2,
                       title: Optional[str] = None, max_chars: Optional[int] = None) -> str:
    """Markdown bullet list grouped by scope → category."""
    h = "#" * heading_level
    lines: list = []
    if title:
        lines += [f"{'#' * max(1, heading_level - 1)} {title}", ""]
    groups = group_memories(memories)
    for scope in sorted(groups, key=lambda s: (s != "global", s)):
        if scope != "global":
            lines += [f"{h} Project: {project_name(scope)}", ""]
        for cat in sorted(groups[scope], key=_cat_key):
            hh = h if scope == "global" else h + "#"
            lines.append(f"{hh} {CATEGORY_TITLES.get(cat, cat)}")
            lines.append("")
            seen = set()
            for m in groups[scope][cat]:
                t = maybe(m.text, redact).strip()
                if t in seen:
                    continue
                seen.add(t)
                t = t.replace("\n", "\n  ")
                lines.append(f"- {t}")
            lines.append("")
    out = "\n".join(lines).rstrip() + "\n"
    if max_chars and len(out) > max_chars:
        cut = out[:max_chars]
        cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
        out = cut.rstrip() + "\n\n<!-- truncated by ctxport to fit the size limit -->\n"
    return out


def render_memories_plain(memories: list, redact: bool = True) -> str:
    lines = []
    for m in memories:
        t = " ".join(maybe(m.text, redact).split())
        scope = "" if m.scope == "global" else f" (project {project_name(m.scope)})"
        lines.append(f"- {t}{scope}")
    return "\n".join(lines) + ("\n" if lines else "")


def visible(msgs: list, include_tools: bool = False, include_reasoning: bool = False,
            include_system: bool = False) -> list:
    out = []
    for m in msgs:
        if m.kind in ("message", "artifact", "attachment"):
            if m.role == "system" and not include_system:
                continue
            out.append(m)
        elif m.kind in ("tool_call", "tool_result") and include_tools:
            out.append(m)
        elif m.kind == "reasoning" and include_reasoning:
            out.append(m)
        elif m.kind == "system_prompt" and include_system:
            out.append(m)
    return out


ROLE_LABEL = {"user": "User", "assistant": "Assistant", "system": "System", "tool": "Tool", "unknown": "Unknown"}


def _msg_header(m: Message) -> str:
    bits = [ROLE_LABEL.get(m.role, m.role)]
    if m.kind != "message":
        bits[0] += f" · {m.kind}"
        if m.meta.get("tool"):
            bits[0] += f" `{m.meta['tool']}`"
    if m.created_at:
        bits.append(fmt_time(m.created_at))
    if m.model:
        bits.append(m.model)
    return " · ".join(bits)


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def render_message_md(m: Message, redact: bool = True) -> str:
    text = maybe(m.text or "", redact)
    if m.kind in ("tool_call", "tool_result"):
        body = _fence(text[:20000] + ("\n…(truncated)" if len(text) > 20000 else ""))
    elif m.kind == "reasoning":
        body = "> " + text.replace("\n", "\n> ") if text else "> (reasoning content not stored)"
    elif m.kind == "attachment" and not text:
        body = f"*Attachment:* {m.meta.get('name') or m.meta.get('path') or m.meta.get('mime') or ''}"
    else:
        body = text
    atts = m.meta.get("attachments") or []
    if atts:
        body += "\n\n" + "\n".join(f"- 📎 {a.get('name') or a.get('path') or a.get('key') or a}" for a in atts if a)
    return f"### {_msg_header(m)}\n\n{body}\n"


def render_conversation_md(conv: Conversation, redact: bool = True, include_tools: bool = False,
                           include_reasoning: bool = False, include_system: bool = False) -> str:
    vis = lambda msgs: visible(msgs, include_tools, include_reasoning, include_system)  # noqa: E731
    main = vis(conv.messages)
    lines = [f"# {maybe(conv.title, redact)}", ""]
    meta_rows = [
        ("Platform", conv.platform), ("ID", f"`{conv.id}`"), ("Project", conv.project), ("Model", conv.model),
        ("Created", fmt_time(conv.created_at)), ("Updated", fmt_time(conv.updated_at)),
        ("Messages", f"{len(main)} shown / {len(conv.messages)} stored" + (f", {len(conv.branches)} branches" if conv.branches else "")),
        ("Source", f"`{conv.source_path}`" if conv.source_path else None),
    ]
    for k, v in meta_rows:
        if v:
            lines.append(f"- **{k}:** {v}")
    for k in ("git_branch", "cli_version", "archived", "is_subagent", "parent_id", "tags", "folder"):
        if conv.meta.get(k) not in (None, "", False, []):
            lines.append(f"- **{k}:** {conv.meta[k]}")
    lines.append("")
    for m in main:
        lines.append(render_message_md(m, redact))
    for i, b in enumerate(conv.branches, 1):
        bm = vis(b)
        if not bm:
            continue
        lines += ["---", "", f"## Branch {i} / 分支 {i} ({len(bm)} messages)", ""]
        for m in bm:
            lines.append(render_message_md(m, redact))
    return "\n".join(lines).rstrip() + "\n"


def render_transcript(msgs: list, redact: bool = True, limit_chars: Optional[int] = None) -> str:
    parts = []
    for m in msgs:
        role = ROLE_LABEL.get(m.role, m.role)
        parts.append(f"[{role}] {maybe(m.text or '', redact).strip()}")
    out = "\n\n".join(parts)
    if limit_chars and len(out) > limit_chars:
        out = "…\n" + out[-limit_chars:]
    return out
