# Changelog

All notable changes to ctxport are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Memory quality (database version 3): extracted facts that nearly duplicate an existing memory (same category,
  ~80% alike after folding width, case, spacing and the subject) are merged into it; each memory counts the
  conversations that mention it and records when it was first and last seen; memories rank by accepted, then
  most seen, then most recent; rewordings of a rejected memory stay rejected; the review checklist shows counts
  and flags older facts as "maybe outdated" when a newer related one appears. `ctxport memories dedupe` merges
  duplicates already stored.
- `ctxport profile --max-chars` keeps every category: each gets its top entries first, then the rest by rank,
  with a "… N more not shown" line instead of cutting whole sections off.
- Smaller databases: long tool outputs keep their first and last 2 KB (`--keep-full-tools` keeps them whole) and
  the full-text index covers dialogue only (database version 2; the index is rebuilt once on upgrade). A
  coding-assistant sample went from 274 MB to 30 MB.
- `ctxport compact` trims tool output stored by older versions, merges the search index and runs VACUUM;
  `ctxport stats --size` shows disk usage per platform; `sync` suggests `compact` when it would help.
- Coverage report after `extract` and in `ctxport memories coverage`: memories per category, empty categories,
  and conversations where you wrote a lot but nothing was extracted.
- `ctxport profile`: a one-page, paste-ready summary of your background (memories grouped by category plus recent
  conversation topics), with `--copy`, `--out`, `--max-chars`, `--since`, `--platform` and an optional
  `--llm` first-person summary. Falls back to unreviewed candidates, with a warning, when nothing is reviewed yet.

### Security
- MCP `remember()` saves memories as candidates that you must accept before they are used or exported.
- Secret redaction covers JSON keys, environment variables, `Authorization` headers, passwords in URLs,
  and Stripe, Hugging Face, GitLab, npm, PyPI and Slack webhook tokens.
- The browser extension pairs with `ctxport serve-ingest` through a one-time code
  (`ctxport serve-ingest --pair`) and a per-extension token. Extensions paired with 0.1.0 must pair again once.
- `~/.ctxport` is created 0700 and its database and inbox files 0600; existing installs are tightened.
- `memories rm` / `memories accept` need an exact id or a unique prefix.
- Writes to `CLAUDE.md` / `AGENTS.md` are atomic, and memory text can't break out of the managed block.

### Packaging
- PyPI metadata (project links, classifiers), the version is read from `ctxport.__version__` only,
  and a tag-triggered release workflow publishes to PyPI.
- Smaller downloads: releases attach a 14 KB browser-extension zip (`scripts/pack_extension.py`, runtime files
  only) and the wheel to a GitHub release, so nobody has to clone the repository; the sdist no longer ships tests
  or the extension; GitHub source archives skip tests and CI files; CI enforces a size budget and checks that a
  plain install pulls in no dependencies.

## [0.1.0] - 2026-11-24

First public release: importers and exporters for ChatGPT, Claude.ai, Claude Code, Codex, Gemini CLI,
Gemini web, Google Antigravity, Cherry Studio and Chatbox; local SQLite store with full-text search;
memory extraction and review; MCP server; and the browser companion extension.

[Unreleased]: https://github.com/Memoryport/ctxport/compare/cca1cc6...HEAD
[0.1.0]: https://github.com/Memoryport/ctxport/commit/cca1cc6
