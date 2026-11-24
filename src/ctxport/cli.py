"""ctxport command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .adapters import exporters, get_exporter, importers
from .adapters._render import render_message_md, render_transcript, visible
from .adapters.base import ExportOptions
from .models import MemoryItem
from .redact import redact
from .store import TOOL_KEEP, AmbiguousId, NewerDatabase, Store, default_home
from .utils import fmt_time, parse_since, write_managed


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def info(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def open_store(args) -> Store:
    if getattr(args, "keep_raw", False):
        os.environ["CTXPORT_STORE_RAW"] = "1"
    if getattr(args, "keep_full_tools", False):
        os.environ["CTXPORT_KEEP_FULL_TOOLS"] = "1"
    return Store(Path(args.db).expanduser() if getattr(args, "db", None) else None)


# ---------------------------------------------------------------- commands

def cmd_platforms(args) -> int:
    print("Import sources / 导入来源\n")
    for imp in importers():
        roots = imp.default_roots()
        found = [str(r) for r in roots if Path(r).exists()]
        status = ("✔ " + ", ".join(found)) if found else ("— not found" if roots else "export file")
        exp = " (experimental)" if imp.experimental else ""
        print(f"  {imp.name:16} {imp.label}{exp}\n  {'':16} {status}\n  {'':16} how: {imp.how_to_get}\n")
    print("Export targets / 导出目标\n")
    for ex in exporters():
        print(f"  {ex.name:16} {ex.label}")
    return 0


def _print_report(r) -> None:
    info(f"[{r.platform}] units {r.units} · imported {r.imported} · unchanged {r.skipped} · "
         f"conversations {r.conversations} · messages {r.messages} · new memories {r.memories_new} · "
         f"warnings {len(r.warnings)}")
    for w in r.warnings[:20]:
        info(f"    ! {w}")
    if len(r.warnings) > 20:
        info(f"    … {len(r.warnings) - 20} more warnings")


def cmd_sync(args) -> int:
    from .pipeline import sync
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    with open_store(args) as s:
        reports = sync(s, only=only, force=args.force, progress=info)
        if not reports:
            info("No local sources detected. Run `ctxport platforms` to see where ctxport looks.")
        for r in reports:
            _print_report(r)
        if args.extract:
            from .extract import coverage_report, format_coverage, run_extract
            info(f"extract: {run_extract(s, progress=info)}")
            info(format_coverage(coverage_report(s)))
        _hint_compact(s)
    return 0


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _hint_compact(s: Store) -> None:
    r = s.size_report()
    if r["pages"] > 256 and (r["free_ratio"] > 0.25 or r["trimmable_tool_messages"] > 100):
        info(f"tip: {r['free_ratio']:.0%} of the database is free space and {r['trimmable_tool_messages']} tool outputs "
             "can be trimmed; `ctxport compact` makes the file smaller.")


def cmd_import(args) -> int:
    from .pipeline import import_path
    with open_store(args) as s:
        r = import_path(s, args.platform, Path(args.path) if args.path else None, force=args.force, progress=info)
        _print_report(r)
        return 0 if r.units else 1


def _llm_client(args):
    from .llm import LLMClient
    return LLMClient(base_url=args.base_url or "", model=args.model or "", allow_remote=args.allow_remote)


def cmd_extract(args) -> int:
    from .extract import run_extract
    from .llm import RemoteNotAllowed
    try:
        llm = _llm_client(args) if args.llm else None
    except RemoteNotAllowed as e:
        info(str(e))
        return 2
    from .extract import coverage_report, format_coverage
    with open_store(args) as s:
        st = run_extract(s, llm, platform=args.platform, days=args.days, limit=args.limit, progress=info)
        info(f"extract[{st['method']}]: {st['conversations']} conversations processed, {st['skipped']} already done, "
             f"{st['new']} new candidates. Review with `ctxport memories edit`.")
        info(format_coverage(coverage_report(s)))
    return 0


def cmd_memories(args) -> int:
    from .extract import apply_review, parse_review, render_review
    with open_store(args) as s:
        a = args.action
        if a == "list":
            status = None if args.all else (("accepted", "candidate") if args.candidates else ("accepted",))
            for m in s.memories(status=status, platform=args.platform):
                seen = f"×{m.seen_count}" if (m.seen_count or 1) > 1 else ""
                print(f"{m.id}  [{m.status[:4]}] {m.scope:>10} {m.category:<18} {seen:>4} {redact(m.text)[:160]}")
            return 0
        if a == "dedupe":
            print(f"{s.dedupe_memories()} near-duplicate memories merged")
            return 0
        if a == "coverage":
            from .extract import coverage_report, format_coverage
            cov = coverage_report(s)
            print(json.dumps(cov, ensure_ascii=False, indent=2) if args.json else format_coverage(cov))
            return 0
        if a == "add":
            text = " ".join(args.text)
            r = s.upsert_memory(MemoryItem(text=text, platform="manual", category=args.category, status="accepted",
                                           scope=f"project:{args.project}" if args.project else "global"))
            s.commit()
            print(r)
            return 0
        if a in ("accept", "reject"):
            target = "accepted" if a == "accept" else "rejected"
            if args.all_candidates:
                n = 0
                for m in s.memories(status=("candidate",)):
                    n += s.set_memory_status(m.id, target)
            else:
                try:  # resolve every id first, so an ambiguous one changes nothing
                    ids = [s.resolve_memory_id(i) for i in args.ids]
                except AmbiguousId as e:
                    info(str(e))
                    return 2
                for i, rid in zip(args.ids, ids):
                    if rid is None:
                        info(f"no memory with id {i!r}")
                n = sum(s.set_memory_status(rid, target) for rid in dict.fromkeys(ids) if rid)
            print(f"{n} memories → {target}")
            return 0
        if a == "rm":
            try:
                ids = [s.resolve_memory_id(i) for i in args.ids]
            except AmbiguousId as e:
                info(str(e))
                return 2
            for i, rid in zip(args.ids, ids):
                if rid is None:
                    info(f"no memory with id {i!r}")
            print(f"{sum(s.delete_memory(rid) for rid in dict.fromkeys(ids) if rid)} deleted")
            return 0
        if a == "edit":
            path = Path(args.file)
            if path.exists() and not args.reset:
                st = apply_review(s, parse_review(path.read_text(encoding="utf-8")))
                print(f"applied {path}: {st}")
                path.unlink()
                print("Review file removed. Run `ctxport memories edit` again to start a new review.")
                return 0
            mems = s.memories(status=("accepted", "candidate"))
            path.write_text(render_review(mems), encoding="utf-8")
            print(f"Wrote {path} ({len(mems)} items). Edit it, then run `ctxport memories edit` again to apply.")
            return 0
    return 1


def _export_opts(args) -> ExportOptions:
    return ExportOptions(
        out_dir=Path(args.out), apply=args.apply, project=Path(args.project) if args.project else None,
        platform=args.platform, limit=args.limit, since=parse_since(args.since),
        include_candidates=args.include_candidates, include_tools=args.include_tools,
        include_reasoning=args.include_reasoning, redact=not args.no_redact, include_raw=args.include_raw)


def run_export(store: Store, target: str, opts: ExportOptions) -> list:
    ex = get_exporter(target)
    status = ("accepted", "candidate") if opts.include_candidates else ("accepted",)
    memories = store.memories(status=status)
    files = ex.render(store, memories, opts)
    written = []
    for f in files:
        p = opts.out_dir / f.rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content, encoding="utf-8")
        written.append(str(p))
    if opts.apply:
        if not ex.appliable:
            info(f"[{target}] --apply is not available for this target (nothing is written outside {opts.out_dir}).")
        for f in files:
            if f.apply_path and f.managed:
                from .utils import BLOCK_BEGIN, BLOCK_END
                body = f.content.split("\n", 1)[1].rsplit(BLOCK_END, 1)[0] if f.content.startswith(BLOCK_BEGIN) else f.content
                r = write_managed(Path(f.apply_path), body, target)
                info(f"[{target}] updated {r['path']}" + (f" (backup {r['backup']})" if r.get("backup") else ""))
    info(f"[{target}] {len(files)} file(s) → {opts.out_dir.resolve()}")
    if not opts.apply or not ex.appliable:
        info(f"[{target}] {ex.instructions}")
    return written


def cmd_export(args) -> int:
    with open_store(args) as s:
        for t in args.target.split(","):
            run_export(s, t.strip(), _export_opts(args))
    return 0


def cmd_migrate(args) -> int:
    from .extract import run_extract
    from .pipeline import import_path
    with open_store(args) as s:
        r = import_path(s, args.source, Path(args.path) if args.path else None, force=args.force, progress=info)
        _print_report(r)
        info(f"extract: {run_extract(s, platform=args.source, progress=info)}")
        n_acc = len(s.memories(status=("accepted",)))
        n_cand = len(s.memories(status=("candidate",)))
        info(f"memories: {n_acc} accepted, {n_cand} candidates (only accepted are exported unless "
             f"--include-candidates; review with `ctxport memories edit`).")
        for t in args.target.split(","):
            run_export(s, t.strip(), _export_opts(args))
    return 0


def cmd_list(args) -> int:
    with open_store(args) as s:
        for r in s.conversation_rows(platform=args.platform, limit=args.limit, since=parse_since(args.since)):
            print(f"{r['id'][:60]:60}  {fmt_time(r['updated_at'])}  {r['message_count']:>5}  {(r['title'] or '')[:70]}")
    return 0


def _resolve(s: Store, ident: str) -> Optional[str]:
    cid = s.find_conversation_id(ident)
    if not cid:
        info(f"conversation not found or ambiguous: {ident}")
    return cid


def cmd_show(args) -> int:
    with open_store(args) as s:
        cid = _resolve(s, args.id)
        if not cid:
            return 1
        conv = s.get_conversation(cid)
        print(f"# {conv.title}\n{conv.id} · {conv.project or '-'} · {conv.model or '-'} · "
              f"{fmt_time(conv.created_at)} → {fmt_time(conv.updated_at)}\n")
        msgs = visible(conv.messages, args.include_tools, args.include_reasoning)
        if args.last:
            msgs = msgs[-args.last:]
        for m in msgs:
            print(render_message_md(m, redact=not args.no_redact))
        if args.branches:
            for i, b in enumerate(conv.branches, 1):
                print(f"\n---\n## Branch {i}\n")
                for m in visible(b, args.include_tools, args.include_reasoning):
                    print(render_message_md(m, redact=not args.no_redact))
    return 0


def cmd_search(args) -> int:
    with open_store(args) as s:
        q = " ".join(args.query)
        rows = s.search(q, platform=args.platform, limit=args.limit, include_tools=args.include_tools)
        for r in rows:
            t = redact(r["text"] or "")
            i = max(0, min([t.lower().find(w.lower()) for w in args.query if t.lower().find(w.lower()) >= 0] or [0]) - 60)
            print(f"[{r['platform']}] {fmt_time(r['created_at'])} {r['role']:9} {r['conversation_id'][:50]}\n"
                  f"    {(r['title'] or '')[:60]}\n    …{t[i:i + 220].replace(chr(10), ' ')}…\n")
        if not rows:
            info("no results")
    return 0


HANDOFF_HEADER = """I'm continuing a conversation I started in another AI assistant ({platform}). Below is the context.
Please read it, briefly confirm where we left off, and continue helping me from there.
我在另一个 AI 助手（{platform}）里有一段进行中的对话，下面是上下文，请先简要确认进度，然后接着帮我。

## Conversation: {title}
"""


def cmd_handoff(args) -> int:
    with open_store(args) as s:
        cid = _resolve(s, args.id)
        if not cid:
            return 1
        conv = s.get_conversation(cid)
        msgs = visible(conv.messages)
        mems = s.memories(status=("accepted",))
        parts = [HANDOFF_HEADER.format(platform=conv.platform, title=conv.title)]
        if conv.project:
            parts.append(f"Project / working directory: {conv.project}\n")
        if args.llm:
            from .llm import RemoteNotAllowed
            try:
                llm = _llm_client(args)
            except RemoteNotAllowed as e:
                info(str(e))
                return 2
            earlier = msgs[:-args.last] if len(msgs) > args.last else []
            if earlier:
                summary = llm.chat([{"role": "system", "content": "Summarize this conversation for handing it to "
                                     "another assistant: goals, decisions, current state, open questions. Be concise; "
                                     "use the conversation's language."},
                                    {"role": "user", "content": render_transcript(earlier, True, 24000)}])
                parts.append("### Summary of earlier messages\n" + summary.strip() + "\n")
        elif len(msgs) > args.last:
            parts.append(f"(The first {len(msgs) - args.last} messages are omitted.)\n")
        parts.append("### Recent messages\n" + render_transcript(msgs[-args.last:], redact=True))
        if mems:
            parts.append("\n### About me\n" + "\n".join(f"- {redact(m.text)}" for m in mems[:40]))
        text = "\n".join(parts).rstrip() + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            info(f"handoff prompt written to {args.out}")
        else:
            print(text)
    return 0


def cmd_profile(args) -> int:
    from .profile import build_profile, copy_to_clipboard
    llm = None
    if args.llm:
        from .llm import RemoteNotAllowed
        try:
            llm = _llm_client(args)
        except RemoteNotAllowed as e:
            info(str(e))
            return 2
    with open_store(args) as s:
        try:
            text, st = build_profile(s, include_candidates=args.include_candidates, all_categories=args.all_categories,
                                     topics=args.topics, since=parse_since(args.since), platform=args.platform,
                                     redact=not args.no_redact, max_chars=args.max_chars, llm=llm)
        except RuntimeError as e:  # LLM unreachable or returned an error
            info(f"profile: {e}")
            return 2
    if not st["memories"] and not st["topics"]:
        info("Nothing to summarize yet. Run `ctxport sync --extract` (or `ctxport import …`) first, "
             "or add facts with `ctxport memories add \"I am …\"`.")
        return 1
    if args.out:
        from .store import private_write_text
        private_write_text(Path(args.out), text)
        info(f"profile written to {args.out}")
    copied = False
    if args.copy:
        copied = copy_to_clipboard(text)
        info("copied to the clipboard" if copied else
             "no clipboard tool found (pbcopy / clip / wl-copy / xclip); printing instead")
    if not args.out and not copied:
        print(text, end="")
    info(f"profile: {st['memories']} memories, {st['topics']} recent topics, {st['chars']} characters"
         + (" + an LLM summary" if st["summary"] else "")
         + (f"; {st['omitted']} left out to fit --max-chars" if st.get("omitted") else ""))
    if st["used_candidates"] and not args.include_candidates:
        info("Note: nothing is reviewed yet, so this uses unreviewed candidates. "
             "Check them with `ctxport memories edit` before sharing.")
    info("Paste it into a new assistant: ChatGPT → Settings → Personalization → Custom instructions; "
         "Claude → claude.ai/import-memory or a project's instructions; Gemini → Saved info. "
         "粘贴到新助手的个性化设置或第一条消息里即可。")
    return 0


def cmd_compact(args) -> int:
    with open_store(args) as s:
        before = s.size_report()
        trimmed = 0 if args.keep_tools else s.trim_tool_output()
        s.compact()
        after = s.size_report()
    b, a = sum(before["files"].values()), sum(after["files"].values())
    print(f"{_fmt_bytes(b)} → {_fmt_bytes(a)} (saved {_fmt_bytes(max(0, b - a))}); "
          f"{trimmed} tool outputs trimmed to their first and last {TOOL_KEEP // 1024} KB")
    return 0


def _print_size(s: Store) -> None:
    r = s.size_report()
    total = sum(r["files"].values())
    print(f"Store: {r['path']}  {_fmt_bytes(total)} on disk, {r['free_ratio']:.0%} free space\n")
    print(f"{'platform':16} {'dialogue':>10} {'tools':>10} {'other':>10} {'raw':>10}")
    for p, d in sorted(r["text_bytes"].items()):
        print(f"{p:16} " + " ".join(f"{_fmt_bytes(d.get(k, 0)):>10}" for k in ("dialogue", "tools", "other", "raw")))
    print("\n(text sizes before the search index; the index is roughly 1.3× the dialogue text)")
    if r["trimmable_tool_messages"] or r["free_ratio"] > 0.1:
        print(f"`ctxport compact` would trim {r['trimmable_tool_messages']} tool outputs and reclaim free space.")


def cmd_stats(args) -> int:
    with open_store(args) as s:
        if args.size:
            _print_size(s)
            return 0
        st = s.stats()
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"Store: {s.path}  (FTS5 trigram: {'yes' if s.fts else 'no, LIKE fallback'})\n")
        print(f"{'platform':16} {'convs':>6} {'msgs':>8} {'records':>8} {'branch':>6}  {'first':10}  {'last':10}  top models")
        for p, d in st["platforms"].items():
            models = ", ".join(f"{m} ({n})" for m, n in d["top_models"])
            print(f"{p:16} {d['conversations']:>6} {d['messages']:>8} {d['all_records']:>8} {d['branches']:>6}  "
                  f"{fmt_time(d['first'], False):10}  {fmt_time(d['last'], False):10}  {models}")
        print(f"\nmemories: {st['memories']}   import warnings: {st['warnings']}")
    return 0


def cmd_mcp_config(args) -> int:
    from .mcp_server import mcp_config
    print(mcp_config(args.client))
    return 0


def cmd_serve_mcp(args) -> int:
    from .mcp_server import serve
    if args.db:
        os.environ["CTXPORT_HOME"] = str(Path(args.db).expanduser().parent)
    serve()
    return 0


def cmd_serve_ingest(args) -> int:
    from . import ingest
    if args.reset_pairing:
        print("pairing reset" if ingest.reset_pairing() else "no pairing to reset")
        ingest.print_pairing_code()
        return 0
    if args.pair:
        ingest.print_pairing_code()
        print("The running receiver accepts it right away; pairing replaces any earlier extension.")
        return 0
    if args.autostart == "install":
        print(ingest.autostart_install(args.port))
        if not ingest.is_paired():
            ingest.print_pairing_code()
        return 0
    if args.autostart == "remove":
        print(ingest.autostart_remove())
        return 0
    ingest.serve(args.port, Path(args.db).expanduser() if args.db else None)
    return 0


# ---------------------------------------------------------------- parser

def _add_export_flags(p) -> None:
    p.add_argument("--out", default="ctxport-out", help="preview/output directory (default ./ctxport-out)")
    p.add_argument("--apply", action="store_true", help="also write into the real location (managed block)")
    p.add_argument("--project", help="project directory for CLAUDE.md / AGENTS.md / GEMINI.md")
    p.add_argument("--platform", help="only conversations from these platforms (comma separated)")
    p.add_argument("--limit", type=int)
    p.add_argument("--since", help="e.g. 30d, 12h or 2026-01-01")
    p.add_argument("--include-candidates", action="store_true")
    p.add_argument("--include-tools", action="store_true")
    p.add_argument("--include-reasoning", action="store_true")
    p.add_argument("--include-raw", action="store_true", help="include raw source JSON (json / ctxport-jsonl)")
    p.add_argument("--no-redact", action="store_true")


def _add_llm_flags(p) -> None:
    p.add_argument("--llm", action="store_true", help="use an OpenAI-compatible LLM (default local Ollama)")
    p.add_argument("--model")
    p.add_argument("--base-url")
    p.add_argument("--allow-remote", action="store_true", help="allow a non-localhost LLM endpoint")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctxport", description="Local-first migration of AI chat history and memory.")
    ap.add_argument("--version", action="version", version=f"ctxport {__version__}")
    ap.add_argument("--db", help=f"store path (default {default_home() / 'ctxport.db'}; or set CTXPORT_HOME)")
    ap.add_argument("--keep-raw", action="store_true", help="also store raw source JSON for each message")
    ap.add_argument("--keep-full-tools", action="store_true",
                    help=f"store tool output in full (default: first and last {TOOL_KEEP // 1024} KB of long outputs)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("platforms", help="supported platforms and what was detected here").set_defaults(fn=cmd_platforms)

    p = sub.add_parser("sync", help="incrementally import every detected local source")
    p.add_argument("--only", help="comma separated platform names")
    p.add_argument("--force", action="store_true")
    p.add_argument("--extract", action="store_true", help="run rule-based memory extraction afterwards")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("import", help="import one platform (export file or local folder)")
    p.add_argument("platform")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("extract", help="extract memory candidates")
    _add_llm_flags(p)
    p.add_argument("--platform")
    p.add_argument("--days", type=int)
    p.add_argument("--limit", type=int)
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("memories", help="review and manage memories")
    msub = p.add_subparsers(dest="action", required=True)
    q = msub.add_parser("list")
    q.add_argument("--candidates", action="store_true", help="include candidates")
    q.add_argument("--all", action="store_true", help="include rejected too")
    q.add_argument("--platform")
    msub.add_parser("dedupe", help="merge near-duplicate memories (same category, ~80% alike)")
    q = msub.add_parser("coverage", help="which categories are empty, which conversations gave nothing")
    q.add_argument("--json", action="store_true")
    q = msub.add_parser("add")
    q.add_argument("text", nargs="+")
    q.add_argument("--category", default="general")
    q.add_argument("--project")
    for name in ("accept", "reject"):
        q = msub.add_parser(name)
        q.add_argument("ids", nargs="*")
        q.add_argument("--all-candidates", action="store_true")
    q = msub.add_parser("rm")
    q.add_argument("ids", nargs="+")
    q = msub.add_parser("edit", help="write/apply the ctxport-memories.md checklist")
    q.add_argument("--file", default="ctxport-memories.md")
    q.add_argument("--reset", action="store_true", help="discard an existing review file and write a new one")
    p.set_defaults(fn=cmd_memories)

    p = sub.add_parser("export", help="export to a target (see `ctxport platforms`)")
    p.add_argument("target", help="comma separated, e.g. claude-code,codex or markdown")
    _add_export_flags(p)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("migrate", help="import → extract → export in one step")
    p.add_argument("source")
    p.add_argument("args", nargs="+", metavar="[path] target")
    p.add_argument("--force", action="store_true")
    _add_export_flags(p)
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("list", help="list conversations")
    p.add_argument("--platform")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--since")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="print one conversation")
    p.add_argument("id")
    p.add_argument("--last", type=int)
    p.add_argument("--branches", action="store_true")
    p.add_argument("--include-tools", action="store_true")
    p.add_argument("--include-reasoning", action="store_true")
    p.add_argument("--no-redact", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("search", help="full-text search (Chinese works without word segmentation)")
    p.add_argument("query", nargs="+")
    p.add_argument("--platform")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--include-tools", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("handoff", help="build a prompt to continue a conversation in another assistant")
    p.add_argument("id")
    p.add_argument("--last", type=int, default=30)
    p.add_argument("--out")
    _add_llm_flags(p)
    p.set_defaults(fn=cmd_handoff)

    p = sub.add_parser("profile", help="one-page summary of your background, ready to paste into another AI")
    p.add_argument("--out", help="write to this file instead of printing")
    p.add_argument("--copy", action="store_true", help="also copy it to the clipboard")
    p.add_argument("--topics", type=int, default=15, help="how many recent conversation titles to list (0 = none)")
    p.add_argument("--since", help="only topics from conversations updated since, e.g. 90d or 2026-01-01")
    p.add_argument("--platform", help="only memories and topics from these platforms (comma separated)")
    p.add_argument("--max-chars", type=int, help="trim to this many characters (ChatGPT custom instructions: 1500)")
    p.add_argument("--include-candidates", action="store_true", help="also include unreviewed memory candidates")
    p.add_argument("--all-categories", action="store_true",
                   help="also include agent instruction files and assistant prompts")
    p.add_argument("--no-redact", action="store_true")
    _add_llm_flags(p)
    p.set_defaults(fn=cmd_profile)

    p = sub.add_parser("compact", help="make the database smaller: trim tool output, merge the index, VACUUM")
    p.add_argument("--keep-tools", action="store_true", help="don't trim stored tool output, only reclaim space")
    p.set_defaults(fn=cmd_compact)

    p = sub.add_parser("stats", help="per-platform statistics")
    p.add_argument("--json", action="store_true")
    p.add_argument("--size", action="store_true", help="disk usage by platform: dialogue, tool output, raw JSON")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("mcp-config", help="print MCP configuration for a client")
    p.add_argument("client", nargs="?")
    p.set_defaults(fn=cmd_mcp_config)

    sub.add_parser("serve-mcp", help="run the MCP server on stdio").set_defaults(fn=cmd_serve_mcp)

    p = sub.add_parser("serve-ingest", help="local receiver for the ctxport browser extension (127.0.0.1)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--pair", action="store_true",
                   help="print a one-time code to pair the browser extension (works while the receiver runs)")
    p.add_argument("--reset-pairing", action="store_true", help="forget the paired extension and print a new code")
    p.add_argument("--autostart", choices=["install", "remove"],
                   help="start the receiver at login (launchd / systemd --user / Startup folder)")
    p.set_defaults(fn=cmd_serve_ingest)
    return ap


def main(argv: Optional[list] = None) -> int:
    _utf8_stdio()
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "migrate":
        rest = args.args
        if len(rest) == 1:
            args.path, args.target = None, rest[0]
        elif len(rest) == 2:
            args.path, args.target = rest
        else:
            ap.error("usage: ctxport migrate <source> [path] <target>")
    try:
        return args.fn(args) or 0
    except KeyError as e:
        info(str(e).strip("'\""))
        return 2
    except NewerDatabase as e:
        info(str(e))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
