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
    # broader self-descriptions (C2)
    (re.compile(r"^作为(一名|一个|一位)?.{1,15}[，,]"), "identity"),
    (re.compile(r"^我(是|在)做.{1,20}的"), "work"),
    (re.compile(r"^我(之前|以前)?(学|读)的是"), "work"),
    (re.compile(r"^我(的)?(技术栈|工作|专业|研究方向|主业)是"), "work"),
    (re.compile(r"^我(正在|在)(做|开发|写|研究|准备|搭建)"), "project"),
    (re.compile(r"^我们?(团队|公司|组)(主要|一般|平时)?(用|使用)"), "tooling"),
    (re.compile(r"^我(平时|一般|主要|经常|日常)(写|用|使用|在用)"), "tooling"),
    (re.compile(r"^我(对|在).{1,15}(是新手|不太熟|比较熟|很熟)"), "identity"),
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
    # broader self-descriptions (C2)
    (re.compile(r"^as an? [\w-]+( [\w-]+){0,3},", re.I), "identity"),
    (re.compile(r"^i work as ", re.I), "work"),
    (re.compile(r"^i have \d+\+? years? of experience", re.I), "identity"),
    (re.compile(r"^i'?m (new to|a beginner|an expert|experienced) ", re.I), "identity"),
    (re.compile(r"^i'?m (currently )?(building|working on|developing|writing|researching) ", re.I), "project"),
    (re.compile(r"^(my|our) (tech )?(stack|setup) (is|includes|uses) ", re.I), "tooling"),
    (re.compile(r"^we (mainly |mostly |usually )?use ", re.I), "tooling"),
]
# A question can still open with a self-description: "我是前端开发，怎么学 Rust？" → "我是前端开发".
_CLAUSE = re.compile(r"[，,:：]")
_LEAD_CATEGORIES = ("identity", "work", "project", "tooling")


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
        if _CODE_HINT.search(s):
            continue
        if _QUESTION_END.search(s):
            lead = _CLAUSE.split(s, 1)[0].strip()
            if lead != s and 6 <= len(lead) <= 80:
                for rx, cat in ZH_RULES + EN_RULES:
                    if cat in _LEAD_CATEGORIES and rx.search(lead):
                        found.append((lead, cat))
                        break
            continue
        for rx, cat in ZH_RULES + EN_RULES:
            if rx.search(s):
                found.append((_as_statement(s.rstrip("。.;；")), cat))
                break
    return found


_AS_ZH = re.compile(r"^作为((?:一名|一个|一位)?)(.{1,15}?)[，,]")
_AS_EN = re.compile(r"^as (an?) ([\w-]+(?: [\w-]+){0,3}),", re.I)


def _as_statement(s: str) -> str:
    """"作为一名老师，我想…" → "我是一名老师"; "As a data scientist, I need…" → "I am a data scientist"."""
    m = _AS_ZH.match(s)
    if m:
        return f"我是{m.group(1)}{m.group(2)}"
    m = _AS_EN.match(s)
    if m:
        return f"I am {m.group(1).lower()} {m.group(2)}"
    return s


def user_texts(conv: Conversation) -> list:
    """Your own messages from the main line and from branches (edits and regenerations), main line first."""
    out = [m.text for m in conv.messages if m.role == "user" and m.kind == "message" and (m.text or "").strip()]
    seen = set(out)
    for branch in conv.branches:
        for m in branch:
            if m.role == "user" and m.kind == "message" and (m.text or "").strip() and m.text not in seen:
                seen.add(m.text)
                out.append(m.text)
    return out


_CONFIRM = re.compile(r"^\s*(对|对的|是的|没错|正确|就是这样|yes|yep|yeah|exactly|correct|right|that's right)[\s。.!！,，]*$",
                      re.I)


def confirmed_summaries(conv: Conversation, limit: int = 600) -> list:
    """Assistant statements you explicitly confirmed ("so your stack is X and Y?" → "yes"). Only these assistant
    words are used, and only by LLM extraction, marked as confirmed by you."""
    out = []
    msgs = [m for m in conv.messages if m.kind == "message" and m.role in ("user", "assistant")]
    for prev, cur in zip(msgs, msgs[1:]):
        if prev.role == "assistant" and cur.role == "user" and _CONFIRM.match(cur.text or ""):
            out.append("[An assistant said this and I confirmed it] " + (prev.text or "")[-limit:])
    return out


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
            texts = texts + confirmed_summaries(conv)
            pairs = llm_candidates(llm, texts) if texts else []
            conf = LLM_CONFIDENCE
        else:
            pairs = [p for t in texts for p in rule_candidates(t)]
            conf = RULE_CONFIDENCE
        for text, cat in pairs:
            mem = MemoryItem(text=text, platform=conv.platform, category=cat, source_ref=cid, confidence=conf,
                             status="candidate", scope=scope)
            if store.upsert_memory(mem, seen_at=conv.updated_at or conv.created_at) == "new":
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


# Categories where a newer, related fact usually replaces an older one (a new editor, a new job, a new project).
REPLACEABLE = ("identity", "work", "project", "tooling")
RELATED = 0.35          # similar enough to be about the same thing …
STALE_GAP = 180 * 86400  # … and mentioned this much later


def possibly_outdated(memories: list) -> dict:
    """{id of an older memory: text of the newer related one}. Only a hint for review, never applied."""
    from .models import similarity
    from .store import NEAR_DUPLICATE
    out: dict = {}
    groups: dict = {}
    for m in memories:
        if m.category in REPLACEABLE and m.last_seen:
            groups.setdefault((m.scope, m.category), []).append(m)
    for items in groups.values():
        for old in items:
            for new in items:
                if new is old or new.last_seen - old.last_seen < STALE_GAP:
                    continue
                if RELATED <= similarity(old.text, new.text) < NEAR_DUPLICATE:
                    out[old.id] = new.text
                    break
    return out


def _review_note(m, outdated: dict) -> str:
    bits = []
    if m.status != "accepted":
        bits.append(f"candidate {m.platform} {m.confidence:.1f}")
    if (m.seen_count or 1) > 1:
        bits.append(f"seen in {m.seen_count} conversations")
    if m.last_seen:
        bits.append(f"last {time.strftime('%Y-%m-%d', time.localtime(m.last_seen))}")
    if m.id in outdated:
        newer = " ".join(outdated[m.id].split())[:80].replace("--", "–").replace(">", "›")
        bits.append("maybe outdated, newer: " + newer)
    return f"  <!-- {'; '.join(bits)} -->" if bits else ""


def render_review(memories: list) -> str:
    outdated = possibly_outdated(memories)
    lines = [REVIEW_HEADER]
    by_scope: dict = {}
    for m in memories:
        by_scope.setdefault(m.scope, []).append(m)
    for scope in sorted(by_scope, key=lambda s: (s != "global", s)):
        lines.append(f"## {scope}\n")
        for m in sorted(by_scope[scope], key=lambda x: (x.status != "accepted", x.category, -(x.seen_count or 1),
                                                        -x.confidence)):
            box = "x" if m.status == "accepted" else " "
            text = " ".join(m.text.split())
            lines.append(f"- [{box}] {text} <!-- {m.id} {m.category} -->" + _review_note(m, outdated))
        lines.append("")
    return "\n".join(lines)


def parse_review(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = re.sub(r"(<!--\s*[0-9a-f]{16}\s+[\w:-]+\s*-->)\s*<!--[^>]*-->\s*$", r"\1", line)
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
