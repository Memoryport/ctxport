"""Memory extraction: offline rules (default) + optional LLM. Results are candidates for review."""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from .llm import LLMClient, parse_json_array
from .models import Conversation, MemoryItem
from .redact import redact
from .store import Store
from .utils import chunks

RULE_CONFIDENCE = 0.4
LLM_CONFIDENCE = 0.8
CATEGORIES = ("identity", "work", "preference", "project", "tooling", "general")

_SPLIT = re.compile(r"(?<=[。！；!;\n])|(?<=[.?])\s+")
_QUESTION_END = re.compile(r"[?？吗呢吧嘛]\s*[。.!！]?$")
_CODE_HINT = re.compile(r"(```|^\s*[$>#] |\b(def|class|function|import|return|const|let|var)\b.*[({;=]|Traceback|"
                        r"Error:|Exception|^\s*at \w|[{}]\s*$|=>|::|\w+\(\)|/[\w.-]+/[\w.-]+|https?://)", re.M)

ZH_RULES = [
    (re.compile(r"^(我是|我叫|叫我)"), "identity"),
    (re.compile(r"^我(目前|现在)?在.{1,30}(工作|上学|读书|就读|实习|上班)"), "work"),
    (re.compile(r"^我(比较|更|一般|通常|不|很|非常|最)?(喜欢|偏好|习惯|讨厌|不喜欢)"), "preference"),
    (re.compile(r"^我(主要|平时|一般|通常)?(用|使用)"), "tooling"),
    (re.compile(r"^(以后|今后|之后)(请|都|要)"), "preference"),
    (re.compile(r"^请记住"), "general"),
    (re.compile(r"^我的.{1,12}是"), "identity"),
]
EN_RULES = [
    (re.compile(r"^i am an? ", re.I), "identity"),
    (re.compile(r"^i'm an? ", re.I), "identity"),
    (re.compile(r"^my name is ", re.I), "identity"),
    (re.compile(r"^call me ", re.I), "identity"),
    (re.compile(r"^i (work|study|intern) (at|for|in) ", re.I), "work"),
    (re.compile(r"^i (prefer|like|love|hate|always|never|usually) ", re.I), "preference"),
    (re.compile(r"^i (mainly |mostly |usually )?use ", re.I), "tooling"),
    (re.compile(r"^(please (always|remember|never)|from now on)\b", re.I), "preference"),
]


def split_sentences(text: str) -> list:
    out = []
    for part in _SPLIT.split(text or ""):
        s = part.strip(" \t\r\n-*•>")
        if s:
            out.append(s)
    return out


def rule_candidates(text: str) -> list:
    """[(sentence, category)] from one user message."""
    found = []
    if not text or len(text) > 20000:
        return found
    for s in split_sentences(text):
        if not (6 <= len(s) <= 200):
            continue
        if _QUESTION_END.search(s) or _CODE_HINT.search(s):
            continue
        for rx, cat in ZH_RULES + EN_RULES:
            if rx.search(s):
                found.append((s.rstrip("。.;；"), cat))
                break
    return found


def user_texts(conv: Conversation) -> list:
    return [m.text for m in conv.messages if m.role == "user" and m.kind == "message" and (m.text or "").strip()]


SYSTEM_PROMPT = """You extract durable personal memory about the USER from their own chat messages.
Return ONLY a JSON array: [{"text": "...", "category": "identity|work|preference|project|tooling|general"}].
Rules:
- Only stable facts or preferences about the user (who they are, what they work on, how they like answers,
  tools they use, ongoing projects). Skip one-off task details and anything about other people.
- Write each item in the third person ("The user …" / "用户…"), in the language the user mainly writes in.
- Never include passwords, keys, ID/account numbers, addresses, or health, sexual or criminal information.
- If nothing qualifies, return [].
"""


def llm_candidates(client: LLMClient, texts: list, chunk_chars: int = 6000) -> list:
    out = []
    for chunk in chunks([redact(t) for t in texts], chunk_chars):
        reply = client.chat([{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": "User messages:\n\n" + chunk}])
        for item in parse_json_array(reply):
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            cat = str(item.get("category") or "general").lower()
            if 4 <= len(text) <= 400:
                out.append((text, cat if cat in CATEGORIES else "general"))
    return out


def run_extract(store: Store, llm: Optional[LLMClient] = None, platform: Optional[str] = None,
                days: Optional[int] = None, limit: Optional[int] = None,
                progress: Optional[Callable[[str], None]] = None) -> dict:
    method = "llm" if llm else "rules"
    since = time.time() - days * 86400 if days else None
    stats = {"conversations": 0, "skipped": 0, "new": 0, "method": method}
    for row in store.conversation_rows(platform=platform, limit=limit, since=since):
        cid = row["id"]
        if store.is_extracted(cid, method):
            stats["skipped"] += 1
            continue
        conv = store.get_conversation(cid)
        if conv is None:
            continue
        if conv.meta.get("is_subagent"):
            store.mark_extracted(cid, method)
            continue
        texts = user_texts(conv)
        scope = "global"
        if llm:
            pairs = llm_candidates(llm, texts) if texts else []
            conf = LLM_CONFIDENCE
        else:
            pairs = [p for t in texts for p in rule_candidates(t)]
            conf = RULE_CONFIDENCE
        for text, cat in pairs:
            mem = MemoryItem(text=text, platform=conv.platform, category=cat, source_ref=cid, confidence=conf,
                             status="candidate", scope=scope)
            if store.upsert_memory(mem) == "new":
                stats["new"] += 1
        store.mark_extracted(cid, method)
        stats["conversations"] += 1
        if progress and stats["conversations"] % 25 == 0:
            progress(f"extract[{method}]: {stats['conversations']} conversations…")
        store.commit()
    store.commit()
    return stats


# ------------------------------------------------------------------ review checklist

REVIEW_HEADER = """# ctxport memory review / 记忆审核

Tick `[x]` to keep (accepted), untick `[ ]` to reject. You may rewrite the text after the checkbox.
Lines you delete are left unchanged. Then run `ctxport memories edit` again to apply.
勾选 [x] 保留，取消勾选为拒绝，可直接改写文字；保存后再运行一次 `ctxport memories edit` 写回。

"""
_LINE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.*?)\s*<!--\s*([0-9a-f]{16})\s+([\w:-]+)\s*-->\s*$")


def render_review(memories: list) -> str:
    lines = [REVIEW_HEADER]
    by_scope: dict = {}
    for m in memories:
        by_scope.setdefault(m.scope, []).append(m)
    for scope in sorted(by_scope, key=lambda s: (s != "global", s)):
        lines.append(f"## {scope}\n")
        for m in sorted(by_scope[scope], key=lambda x: (x.status != "accepted", x.category, -x.confidence)):
            box = "x" if m.status == "accepted" else " "
            text = " ".join(m.text.split())
            lines.append(f"- [{box}] {text} <!-- {m.id} {m.category} -->"
                         + ("" if m.status == "accepted" else f"  <!-- candidate {m.platform} {m.confidence:.1f} -->"))
        lines.append("")
    return "\n".join(lines)


def parse_review(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = re.sub(r"\s*<!--\s*candidate[^>]*-->\s*$", "", line)
        m = _LINE.match(line)
        if m:
            out.append({"checked": m.group(1).lower() == "x", "text": m.group(2).strip(), "id": m.group(3),
                        "category": m.group(4)})
    return out


def apply_review(store: Store, entries: list) -> dict:
    stats = {"accepted": 0, "rejected": 0, "rewritten": 0}
    current = {m.id: m for m in store.memories(status=None)}
    for e in entries:
        old = current.get(e["id"])
        if not old:
            continue
        if not e["checked"]:
            if old.status != "rejected":
                store.set_memory_status(e["id"], "rejected")
                stats["rejected"] += 1
            continue
        if " ".join(old.text.split()) != e["text"] and e["text"]:
            store.update_memory_text(e["id"], e["text"], e["category"], status="accepted")
            stats["rewritten"] += 1
        elif old.status != "accepted" or old.category != e["category"]:
            store.update_memory_text(e["id"], old.text, e["category"], status="accepted")
            stats["accepted"] += 1
    store.commit()
    return stats


def coverage_report(store: Store, min_user_messages: int = 5) -> dict:
    """How complete the extracted memory is: memories per category, empty categories, and conversations where
    you wrote at least `min_user_messages` messages but nothing was extracted (per platform)."""
    cats = {c: 0 for c in CATEGORIES}
    for r in store.db.execute("SELECT category, COUNT(*) n FROM memories WHERE status!='rejected' GROUP BY category"):
        cats[r["category"] or "general"] = r["n"]
    silent: dict = {}
    total: dict = {}
    for r in store.db.execute(
            "SELECT c.platform, COUNT(*) n, SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM memories m WHERE m.source_ref=c.id)"
            " THEN 1 ELSE 0 END) silent FROM conversations c JOIN extracted e ON e.conversation_id=c.id"
            " WHERE c.user_count >= ? GROUP BY c.platform", (min_user_messages,)):
        total[r["platform"]] = r["n"]
        silent[r["platform"]] = r["silent"] or 0
    return {"categories": cats, "empty_categories": [c for c in CATEGORIES if not cats.get(c)],
            "conversations": total, "silent_conversations": silent, "min_user_messages": min_user_messages}


def format_coverage(cov: dict) -> str:
    lines = ["Coverage / 覆盖率"]
    lines.append("  memories by category: " + ", ".join(f"{c} {n}" for c, n in cov["categories"].items()
                                                         if c in CATEGORIES))
    if cov["empty_categories"]:
        lines.append("  ! empty: " + ", ".join(cov["empty_categories"])
                     + "  (try `ctxport extract --llm`, or add facts with `ctxport memories add`)")
    for p, n in sorted(cov["conversations"].items()):
        k = cov["silent_conversations"].get(p, 0)
        lines.append(f"  {p}: {k} of {n} conversations with ≥{cov['min_user_messages']} of your messages gave nothing"
                     + (f" ({k * 100 // n}%)" if n else ""))
    return "\n".join(lines)
