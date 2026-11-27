# Changelog

All notable changes to ctxport are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- English for `ctxport app` and the browser extension popup: a 中文 / English button in the header switches the
  language (remembered; the default follows the browser's language). Chinese stays the source text; progress log
  lines from a distillation run are still in Chinese.
- `ctxport app` is now a guided four-step page (collect → distill → review → import) with a "next step" card and
  how-to text on every step. Distilling with Claude Code or Codex lets you pick the model, the reasoning effort
  (`--effort` / `model_reasoning_effort`) and how many recent conversations to process, previews the work before it
  starts, shows progress, and can be stopped (a later run resumes).
- Token estimates for agent distillation: before a run, from the conversations' length; after the first batch, from
  the usage Claude Code (`--output-format json`) or Codex (`--json`) reports, saved per agent, model and effort.
- A cheaper way to distill with Claude Code or Codex, now the default ("省 token"): conversations are shortened
  first (code becomes one line saying what it is, stack traces their last line, long pasted text its start and end,
  long conversations their start, end and every self-statement) and several are sent in one tool-less question.
  Measured on real data: about 1.4k tokens per conversation instead of about 18k with the agent loop, which stays
  available as "Agent 逐个读文件".
- The profile comes in three sizes: compact (~8k characters, the default for imports), standard (~20k) and full.
  The shorter ones give every category a share of the space before filling it by importance, and say that the rest
  can be looked up. Step 4 of the app picks the size (also used by the extension and `--profile-size` on the
  command line), step 3 shows each one. A new MCP tool, `search_memory`, searches every fact of the full profile, and
  `get_user_memory` returns the standard profile when there is one.
- Agent distillation can run several batches at once (1–16), stops the whole run as soon as the agent reports its
  quota is used up (showing the agent's own message instead of the log noise around it), and can then continue with
  the other agent and/or wait until the time the agent names for the quota to come back (keeping the Mac awake while
  it waits). After a run the app says what to do next: how many conversations are left, view the profile, import it.
- Codex is found inside the ChatGPT desktop app (`ChatGPT.app/Contents/Resources/codex-cli/bin/codex`) when it is not
  on PATH, and its model list comes from Codex's own cache.

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
- Rule extraction finds more ways of saying who you are ("我目前是…", "我住在…", "我毕业于…", "我在腾讯做前端",
  "I'm based in…", "I work on…", "My team uses…", "I code mostly in…") and skips more remarks about the task at hand
  ("我用了你的方法…", "我在想要不要…", "I use this function in a loop…"). On 80 held-out labelled messages F1 rose
  from 0.61 to 0.88 (recall 0.55 → 1.00, precision 0.69 → 0.78).
- LLM extraction leaves out fenced code and runs of code / log lines in your messages and sends repeated messages
  once: 61% fewer characters and 54% fewer requests on synthetic history, with every embedded fact still sent.
- Size-limited exports (Antigravity's 12,000-character rules file) keep every category's top memories, then the most
  seen ones, instead of cutting the file after the first categories (6 of 6 categories instead of 3 of 6 on 2,000
  synthetic memories). `export chatgpt` also writes `chatgpt-custom-instructions.txt`, fitted to the 1,500-character
  custom-instructions field the same way. `profile --max-chars` is about 4.5x faster.
- The database uses `synchronous=NORMAL` with its write-ahead log: commits no longer wait for the disk, which made a
  fresh import about 10% faster. A power cut can lose the last commits but cannot corrupt the database.

### Fixed
- `import` and `migrate` exit with 1 and say why when nothing was imported (a wrong platform, an empty, broken or
  unrelated file, including files a platform's reader cannot parse at all), instead of reporting "imported 1" and
  exiting 0; `migrate` then stops before exporting.
- An unreachable LLM is one clear line with exit code 2 in `extract --llm` and `handoff --llm` too, as in `profile`.
- A database that is not a ctxport database or is damaged gives a clear message instead of a Python traceback.
- Conversations without any time in the file (generic JSON) keep an unknown date instead of the file's date.
- Chinese statements joined by commas ("我是…，我在…，平时…") become separate memories, as English sentences do, and
  short Chinese statements ("我是果冻") are no longer dropped for being under six characters.
- An assistant's persona (Chatbox agent-soul) and assistant / copilot prompts (Chatbox, Cherry Studio) are no longer
  imported as memories: they describe a bot, not the user, and the persona was even filed as the user's identity.
- `profile` includes agent instructions, as exports to CLAUDE.md / AGENTS.md do.
- Antigravity can be imported from a parent folder (the home folder or `~/.gemini`), like the other platforms.
- Exported project memories name the project folder only, not its full local path, in the text and in the
  "Project:" headings of CLAUDE.md / AGENTS.md / `profile`.
- Databases from earlier versions mark assistant personas and assistant-prompt candidates as rejected once
  (database version 6), so an old "Friendly tutor" identity no longer reaches exports.
- `memories --help` shows the dedupe description instead of argparse internals; `profile --max-chars` that is too
  small to fit anything says so; MCP `remember` answers every outcome; the README lists all six MCP tools.
- Distilling with Codex works with current Codex versions, which removed `--full-auto` (the sandbox is now chosen
  with `-s workspace-write` when `--full-auto` is missing).
- Agent distillation no longer loads the user's own MCP servers, settings and global CLAUDE.md for every batch
  (`--setting-sources "" --strict-mcp-config`, Codex `--ignore-user-config` with the user's default model kept).
- Agent distillation runs no longer leave sessions in Claude Code / Codex history (`--no-session-persistence`,
  `--ephemeral`), which ctxport would otherwise import as new conversations.
- `ctxport app --launcher FOLDER` (macOS) creates a double-clickable ctxport.app; launching `ctxport app` while it is
  already running reopens the running page instead of failing on the busy port.
- `ctxport app`: a local web app (127.0.0.1, per-launch random token, Host and Origin checks) to review memories and
  import them with one click into Claude Code, Codex, Gemini CLI, Antigravity and Cursor (project rule or User
  Rules text), to connect the ctxport MCP server to Claude Desktop, Claude Code, Codex, Cursor, Gemini CLI and
  Antigravity (only the `ctxport` entry is touched; unreadable config files are never overwritten), and to prepare
  Chatbox / Cherry Studio imports.
- The browser extension can write memories into ChatGPT custom instructions (keeping the user's own "about you"
  text), the Claude.ai memory import page and Gemini saved info (form filling, experimental). The text comes from
  the paired receiver's new `/memory-text` endpoint, never from a web page.
- `cursor` export target.
- `ctxport distill`: coverage-guaranteed hierarchical memory distillation with a model the user chooses
  (`ctxport llm set --preset … --model …`; presets for common OpenAI-compatible providers, no built-in model).
  L1 episode cards with evidence (cached per conversation), L2 canonical facts that cover every L1 fact, L3 themed
  profile that lists every canonical fact (both invariants checked in code, dropped items restored), L4 budgeted
  renderings with coverage@budget. `--manual DIR` writes prompts to paste into any chat AI. Each run logs counts,
  tokens, timings and coverage to `~/.ctxport/distilled/run-*.json`. `export --distilled` and `ctxport app` use the
  profile.
- Extension: rate-limit waits are timed by the background worker, so a background tab no longer stretches them.
- `ctxport distill` without an API key: `--agent claude-code|codex` runs the user's own agent CLI batch by batch
  (`claude -p` / `codex exec`, resumable), `--agent folder` prepares a task folder (TASK.md, AGENTS.md, CLAUDE.md)
  for any agent, and the app has a copy/paste flow for ChatGPT / Claude on the web (optionally only the most recent
  50 / 200 conversations). The API path is now opt-in with `--api`.
- One-click ChatGPT official export: the extension popup and `ctxport app` press "Export data" in the logged-in
  ChatGPT tab (falling back to opening Settings → Data controls). Official ChatGPT and Claude.ai export zips saved in
  the Downloads folder are detected by their file list and imported by `ctxport sync` (`CTXPORT_DOWNLOADS` to change
  the folder, `off` to disable); conversations already synced by the extension are deduplicated by id.
- Extension pause switch: stops a running sync at the next checkpoint (also during a long rate-limit wait; saved
  conversations are kept), and skips auto-sync and scheduled syncs while paused. Turning it back on resumes.
- `ctxport forget-web PLATFORM` and a delete button in `ctxport app`: delete what the browser extension collected
  for one site (conversations, messages, search index entries, unreviewed candidates, inbox files; then the
  database is compacted) and turn that site's web sync off, so the receiver refuses the extension (HTTP 410) instead
  of letting it read everything again. Official export imports and accepted or rejected memories are kept; the
  official export's import record is forgotten so the next `ctxport sync` restores any conversation it had skipped.
  `ctxport web-sync [PLATFORM on|off]` shows or changes it.
- ChatGPT official exports in the 2026 layout (`index.html` and `export_manifest.json` instead of `chat.html`) are
  found in the Downloads folder again; before, `ctxport sync` skipped them silently.
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
