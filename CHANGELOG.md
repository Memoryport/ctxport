# Changelog

All notable changes to ctxport are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Re-syncing a conversation rewrites only the messages that changed. The browser extension sends a whole
  conversation again after each new exchange; before, every message was deleted and re-inserted together with its
  full-text index entries. On 600 synthetic conversations (33,760 messages) a re-sync with one new exchange each
  took 0.74 s instead of 11.7 s, an unchanged re-sync 0.42 s instead of 11.1 s, and the database stayed 96 MB
  instead of growing to 218 MB. `benchmarks/bench.py` reproduces the numbers.
- The near-duplicate check compares a new memory only with memories whose length can reach the 80% threshold
  (database version 5 stores each memory's bigram count) and caches bigram sets: adding 6,000 candidates took
  2.6 s instead of 14.6 s and `memories dedupe` 0.54 s instead of 6.6 s, with the same merges.
- Search terms shorter than 3 characters ("前端", "索引") no longer turn the whole query into a full scan: the longer
  terms use the full-text index and the short ones filter its hits ("前端 性能优化": 27 ms instead of 70 ms, same
  results).
- The database uses `synchronous=NORMAL` with its write-ahead log: commits no longer wait for the disk, which made a
  fresh import about 10% faster. A power cut can lose the last commits but cannot corrupt the database.

### Fixed
- E-mail addresses, WeChat / QQ ids and licence plates are no longer turned into memories (they were exported to
  every assistant and shown by `profile`). Conversation archives keep them, as before.
- Masking also covers `密码 hunter22` (a space alone before the value), passport numbers after "护照号" / "passport no.",
  and Chinese street addresses down to the house number ("中关村大街27号").
- Exported files and `handoff --out` are written with 0600 permissions.
- Claude memory: short italic sub-headings ("*Earlier context*") are no longer glued to the next memory.
- `compact` keeps the import records of the inbox files it gzip-compresses, so the next sync no longer reads them all
  again and reports them as imported. A warning no longer repeats the file name.
- Extension: one conversation that keeps failing to download no longer aborts the whole ChatGPT / Claude sync. It is
  skipped, counted in the popup and fetched again next sync; the rest, memories and custom instructions are still
  sent. Signing out or the local ctxport server refusing data stops every parallel download at once.
- After an inbox file is gzip-compressed (by `compact`, or rewritten by the extension), the plain file's row is removed
  from the sources table instead of staying there for ever.
- Importing an older copy of a conversation (an official export downloaded before the browser extension synced newer
  messages) no longer replaces the newer one. The stored copy is kept with a warning; `--force` overwrites on purpose.
- Sentences holding a secret or a personal identifier (API keys, passwords, ID card, phone and card numbers) are never
  turned into memories, by rules, the LLM, `extract --questions` or `memories import`. Such candidates left by older
  versions are removed on the next `extract` or `memories edit`. The review checklist masks secrets and is written
  with 0600 permissions.
- Claude memory: only the memory text is imported. Account / memory ids, timestamps and bold sub-headings
  ("**Work context**") no longer become accepted memories, and project memories keep their project's scope. Database
  version 4 deletes such entries imported by older versions.
- Masking also covers Chinese mobile, ID card (checksum-verified) and card numbers (Luhn-verified), Alibaba / Tencent
  Cloud access key ids, `password hunter2` without "is", and `token 是 …`.
- The extension receiver marks a conversation as known only after it is really in the store, so one whose import
  failed (bad data, a busy database) is sent again; the store waits up to 60 s for another writer instead of failing
  after 5 s.
- Rule extraction skips sentences about the conversation rather than you ("我是说…", "我的问题是…", "我喜欢这个答案",
  "I like it", "I'm a bit confused", "I never got that error") and health details, drops the request part of a
  sentence ("我主要用 Neovim，帮我写个 Makefile" → "我主要用 Neovim"), and finds "以后…都用英文", "我在…读研究生",
  "我在用 …" and "作为一个产品经理，…？".
- A message you replaced by editing it is no longer extracted next to the edited version.
- The questionnaire counts a memory of any category as an answer ("我是后端工程师" answers the role question, a
  custom instruction "回答请用中文" the language question), so `extract --questions` no longer re-asks them.
- Search terms are phrases: question keywords such as "I work as" no longer match "pandas … data work", and
  `ctxport search "vector database"` searches the phrase (unquoted words still all have to appear).

### Added
- 30-question background questionnaire in three stages (`src/ctxport/questionnaire.py`):
  `ctxport memories coverage` shows which questions are answered; `ctxport extract --questions` asks the LLM only
  the unanswered ones, over excerpts of your own messages found by search, and keeps only cited answers;
  `ctxport ask` (and `profile --ask`) asks you up to 5 of the rest; `ctxport prompt` prints a prompt for the
  assistant you're leaving and `ctxport memories import` reads its answer back as candidates.
- Broader extraction rules ("As a …,", "我平时写 …", "Our stack is …", self-descriptions that open a question),
  branches are read, and LLM extraction includes assistant statements you explicitly confirmed.
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

### Documentation
- Visual install guide (`docs/img/install-guide.svg`) in both READMEs, with short install steps for the CLI
  (`pipx install ctxport`) and for the browser extension from the release zip.

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
