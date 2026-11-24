# Platform formats / 各平台数据格式

This file records what each importer reads, **and what was actually observed on a real machine**
(macOS, 2026-11-23, read-only reconnaissance — only key structures and type counts were printed,
never message text). Where reality differed from public descriptions, reality wins and the
difference is marked **⚠ observed**.

本文记录每个导入器读取的格式，以及在真实电脑上只读侦察到的实际结构（只打印键结构与类型分布，未打印正文）。
与公开资料不一致处标注 **⚠ observed**，以实测为准。

Synthetic fixtures for every variant below live in `tests/fixtures.py`.

---

## Reconnaissance summary (2026-11-23)

| Location | Exists | Files | Latest change | Notes |
|---|---|---|---|---|
| `~/.claude/` | yes | ~10.1k | 2026-11-23 | 172 session JSONL (171 top-level + 1 subagent), 25 auto-memory `.md` |
| `~/.codex/` | yes | ~46k | 2026-11-23 | only **12** rollout JSONL but **184** threads in SQLite |
| `~/.gemini/` | yes | 69 | 2026-11-19 | no `tmp/` (Gemini CLI never used); `antigravity/` present |
| `~/.gemini/antigravity{-ide,-cli,-backup}` | no | – | – | |
| Cherry Studio (`~/Library/Application Support/CherryStudio`) | yes | 144 | 2026-09-10 | version 1.9.9; no `cherrystudio.sqlite`; `Data/agents.db` present |
| Chatbox (`~/Library/Application Support/xyz.chatboxapp.app`) | yes | 92 | 2026-04-12 | sessions in IndexedDB, `config.json` has settings only |
| ChatGPT / Claude.ai / Takeout / Cherry / Chatbox export files | none found | | | importers tested on synthetic fixtures only |

---

## ChatGPT (official export)

`conversations.json` or sharded `conversations-NNN.json` (matched by regex), each a list of
`{title, create_time, update_time, conversation_id, current_node, mapping{id:{id,parent,children,message}}}`.

* Main line = walk `parent` from `current_node`. Every other leaf's path, cut at the fork point, is a branch.
* `content.content_type`: `text` / `multimodal_text` (parts are strings or `*_asset_pointer` objects → attachment meta),
  `code` → tool_call, `execution_output` → tool_result, `thoughts` / `reasoning_recap` → reasoning,
  `tether_browsing_display` / `tether_quote` → tool_result with citation meta,
  `user_editable_context` → system_prompt + accepted memories (`user_profile`, `user_instructions`),
  `model_editable_context` → system_prompt.
* `metadata.is_visually_hidden_from_conversation` → system_prompt. `metadata.model_slug` → model.
* Any `*memor*.json` in the export: every string ≥ 6 chars becomes an accepted memory (shape is undocumented).
* Not verified on real data (no export on this machine).

## Claude.ai (official export)

`conversations.json`: `[{uuid, name, created_at, updated_at, chat_messages:[{uuid, sender, text, content[], attachments, files, created_at, parent_message_uuid?}]}]`.
Content blocks `text`, `thinking` (encrypted → empty text + `meta.encrypted`), `tool_use`, `tool_result`, others kept as text.
If `parent_message_uuid` exists the tree is honoured (leaf = `current_leaf_message_uuid` or last message).
`projects.json` → `prompt_template` = project-scoped memories, `docs` = a "Project: …" conversation of artifacts.
`memories.json` → all strings. Not verified on real data.

## Claude Code — ⚠ observed differences

`$CLAUDE_CONFIG_DIR` or `~/.claude`; sessions at `projects/<encoded-cwd>/<session-uuid>.jsonl`.

Observed record `type` distribution across all 172 files:

| type | count | handling |
|---|---|---|
| `assistant` | 17,508 | message / reasoning / tool_call blocks |
| `user` | 10,237 | prompt, tool_result, or injected (see below) |
| `api-request`, `api-request-blob`, `api-request-shape` | 8,025 / 4,463 / 248 | **ignored** — ⚠ `api-request-blob` repeats message content (would duplicate) |
| `attachment` | 6,651 | ignored (environment snapshots) |
| `last-prompt`, `atis-latch`, `bridge-session`, `queue-operation`, `cost-state`, `mode`, `relocated`, `tools-used`, `file-history-*`, `data-flow-*`, `derived-from` | – | ignored |
| `custom-title` / `agent-name` | 3,825 / 3,647 | ⚠ title source (last `customTitle` wins); there are **no** `summary` records any more (still supported) |
| `pr-link` | 567 | `meta.pr_links` |
| `system` | 1,334 | `compact_boundary` has `parentUuid: null` + **`logicalParentUuid`** → followed when walking the chain |

* ⚠ **Subagents** are separate files: `projects/<cwd>/<session>/subagents/agent-<id>.jsonl` → own conversation,
  `meta.is_subagent`, `meta.parent_id`.
* One API reply is written as several lines sharing `message.id`; consecutive text blocks are merged into one message;
  thinking/tool_use blocks become their own reasoning/tool_call messages.
* ⚠ **Branches:** parallel tool calls produce sibling chains (each `tool_result` points at its own `tool_use` record).
  A naive "walk from the last leaf" put 1,062 tool results into fake branches. Fix: a fork only counts as a branch
  when it contains a real user prompt (rewind / edit); other off-path records stay in the main line in file order.
  Result on real data: 0 false branches. Dangling parents (record not in the file) continue from the previous record.
* Injected user text → `system_prompt`: `isMeta`, `isCompactSummary`, `<command-name>`, `<command-message>`,
  `<local-command-stdout|stderr|caveat>`, `<system-reminder>` (stripped from the end of real prompts),
  `<task-notification>`, `[Request interrupted…`. `<bash-input>` / `<bash-stdout>` → tool_call / tool_result.
* Thinking blocks observed with empty `thinking` + `signature` → reasoning with `meta.encrypted=True`; the signature is never stored.
* Memories: `~/.claude/CLAUDE.md` (accepted), `<cwd>/CLAUDE.md` (accepted, scope `project:<cwd>`),
  `projects/*/memory/*.md` (auto-memory, frontmatter stripped, candidate; `MEMORY.md` index skipped).

## Codex — ⚠ major difference

`$CODEX_HOME` or `~/.codex`.

* ⚠ `state_5.sqlite.threads` lists **184** threads but only **12** `rollout_path` files still exist
  (archived dir also empty). The full item history of all 184 threads is in
  **`thread_history_1.sqlite.thread_items`** (29,385 rows). ctxport imports threads from this DB when their
  rollout file is missing (`meta.recovered_from = thread_history_db`), and from the rollout file otherwise
  (never both). The newest `state_N` / `thread_history_N` file is used.
  * `threads` columns used: `id, rollout_path, title, name, cwd, created_at(_ms), updated_at(_ms), archived, model,
    git_branch, git_sha, cli_version, source, thread_source, agent_nickname, agent_role`; `thread_spawn_edges` gives subagent parents.
  * `thread_items.item_type` observed: `reasoning` 9,126 · `commandExecution` 8,296 · `agentMessage` 5,190
    (`phase`: commentary 3,835 / final_answer 1,355) · `mcpToolCall` 3,198 · `userMessage` 1,534 · `fileChange` 854 ·
    `webSearch` 555 · `imageView` 254 · `subAgentActivity` 195 · `collabAgentToolCall` 77 · `contextCompaction` 58 ·
    `sleep` 31 · `dynamicToolCall` 9 · `imageGeneration` 7 · `plan` 1.
* Rollout JSONL `{timestamp, ordinal, type, payload}`. Observed: `response_item` (message 1,479; reasoning 2,438;
  `function_call`/`_output`; `custom_tool_call`/`_output`; ⚠ `agent_message` (inter-agent); `additional_tools`,
  `configuration_update` ignored), `event_msg` (⚠ only `item_completed`, `token_count`, `task_*`,
  `thread_settings_applied` — no `user_message`/`agent_message` in new files; still used as fallback),
  `turn_context` (model), `compacted`, ⚠ `world_state`, `token_usage_record`, `inter_agent_communication_metadata` (ignored).
  Legacy files without the `{type,payload}` wrapper are supported.
* Roles: `developer` → system_prompt. Injected user prefixes → system_prompt: `<environment_context>`,
  `<user_instructions>`, `# AGENTS.md instructions`, `<permissions instructions>`, `<user_shell_command>`,
  `<turn_aborted>`, ⚠ `<external_codex_apps_*>`, `<INSTRUCTIONS>`.
* ⚠ Desktop app file mentions: `# Files mentioned by the user:\n## name: path … ## My request:\n<prompt>` →
  the prompt is kept as the user text, files go to `meta.attachments`. `<send_user_message_question_reply>` is unwrapped.
* Memories: `AGENTS.md` (accepted), `<cwd>/AGENTS.md` (project scope). ⚠ `memories/` is a git repo with
  `MEMORY.md` (~119 KB), `memory_summary.md`, `raw_memories.md`, `rollout_summaries/`; ctxport reads top-level
  `.md/.txt/.json` except `raw_memories.md` as **candidates** (model-generated). `memories_v2/` currently holds no top-level files.
  `memories_*.sqlite.stage1_outputs` (per-thread raw memories) are not imported.

## Gemini CLI

`~/.gemini/tmp/<project_hash>/chats/session-*.json` (`ConversationRecord`) or `.jsonl`
(each line = full record, a single message — later lines with the same `id` replace earlier ones — or metadata / `{"$set":…}`),
`checkpoint-<tag>.json` (`[{role, parts}]`). `toolCalls` → tool_call + tool_result (`resultDisplay`), `thoughts` and
`parts[].thought` → reasoning, `info/error/warning` → system_prompt. Memory: `~/.gemini/GEMINI.md`.
⚠ Not installed on this machine (`~/.gemini/tmp` missing) — fixtures only.

## Google Antigravity (experimental)

Scanned: `~/.gemini/antigravity`, `-ide`, `-cli`, `-backup` (files sorted by mtime so the newest copy of a
conversation wins). ⚠ Observed: only `antigravity/` exists; `conversations/` and `brain/` are **empty**,
`knowledge/` has only a lock file, so **no `.pb` sample was available**: header bytes could not be checked and
no stable role field number could be identified. Roles therefore stay `unknown`.

* Generic protobuf wire decoder (varint / fixed64 / length-delimited / fixed32), recursive, keeps readable UTF-8,
  drops UUIDs, paths, URLs, base64, JSON tool args, enum names and single ASCII tokens. The most repeated top-level
  field is treated as "steps" (one message per step). Gzip/encrypted input yields no text and a warning with the header hex.
* ⚠ Observed extra source: `conversation_summaries.db` (SQLite, table `conversation_summaries(conversation_id, title,
  preview, step_count, last_modified_time, workspace_uris, parent_conversation_id, …)` + FTS table `conversations_fts`
  with `user_body` / `agent_body`) — 0 rows here; used for titles/workspace/parent when present.
  `antigravity_state.pbtxt` is app state (no titles). `annotations/*.pbtxt` `title: "…"` is used when present.
* `brain/<id>/*.md` → artifact messages; `knowledge/<item>/metadata.json` + files → candidate memory + searchable doc.
* Rules imported as memories: `~/.gemini/AGENTS.md`, `~/.gemini/config/{AGENTS,GEMINI}.md`, `~/.gemini/config/rules/*.md`
  (⚠ observed `~/.gemini/config/` contains `config.json`, `mcp_config.json`, `projects/`).

## Gemini web / app (Google Takeout, experimental)

`Takeout/<My Activity, localized>/<Gemini Apps>/MyActivity.json` (regex on file name). Each entry: `title`
(localized "Prompted …" prefix stripped), `time`, `safeHtmlItem[].html` (tags stripped), `attachedFiles`.
Entries < 30 min apart are merged into one conversation (`CTXPORT_TAKEOUT_GAP_MIN`). HTML-only export → warning.

## Cherry Studio — ⚠ observed differences

* 2.x: `<userData>/Data/cherrystudio.sqlite` (`topic`, `message` tree via `parent_id`/`active_node_id`,
  `assistant`, `agent_session*`). Main line from `active_node_id`; other subtrees → branches; `deleted_at` skipped;
  text from `searchable_text`, else `data` JSON (`blocks`/`parts`, `main_text`, `thinking`).
* ⚠ Installed version is **1.9.9**: chat topics live in Chromium **IndexedDB (LevelDB, snappy + V8 serialization)** —
  not parsed (would need non-stdlib decoders). Use Cherry's own backup zip instead.
* ⚠ Observed `Data/agents.db` (drizzle): `agents`, `sessions`, `session_messages(content = {message, blocks[]})`,
  `scheduled_tasks`, `task_run_logs`, `skills` … → agent sessions are imported, agent `instructions` → candidate memories.
* Backups: v7 zip (`metadata.json` + `Data/cherrystudio.sqlite`, extracted to a temp dir) and 1.x zip
  (`data.json` → `indexedDB.topics[].messages[]` + `indexedDB.message_blocks[]`, topic names from the nested
  `localStorage["persist:cherry-studio"]` redux JSON).

## Chatbox — ⚠ observed difference

Backups V1 (`.json`), V2 (zip, `sessions/<path>/session.json`), V3 (zip, `sessions/<urlencoded id>.jsonl` with
`session` / `thread` / `message` lines; memories from `agent-memories`, `copilot-memories`, `agent-soul`).
Main thread = `session.threadId` (fallback: thread named main / last thread); other threads → branches.
⚠ The desktop app's local data is in IndexedDB; `config.json` only holds settings — use the backup export.

## Local Chromium storage (pure-Python reader, `ctxport/chromium/`)

Electron apps keep data in Chromium IndexedDB = LevelDB (`*.log` write-ahead batches in 32 KiB blocks,
`*.ldb` tables with snappy blocks) + IndexedDB key encoding + Blink/V8 structured-clone values.
Implemented from the public formats: LevelDB log/table reader (newest sequence wins, deletions honoured),
snappy decompressor, IndexedDB metadata (database names: key type 201; object-store names: type 50, UTF-16BE),
encoded IDB keys (0 null, 1 string, 2 date, 3 number, 4 array, 6 binary), Blink envelope
(`FF ver [FE trailer]`) + V8 tags, wrapped/blob and snappy-compressed values, Local Storage (`_origin\0\1key`).

⚠ Observed (2026-11-23, structure only):

| App | Database / store | Records | Notes |
|---|---|---|---|
| Chatbox (`xyz.chatboxapp.app`) | `chatboxstore` / `keyvaluepairs` (localforage) | 16 | values are **JSON strings** with V1-backup keys (`chat-sessions-list`, `session:<id>`, `myCopilots`); 2 built-in demo sessions; messages have **no timestamp** |
| Cherry Studio 1.9.9 | `CherryStudio` / `topics` (6), `message_blocks` (44), `files`, `settings`, … | 50 | topic = `{id, messages[]}`; names/assistants in Local Storage `persist:cherry-studio` (nested JSON) |
| Claude desktop (`Claude/IndexedDB/https_claude.ai_0…`) | `keyval-store` react-query cache, `claude-conversation-store` (empty) | 7 | only `chat_conversation_list` (titles), **no message bodies** → not used as a message source |

All 50 Cherry and 16 Chatbox records decoded with 0 warnings; one Claude desktop record (Blink host object `K`,
a device key) is skipped.

## Browser extension → inbox

`ctxport serve-ingest` (127.0.0.1:8765) receives what the extension reads from the logged-in pages and writes
`~/.ctxport/inbox/<platform>/<id>.json` (+ `_account.json`, `_index.json`):

* `chatgpt`: `GET /backend-api/conversation/<id>` — same `mapping` tree as the official export
  (list: `/backend-api/conversations?offset&limit&order=updated[&is_archived=true]`, projects via
  `/backend-api/gizmos/snorlax/sidebar` + `/backend-api/gizmos/<id>/conversations`; memories
  `/backend-api/memories`, custom instructions `/backend-api/user_system_messages`).
* `claude-ai`: `GET /api/organizations/<org>/chat_conversations/<uuid>?tree=True&rendering_mode=messages&render_all_tools=true`
  — same `chat_messages` shape as the export plus `parent_message_uuid` / `current_leaf_message_uuid` (branches kept).
* `gemini-web`: DOM scrape `{id, title, url, scrapedAt, messages:[{role: user|model, kind?, text}]}` from
  `user-query` / `model-response` / `model-thoughts` elements.

These endpoints are the sites' own (non-public) front-end APIs; shapes were taken from their responses as used by
the web apps and are **not verified against live accounts in this repository's tests** (tests use mocks).

## Generic

* `openai-messages`: `[{role, content}]`, `{messages:[…]}`, or a list of those; JSONL of messages.
* `ctxport-jsonl`: `{"type":"conversation","conversation":{…}}` and `{"type":"memory","memory":{…}}` lines — lossless.

## Real-data import result (2026-11-23, numbers only)

`ctxport sync --extract && ctxport stats && ctxport export markdown`, numbers only:

| platform | units | conversations | main-line records | of which user / assistant dialogue | subagent | archived | recovered from SQLite | warnings |
|---|---|---|---|---|---|---|---|---|
| claude-code | 218 | 172 | 27,947 | 1,212 user prompts | 1 | – | – | 0 |
| codex | 17 | 184 | 39,774 | 1,457 user prompts | 49 | 14 | 172 | 0 |
| cherry-studio | 1 (`agents.db`) | 1 | 3 | – | – | – | – | 0 |
| gemini-cli / antigravity | 0 | 0 | – | – | – | – | – | 0 |

All records by kind: tool_call 20,954 · tool_result 19,670 · reasoning 15,361 · message 10,524 · system_prompt 907 ·
attachment 317 · artifact 8. Memories: 25 accepted, 756 candidates (incl. 26 from rule extraction).
Markdown archive: 357 conversation files + INDEX.md + MEMORY.md, ~1 s. A second `sync` skipped every unit
except the Claude Code session that was being written at that moment.

Spot check (source → store → Markdown, compared by exact text, no content printed): the largest Claude Code
session, the largest Codex rollout, and the largest SQLite-recovered Codex thread — every user/assistant
message present, in order (363 / 386 / 788 messages; for the recovered thread the 788 source dialogue items
equal the stored sequence exactly).
