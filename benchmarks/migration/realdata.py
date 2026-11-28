"""Real-data study on the author's own ctxport store (N = 1; only aggregate numbers leave the machine).

    python benchmarks/migration/realdata.py bank      # sample self-statements per year, draft questions
    python benchmarks/migration/realdata.py label     # write label.html: the user confirms / edits every answer
    python benchmarks/migration/realdata.py contexts  # every method's context at each budget
    python benchmarks/migration/realdata.py read --readers claude:haiku,...
    python benchmarks/migration/realdata.py judge     # LLM judge vs the user's gold answers
    python benchmarks/migration/realdata.py report    # accuracy by method × year of first mention

Everything is written under ~/.ctxport/research/real/ (private, never committed). Questions are drafted from the
user's raw messages, not from CHMD's facts, so the bank does not favour the method under test; the user can add
questions of their own in the labeling page as a second, extraction-independent set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
import pipeline as P  # noqa: E402
from ctxport import distill as D  # noqa: E402
from ctxport.extract import is_sensitive, user_texts  # noqa: E402
from ctxport.store import Store  # noqa: E402

HOME = Path.home() / ".ctxport"
OUT = HOME / "research" / "real"
BUDGETS = (1400, 4000)
YEARS = ("2023", "2024", "2025", "2026")


def _year(ts) -> str:
    return time.strftime("%Y", time.localtime(ts)) if ts else "?"


def real_messages(store: Store) -> list:
    """[(ts, platform, conv_id, lean text)] of the user's own messages, oldest first."""
    out = []
    for r in store.conversation_rows(order="created_at ASC"):
        c = store.get_conversation(r["id"])
        if c is None or c.meta.get("is_subagent"):
            continue
        mine = set(user_texts(c))
        for m in c.messages:
            if m.role == "user" and m.kind == "message" and m.text in mine:
                t = " ".join(D.lean_text(m.text).split())
                if t:
                    out.append((m.created_at or c.created_at or 0, c.platform, c.id, t))
    out.sort(key=lambda x: x[0])
    return out


BROAD = re.compile(r"我|\bI\b|\bI'm\b|\bmy\b", re.I)

DRAFT_PROMPT = """You help build a memory test about ONE person from messages they wrote to AI assistants. For each
numbered message below decide whether it states a durable fact about the writer themself (identity, background,
school or work, courses, places, people in their life, tools or devices they use, projects, plans or goals,
preferences about how assistants should answer). Skip greetings, task requests with no personal fact, homework
text, quoted material, hypotheticals and facts about other people. For each kept message write ONE short-answer
question in Chinese that asks the person about that fact as if a new assistant were checking what it knows
("我用的是什么型号的电脑？"), and the answer exactly as the message states it (short). Never ask for passwords, ID or
account numbers, exact addresses, health, sexual or criminal information. Return ONLY JSON:
{"items": [{"n": message number, "question": "...", "answer": "...", "kind": "identity|study|work|place|people|tool|project|goal|preference|other"}]}"""


def cmd_bank(a) -> None:
    store = Store(HOME / "ctxport.db")
    msgs = real_messages(store)
    store.close()
    rng = random.Random(a.seed)
    by_year = defaultdict(list)
    for ts, plat, cid, t in msgs:
        # early years have few messages, so any first-person message is a candidate there
        pat = BROAD if _year(ts) in ("2023", "2024") else D._SELF
        if 12 <= len(t) <= 500 and pat.search(t) and not is_sensitive(t):
            by_year[_year(ts)].append((ts, plat, cid, t))
    cand = []
    for y in YEARS:
        pool = by_year.get(y, [])
        rng.shuffle(pool)
        cand += pool if y in ("2023", "2024") else pool[: a.per_year * 4]   # early years: use every candidate
    batches = [cand[i:i + 25] for i in range(0, len(cand), 25)]

    def draft(batch):
        body = "\n".join(f"[{k}] ({time.strftime('%Y-%m', time.localtime(ts))}) {t}" for k, (ts, _, _, t)
                         in enumerate(batch, 1))
        try:
            r = llm.call(a.model, body, system=llm.STRICT + "\n\n" + DRAFT_PROMPT)
            items = llm.parse_json(r["text"]).get("items") or []
        except Exception:  # noqa: BLE001
            return []
        out = []
        for it in items:
            try:
                ts, plat, cid, t = batch[int(it["n"]) - 1]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            q, ans = str(it.get("question") or "").strip(), str(it.get("answer") or "").strip()
            if q and ans and not is_sensitive(q + " " + ans):
                out.append({"question": q, "answer": ans, "kind": it.get("kind", "other"), "date": ts,
                            "year": _year(ts), "platform": plat, "conversation": cid, "message": t[:400]})
        return out

    with ThreadPoolExecutor(6) as ex:
        drafted = [x for xs in ex.map(draft, batches) for x in xs]
    seen, bank = set(), []
    for it in drafted:
        key = re.sub(r"\W+", "", it["question"])
        if key in seen:
            continue
        seen.add(key)
        bank.append(it)
    final = []
    for y in YEARS:
        ys = [b for b in bank if b["year"] == y]
        rng.shuffle(ys)
        final += ys[: a.per_year]
    for it in final:
        it["id"] = "r" + hashlib.sha256((it["question"] + str(it["date"])).encode()).hexdigest()[:8]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "bank.json").write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"self_statements": {y: len(by_year.get(y, [])) for y in YEARS}, "drafted": len(drafted),
                      "bank": dict(Counter(b["year"] for b in final))}, ensure_ascii=False))


LABEL_HTML = r"""<!doctype html><html lang="zh"><meta charset="utf-8"><title>记忆迁移实验 · 标注</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a66;--line:#e4e2dc;--ok:#1f7a4d;--warn:#b26b00;--bad:#b3261e;--acc:#2b59c3}
*{box-sizing:border-box}body{margin:0;font:15px/1.55 -apple-system,"PingFang SC",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;z-index:5;background:rgba(246,245,242,.94);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:14px 24px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:650}.stat{color:var(--mute);font-size:13px}.bar{flex:1;min-width:160px;height:6px;background:var(--line);border-radius:3px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ok);width:0}
button{font:inherit;border:1px solid var(--line);background:var(--card);border-radius:8px;padding:6px 12px;cursor:pointer}button:hover{border-color:#bbb}
button.primary{background:var(--ink);color:#fff;border-color:var(--ink)}
main{max-width:920px;margin:0 auto;padding:20px 24px 80px}.intro{color:var(--mute);font-size:14px;margin:4px 0 18px}
.filters{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}.filters button.on{background:var(--ink);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px}
.card.done{border-left:4px solid var(--ok)}.card.stale{border-left:4px solid var(--warn)}.card.drop{opacity:.5;border-left:4px solid var(--bad)}
.meta{font-size:12px;color:var(--mute);display:flex;gap:10px;margin-bottom:6px}.src{font-size:13px;color:var(--mute);background:#faf9f6;border-radius:8px;padding:8px 10px;margin:8px 0;white-space:pre-wrap}
label{font-size:12px;color:var(--mute);display:block;margin-top:8px}input[type=text]{width:100%;font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px}
.acts{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}.acts button.sel{outline:2px solid var(--acc)}
.add{background:var(--card);border:1px dashed #c9c6be;border-radius:12px;padding:16px 18px;margin-top:24px}
</style>
<header><h1>记忆迁移实验 · 标注</h1><div class="bar"><i id="prog"></i></div><span class="stat" id="stat"></span>
<button onclick="exportJson()" class="primary">导出 gold.json</button></header>
<main>
<p class="intro">每道题都是从你自己某条消息里起草的。请判断标准答案：<b>对，现在仍然成立</b>；<b>当时对，现在变了</b>（填现在的值，旧值会作为“过时答案”）；或<b>删掉</b>（题目不对、太琐碎、不想用）。可以直接改题目和答案，多个可接受写法用 / 分隔。进度自动保存在本浏览器；做完点右上角导出，把文件放到 ~/.ctxport/research/real/gold.json。页面只在本机打开，不联网。</p>
<div class="filters" id="filters"></div>
<div id="list"></div>
<div class="add"><b>我自己出几道题</b>（不看上面，凭你觉得“新助手应该知道我的事”来出，可选年份）
<label>题目</label><input type="text" id="nq"><label>答案（多个写法用 / 分隔）</label><input type="text" id="na">
<label>这件事大约是哪一年开始成立的</label><input type="text" id="ny" placeholder="2023 / 2024 / 2025 / 2026">
<div class="acts"><button onclick="addOwn()">添加</button></div><div id="own"></div></div>
</main>
<script>
const BANK = __BANK__;
const KEY = "ctxport-label-v1";
let S = JSON.parse(localStorage.getItem(KEY) || "{}"); S.items = S.items || {}; S.own = S.own || [];
let filter = "all";
function save(){localStorage.setItem(KEY, JSON.stringify(S)); stat();}
function st(id){return S.items[id] || {};}
function set(id, patch){S.items[id] = Object.assign({}, st(id), patch); save(); render();}
function stat(){const n=BANK.length, d=BANK.filter(b=>st(b.id).status).length;
  document.getElementById("stat").textContent=`${d} / ${n} 已标注 · 自出 ${S.own.length} 题`;
  document.getElementById("prog").style.width=(100*d/n)+"%";}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function render(){
  const years=[...new Set(BANK.map(b=>b.year))].sort();
  document.getElementById("filters").innerHTML=["all","todo",...years].map(f=>`<button class="${f===filter?'on':''}" onclick="filter='${f}';render()">${f==="all"?"全部":f==="todo"?"未标注":f}</button>`).join("");
  const items=BANK.filter(b=>filter==="all"||(filter==="todo"?!st(b.id).status:b.year===filter));
  document.getElementById("list").innerHTML=items.map(b=>{const s=st(b.id);const cls=s.status==="ok"?"done":s.status==="stale"?"stale":s.status==="drop"?"drop":"";
  return `<div class="card ${cls}"><div class="meta"><span>${b.year}</span><span>${new Date(b.date*1000).toISOString().slice(0,7)}</span><span>${esc(b.platform)}</span><span>${esc(b.kind)}</span></div>
  <div class="src">${esc(b.message)}</div>
  <label>题目</label><input type="text" value="${esc(s.question??b.question)}" onchange="set('${b.id}',{question:this.value})">
  <label>标准答案（当时消息里的说法）</label><input type="text" value="${esc(s.answer??b.answer)}" onchange="set('${b.id}',{answer:this.value})">
  ${s.status==="stale"?`<label>现在的值（问“现在”时的正确答案）</label><input type="text" value="${esc(s.current||"")}" onchange="set('${b.id}',{current:this.value})">`:""}
  <div class="acts"><button class="${s.status==='ok'?'sel':''}" onclick="set('${b.id}',{status:'ok'})">对，现在仍然成立</button>
  <button class="${s.status==='stale'?'sel':''}" onclick="set('${b.id}',{status:'stale'})">当时对，现在变了</button>
  <button class="${s.status==='drop'?'sel':''}" onclick="set('${b.id}',{status:'drop'})">删掉</button></div></div>`}).join("");
  document.getElementById("own").innerHTML=S.own.map((o,i)=>`<div class="card done"><div class="meta"><span>${esc(o.year||"?")}</span><span>自出</span></div>${esc(o.question)} → <b>${esc(o.answer)}</b> <button onclick="S.own.splice(${i},1);save();render()">删</button></div>`).join("");
  stat();}
function addOwn(){const q=nq.value.trim(),a=na.value.trim();if(!q||!a)return;S.own.push({question:q,answer:a,year:ny.value.trim()});nq.value=na.value=ny.value="";save();render();}
function exportJson(){const out=BANK.map(b=>{const s=st(b.id);return {id:b.id,year:b.year,date:b.date,platform:b.platform,kind:b.kind,
  question:s.question??b.question,answer:s.answer??b.answer,current:s.current||null,status:s.status||null,source:"message"}});
  S.own.forEach((o,i)=>out.push({id:"own"+i,year:o.year||null,question:o.question,answer:o.answer,status:"ok",source:"own"}));
  const blob=new Blob([JSON.stringify(out,null,1)],{type:"application/json"});const u=URL.createObjectURL(blob);
  const a=document.createElement("a");a.href=u;a.download="gold.json";a.click();}
render();
</script></html>"""


def cmd_label(a) -> None:
    bank = json.loads((OUT / "bank.json").read_text(encoding="utf-8"))
    html = LABEL_HTML.replace("__BANK__", json.dumps(bank, ensure_ascii=False).replace("</", "<\\/"))
    (OUT / "label.html").write_text(html, encoding="utf-8")
    print(OUT / "label.html")


# ------------------------------------------------------------------ contexts

def cmd_contexts(a) -> None:
    store = Store(HOME / "ctxport.db")
    msgs = real_messages(store)
    native = [m.text for m in store.memories(status=None, platform="chatgpt")]
    store.close()
    facts = json.loads((HOME / "distilled" / "facts.json").read_text(encoding="utf-8"))
    canon = facts["l2"]

    class Fake:   # pipeline helpers expect a Persona
        def user_messages(self):
            return [(ts, plat, t) for ts, plat, _, t in msgs]

    fp = Fake()
    cost = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
    ctx = {"none": "", "chatgpt-native-memory": "\n".join(f"- {t}" for t in native)}
    for f in sorted(OUT.glob("b1-*.txt")):           # self-reports the user pasted from the old assistants
        ctx[f.stem] = f.read_text(encoding="utf-8").strip()
    for b in BUDGETS:
        ctx[f"recent@{b}"] = P.recent(fp, b)
        ctx[f"chmd-detT@{b}"], _ = P.render(canon, b, True)
        ctx[f"chmd-write@{b}"] = P.chmd_write(canon, b, a.model, log=cost[f"chmd-write@{b}"])
        ctx[f"summary@{b}"] = P.summary(fp, b, a.model, log=cost[f"summary@{b}"])
    # an assistant's memory maintained year by year (old profile + that year's messages -> new profile)
    years = sorted({_year(ts) for ts, *_ in msgs})
    prof = ""
    for y in years:
        part = [(ts, plat, t) for ts, plat, _, t in msgs if _year(ts) == y]
        if not prof:
            prof = P.summary(fp, 1400, a.model, log=cost["rolling@1400"], msgs=part)
        else:
            body = f"CURRENT PROFILE:\n{prof}\n\nNEW MESSAGES:\n{P.dated_text(part)}"
            if len(body) > P.SUMMARY_CHUNK:
                # the new year does not fit: notes per chunk first, as summary() does
                notes = P.summary(fp, 4000, a.model, log=cost["rolling@1400"], msgs=part)
                body = f"CURRENT PROFILE:\n{prof}\n\nNEW MESSAGES (condensed notes):\n{notes}"
            r = llm.call(a.model, body, system=llm.STRICT + "\n\n" + P.UPDATE_PROMPT.format(budget=1400))
            prof = P._fit(r["text"].strip(), 1400)
        ctx[f"rolling-{y}@1400"] = prof
    ctx["rolling@1400"] = prof
    bank = _gold()
    sents = []
    for ts, _, _, t in msgs:
        for s in re.split(r"(?<=[。！？!?.])\s*", t):
            if len(s.strip()) > 3:
                sents.append(f"[{P._day(ts)}] {s.strip()}")
    idx = P.BM25(sents)
    bm = {}
    for b in BUDGETS:
        m = {}
        for q in bank:
            text, used = [], 0
            for i in idx.rank(q["question"]):
                s = sents[i] + "\n"
                if used + len(s) > b:
                    continue
                text.append(s)
                used += len(s)
            m[q["id"]] = "".join(text)
        bm[f"bm25@{b}"] = m
    (OUT / "contexts.json").write_text(json.dumps({"contexts": ctx, "bm25": bm, "cost": cost}, ensure_ascii=False),
                                       encoding="utf-8")
    print({k: len(v) for k, v in ctx.items()})


def _gold() -> list:
    f = OUT / "gold.json"
    items = json.loads(f.read_text(encoding="utf-8")) if f.exists() else json.loads(
        (OUT / "bank.json").read_text(encoding="utf-8"))
    out = []
    for it in items:
        if it.get("status") == "drop":
            continue
        q = {"id": it["id"], "year": it.get("year"), "source": it.get("source", "message"),
             "question": it["question"], "answer": it["answer"], "current": it.get("current"),
             "labeled": bool(it.get("status"))}
        out.append(q)
    return out


def cmd_read(a) -> None:
    d = json.loads((OUT / "contexts.json").read_text(encoding="utf-8"))
    bank = _gold()
    qs = [{"id": q["id"], "question": {"zh": q["question"]}} for q in bank]
    rows_f = OUT / "answers.jsonl"
    done = set()
    if rows_f.exists():
        done = {(r["method"], r["reader"]) for r in map(json.loads, open(rows_f, encoding="utf-8"))}
    methods = a.methods.split(",") if a.methods else list(d["contexts"]) + list(d["bm25"])
    jobs = [(m, r) for r in a.readers.split(",") for m in methods if (m, r) not in done]

    def job(mr):
        m, reader = mr
        if m in d["bm25"]:
            ans = P.read_each(reader, d["bm25"][m], qs, lang="zh")
        else:
            ans = P.read(reader, d["contexts"][m], qs, lang="zh", batch=40)
        return [{"method": m, "reader": reader, "qid": q["id"], "answer": ans.get(q["id"])} for q in qs]

    with ThreadPoolExecutor(a.workers) as ex, open(rows_f, "a", encoding="utf-8") as fh:
        for rows in ex.map(job, jobs):
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"[read] {rows[0]['method']} {rows[0]['reader']}", flush=True)


JUDGE_PROMPT = """You grade answers in a memory test about one person. For each item you get the question, the gold
answer (several acceptable forms may be separated by /), optionally the CURRENT value when the fact has since
changed (then either the gold or the current value counts as correct unless the question asks for "now"/"现在",
in which case only the current value is correct), and a model's answer. Grade each as "correct" (same fact, wording
may differ), "abstain" (says it does not know / UNKNOWN) or "wrong" (any other answer, including partly wrong or a
different value). Return ONLY JSON {"grades": {"<id>": "correct|abstain|wrong"}}."""


def cmd_judge(a) -> None:
    bank = {q["id"]: q for q in _gold()}
    rows = [r for r in map(json.loads, open(OUT / "answers.jsonl", encoding="utf-8")) if r["qid"] in bank]
    out_f = OUT / "graded.jsonl"
    graded = {}
    if out_f.exists():
        for r in map(json.loads, open(out_f, encoding="utf-8")):
            graded[(r["method"], r["reader"], r["qid"])] = r
    todo = [r for r in rows if (r["method"], r["reader"], r["qid"]) not in graded]
    batches = [todo[i:i + 30] for i in range(0, len(todo), 30)]

    def job(batch):
        items = []
        for k, r in enumerate(batch):
            q = bank[r["qid"]]
            items.append(f'#{k}: Q: {q["question"]}\n   gold: {q["answer"]}' +
                         (f'\n   current: {q["current"]}' if q.get("current") else "") +
                         f'\n   answer: {r["answer"] if r["answer"] is not None else "(none)"}')
        try:
            res = llm.call(a.model, "\n".join(items), system=llm.STRICT + "\n\n" + JUDGE_PROMPT)
            g = llm.parse_json(res["text"]).get("grades") or {}
        except Exception:  # noqa: BLE001
            g = {}
        return [dict(r, grade=str(g.get(f"#{k}", g.get(str(k), ""))).strip("# ").lower() or None)
                for k, r in enumerate(batch)]

    with ThreadPoolExecutor(6) as ex, open(out_f, "a", encoding="utf-8") as fh:
        for rs in ex.map(job, batches):
            for r in rs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"graded {len(todo)} new answers")


def cmd_report(a) -> None:
    bank = {q["id"]: q for q in _gold()}
    rows = [r for r in map(json.loads, open(OUT / "graded.jsonl", encoding="utf-8")) if r["qid"] in bank]
    if a.labeled_only:
        rows = [r for r in rows if bank[r["qid"]]["labeled"]]
    d = json.loads((OUT / "contexts.json").read_text(encoding="utf-8"))
    by = defaultdict(list)
    for r in rows:
        by[(r["reader"], r["method"])].append(r)
    years = [y for y in YEARS if any(q["year"] == y for q in bank.values())]
    lines = ["# Real-data study (N = 1, author's own history)", "",
             f"questions: {len({r['qid'] for r in rows})} "
             f"({', '.join(f'{y}: {sum(q[chr(121)+chr(101)+chr(97)+chr(114)] == y for q in bank.values())}' for y in years)})",
             ""]
    rng = random.Random(0)
    for reader in sorted({r["reader"] for r in rows}):
        lines += [f"## Reader: {reader}", "", "| method | chars | acc | 95% CI | " + " | ".join(years) +
                  " | wrong | abstain |", "|" + "---|" * (6 + len(years))]
        for (rd, m), rs in sorted(by.items()):
            if rd != reader:
                continue
            n = len(rs)
            acc = sum(r["grade"] == "correct" for r in rs) / n
            boots = sorted(sum(x["grade"] == "correct" for x in (rng.choice(rs) for _ in rs)) / n for _ in range(4000))
            cells = []
            for y in years:
                ys = [r for r in rs if bank[r["qid"]]["year"] == y]
                cells.append(f"{100 * sum(r['grade'] == 'correct' for r in ys) / len(ys):.0f}" if ys else "–")
            z = d["contexts"].get(m)
            chars = len(z) if z is not None else sum(len(v) for v in d["bm25"].get(m, {}).values()) / max(1, len(d["bm25"].get(m, {})))
            lines.append(f"| {m} | {chars:.0f} | {100 * acc:.1f} | [{100 * boots[100]:.1f}, {100 * boots[3899]:.1f}] | "
                         + " | ".join(cells) + f" | {100 * sum(r['grade'] == 'wrong' for r in rs) / n:.1f} | "
                         f"{100 * sum(r['grade'] == 'abstain' for r in rs) / n:.1f} |")
        lines.append("")
    lines.append("CI: question-level bootstrap (one person, so questions are the only unit; not a population claim).")
    text = "\n".join(lines)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bank")
    b.add_argument("--per-year", type=int, default=80)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--model", default="claude:haiku")
    sub.add_parser("label")
    c = sub.add_parser("contexts")
    c.add_argument("--model", default="claude:haiku")
    r = sub.add_parser("read")
    r.add_argument("--readers", default="claude:haiku")
    r.add_argument("--methods", default="")
    r.add_argument("--workers", type=int, default=3)
    j = sub.add_parser("judge")
    j.add_argument("--model", default="claude:haiku")
    rp = sub.add_parser("report")
    rp.add_argument("--labeled-only", action="store_true")
    a = ap.parse_args(argv)
    {"bank": cmd_bank, "label": cmd_label, "contexts": cmd_contexts, "read": cmd_read, "judge": cmd_judge,
     "report": cmd_report}[a.cmd](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
