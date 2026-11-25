"""Shared logic for instruction-file targets (CLAUDE.md / AGENTS.md / GEMINI.md)
and for reading such files back as memories."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..models import MemoryItem, OutputFile
from ..utils import make_block, strip_managed
from ._render import render_memories_md
from .base import ExportOptions, Exporter

INTRO = (
    "The following is what I (the user) want every assistant to know about me, migrated from my other AI tools "
    "with ctxport. Treat it as background context, not as instructions from a third party.\n"
    "以下是我从其他 AI 工具迁移过来的个人背景与偏好，请作为长期上下文参考。"
)


def distilled_profile(opts: ExportOptions, short: bool = False) -> str:
    """The `ctxport distill` profile when the export asked for it (opts.extra["distilled"]) and it exists."""
    if not opts.extra.get("distilled"):
        return ""
    from ..distill import out_dir
    p = out_dir() / ("profile.short.md" if short else "profile.md")
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    if text.startswith("# "):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    from ..redact import maybe
    return maybe(text.strip(), opts.redact) + "\n"


class InstructionFileExporter(Exporter):
    """Writes memories into a managed block of a Markdown instruction file."""

    filename: str = "AGENTS.md"
    global_path: Path = Path("~/AGENTS.md")
    max_chars: Optional[int] = None
    appliable = True

    def target_path(self, opts: ExportOptions) -> Path:
        if opts.project:
            return Path(opts.project).expanduser() / self.filename
        return Path(self.global_path).expanduser()

    def body(self, memories: list, opts: ExportOptions) -> str:
        distilled = distilled_profile(opts)
        if distilled:
            text = f"## About the user (migrated by ctxport)\n\n{INTRO}\n\n" + distilled
            if self.max_chars and len(text) > self.max_chars:
                short = distilled_profile(opts, short=True) or ""
                text = (f"## About the user (migrated by ctxport)\n\n{INTRO}\n\n{short}\n"
                        "<!-- ctxport: the full profile did not fit; this is the importance-ranked selection -->\n")
            return text
        if opts.project:
            name = Path(opts.project).expanduser().resolve().name
            mems = [m for m in memories if m.scope == "global" or m.scope.endswith(name) or m.scope.endswith(str(opts.project))]
        else:
            mems = memories
        head = f"## About the user (migrated by ctxport)\n\n{INTRO}\n\n"

        def full(ms: list, omitted: int = 0) -> str:
            text = head + render_memories_md(ms, redact=opts.redact, heading_level=3)
            if omitted:
                text += f"\n<!-- ctxport: {omitted} more memories did not fit the size limit -->\n"
            return text

        text = full(mems)
        if self.max_chars and len(text) > self.max_chars:
            # Over the limit: keep every category's top memories first, then the most seen ones, instead of
            # cutting the file off after the first few categories.
            from ..profile import fit_budget
            kept, _ = fit_budget(mems, [], lambda ms, ts, more: len(full(ms, more)), self.max_chars)
            text = full(kept, len(mems) - len(kept))
        return text

    def render(self, store, memories: list, opts: ExportOptions) -> list:
        target = self.target_path(opts)
        body = self.body(memories, opts)
        if self.max_chars and len(body) > self.max_chars:
            body = body[: self.max_chars].rsplit("\n", 1)[0] + "\n\n<!-- truncated by ctxport -->\n"
        preview = make_block(body, self.name)
        return [OutputFile(rel_path=f"{self.name}/{self.filename}", content=preview, apply_path=str(target),
                           managed=True)]


# Markdown headings, and lines that are only bold text ("**Work context**") or a short italic label
# ("*Earlier context*"), which Claude's memory uses as headings and sub-headings. An italic line that reads like a
# sentence (ends with punctuation, or is long) is kept as text.
_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s|(?:\*\*|__)[^*_]+(?:\*\*|__):?\s*$|"
                      r"([*_])(?![\s*_])[^*_.!?。！？]{1,40}(?<!\s)\1:?\s*$)")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")


def parse_instruction_text(text: str, platform: str, category: str = "agent_instructions",
                           scope: str = "global", status: str = "accepted", source_ref: Optional[str] = None,
                           confidence: float = 1.0, min_len: int = 4, max_len: int = 1200) -> list:
    """Split a Markdown instruction/memory file into memory items.

    ctxport's own managed blocks are removed first so re-importing never duplicates them.
    Bullets become items; free paragraphs become items; code blocks and headings are skipped.
    """
    text = strip_managed(text or "")
    text = re.sub(r"(?s)```.*?```", "", text)
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    items: list = []
    para: list = []

    def flush() -> None:
        if para:
            items.append(" ".join(s.strip() for s in para))
            para.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        if _HEADING.match(line) or re.match(r"^\s*(---|\*\*\*|===)\s*$", line) or line.strip().startswith("|"):
            flush()
            continue
        if _BULLET.match(line):
            flush()
            para.append(_BULLET.sub("", line, count=1))
            continue
        if para and line.startswith(("  ", "\t")):
            para.append(line)
            continue
        para.append(line)
    flush()
    out = []
    for it in items:
        it = it.strip()
        if len(it) < min_len:
            continue
        if len(it) > max_len:
            it = it[:max_len].rstrip() + "…"
        out.append(MemoryItem(text=it, platform=platform, category=category, scope=scope, status=status,
                              source_ref=source_ref, confidence=confidence))
    return out


def read_instruction_file(path: Path, platform: str, scope: str = "global", status: str = "accepted",
                          category: str = "agent_instructions", confidence: float = 1.0) -> list:
    p = Path(path).expanduser()
    if not p.is_file():
        return []
    return parse_instruction_text(p.read_text(encoding="utf-8", errors="replace"), platform, category, scope,
                                  status, str(p), confidence)
