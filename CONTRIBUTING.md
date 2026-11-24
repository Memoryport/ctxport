# Contributing / 贡献指南

## Ground rules / 基本规则

1. **No network.** Importers and exporters must not make network requests. Only `llm.py` talks to an LLM,
   and only when the user passes `--llm` (remote endpoints additionally need `--allow-remote`).
   不允许联网（LLM 提取除外，且需用户显式开启）。The browser extension is the only other component that
   talks to the network: it may only read the user's own data from the site tab they are logged into and may only
   send it to `http://127.0.0.1` — never request the `cookies` permission or touch saved credentials.
2. **Never write another app's data.** Read SQLite through `utils.sqlite_copy()` (copies `-wal`/`-shm` to a temp dir).
   Instruction files are only changed through `utils.write_managed()`.
   不允许写入其他应用的数据库或会话文件。
3. **Synthetic fixtures only.** Test data must be made up or fully anonymised. Never commit real conversations,
   exports, databases (`*.db`, `*.zip` are git-ignored for a reason).
   样本必须是合成或脱敏的。
4. **Core stays stdlib-only.** Third-party packages are allowed only behind an optional extra (like `ctxport[mcp]`).
5. **Fault tolerant.** A malformed file → a warning in `ImportResult.warnings`, never an exception that stops `sync`.
6. Record real-format observations (structure only, no content) in `docs/formats.md`.

## Add a platform in one file / 一个文件新增一个平台

```python
# src/ctxport/adapters/myapp.py
from pathlib import Path

from ..models import Conversation, ImportResult, Message, OutputFile
from ..utils import Bundle, home, parse_time
from ._render import render_memories_plain
from .base import ExportOptions, Exporter, Importer, register


@register
class MyAppImporter(Importer):
    name = "myapp"                      # CLI name: ctxport import myapp …
    label = "My App"
    how_to_get = "My App → Settings → Export; pass the .zip"
    experimental = True

    def default_roots(self) -> list:     # auto-detected local data; [] for export-file platforms
        return [home() / ".myapp"]

    def discover(self, root: Path) -> list:  # one unit per independently re-importable file
        return sorted(Path(root).glob("sessions/*.json")) if Path(root).is_dir() else [Path(root)]

    def load(self, unit: Path) -> ImportResult:
        res = ImportResult()
        with Bundle(unit) as b:          # same API for folders, zips and single files
            for name in b.find(r"\.json$"):
                data = b.read_json(name)
                conv = Conversation(platform=self.name, native_id=data["id"], title=data.get("title", ""))
                for m in data["messages"]:
                    conv.messages.append(Message(role=m["role"], text=m["text"],
                                                 created_at=parse_time(m.get("time"))))
                res.conversations.append(conv.finalize())
        return res


@register
class MyAppExporter(Exporter):
    name = "myapp"
    label = "My App (paste text)"
    instructions = "Paste myapp-memory.txt into My App → Settings → Memory."

    def render(self, store, memories, opts: ExportOptions) -> list:
        return [OutputFile("myapp/myapp-memory.txt", render_memories_plain(memories, opts.redact))]
```

Then add the module name to `_MODULES` in `src/ctxport/adapters/__init__.py`, a synthetic builder in
`tests/fixtures.py`, and a test in `tests/test_importers.py` that checks main-line content **and order**,
branches, and that injected/metadata messages get the right `kind`.

For instruction-file targets subclass `_instruction.InstructionFileExporter` (set `filename`, `global_path`,
optional `max_chars`) — you get managed blocks, backups and `--project` for free.

Message kinds: `message`, `tool_call`, `tool_result`, `reasoning`, `artifact`, `attachment`, `system_prompt`.
Roles: `user`, `assistant`, `system`, `tool`, `unknown`.

## Dev loop

```bash
pip install -e ".[mcp,dev]"
pytest -q
```

Commits and PRs must not contain real user data.
