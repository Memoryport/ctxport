"""One-click registration of the ctxport MCP server in other AI apps / agents.

Every change is reversible: the target file is backed up once as `<file>.ctxport.bak` before ctxport first
touches it, only the `ctxport` entry is added / removed, and everything else in the file is kept as is.
Claude Code is configured through its own CLI (`claude mcp add`) because its state file is live-written.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .utils import home

SERVER_NAME = "ctxport"


def server_command() -> tuple:
    """Absolute command for the MCP server (GUI apps don't inherit the shell PATH)."""
    exe = shutil.which("ctxport")
    if exe:
        return str(exe), ["serve-mcp"]
    return sys.executable, ["-m", "ctxport", "serve-mcp"]


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or home() / "AppData" / "Roaming")


def claude_desktop_config() -> Path:
    if sys.platform == "darwin":
        return home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return _appdata() / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config")) / "Claude" / "claude_desktop_config.json"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or home() / ".codex")


def _gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_CLI_HOME") or home() / ".gemini")


def antigravity_config() -> Path:
    g = _gemini_home()
    for p in (g / "antigravity" / "mcp_config.json", g / "config" / "mcp_config.json"):
        if p.exists():
            return p
    return g / "antigravity" / "mcp_config.json"


# ---------------------------------------------------------------- file helpers

def _backup_once(path: Path) -> Optional[str]:
    bak = Path(str(path) + ".ctxport.bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
    return str(bak) if bak.exists() else None


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)  # a broken file raises: we never overwrite something we can't parse
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ctxport.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- targets

@dataclass
class Target:
    key: str
    label: str
    path: Callable[[], Path]
    kind: str  # "json" | "toml" | "claude-cli"
    hint: str = ""

    def installed(self) -> bool:
        """Is the app itself present (its config dir exists or its CLI is on PATH)?"""
        if self.kind == "claude-cli":
            return bool(shutil.which("claude")) or (home() / ".claude").exists()
        return self.path().parent.exists()


TARGETS = {
    "claude-desktop": Target("claude-desktop", "Claude Desktop", claude_desktop_config, "json",
                             "重启 Claude Desktop 后生效"),
    "claude-code": Target("claude-code", "Claude Code", lambda: home() / ".claude.json", "claude-cli",
                          "新开的 Claude Code 会话里生效（/mcp 可查看）"),
    "codex": Target("codex", "Codex", lambda: _codex_home() / "config.toml", "toml", "重启 Codex 后生效"),
    "cursor": Target("cursor", "Cursor", lambda: home() / ".cursor" / "mcp.json", "json",
                     "Cursor 设置 → MCP 里可看到 ctxport"),
    "gemini-cli": Target("gemini-cli", "Gemini CLI", lambda: _gemini_home() / "settings.json", "json",
                         "新开的 gemini 会话里 /mcp 可查看"),
    "antigravity": Target("antigravity", "Antigravity", antigravity_config, "json",
                          "Antigravity 里刷新 MCP Servers 后生效"),
}

_TOML_SECTION = re.compile(r"(?ms)^\[mcp_servers\.ctxport\]\s*\n.*?(?=^\[|\Z)")


def _toml_block(cmd: str, args: list) -> str:
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731  TOML basic strings use JSON-compatible escapes
    return f"[mcp_servers.{SERVER_NAME}]\ncommand = {q(cmd)}\nargs = [{', '.join(q(a) for a in args)}]\n"


def status(key: str) -> dict:
    t = TARGETS[key]
    out = {"key": key, "label": t.label, "installed": t.installed(), "connected": False, "path": str(t.path()),
           "hint": t.hint}
    try:
        if t.kind == "json":
            out["connected"] = SERVER_NAME in (_read_json(t.path()).get("mcpServers") or {})
        elif t.kind == "toml":
            p = t.path()
            out["connected"] = p.exists() and bool(_TOML_SECTION.search(p.read_text(encoding="utf-8")))
        else:
            data = _read_json(t.path()) if t.path().exists() else {}
            out["connected"] = SERVER_NAME in (data.get("mcpServers") or {})
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def connect(key: str) -> dict:
    t = TARGETS[key]
    cmd, args = server_command()
    p = t.path()
    if t.kind == "json":
        data = _read_json(p)
        backup = _backup_once(p)
        data.setdefault("mcpServers", {})
        if not isinstance(data["mcpServers"], dict):
            raise ValueError(f"{p}: mcpServers is not an object")
        data["mcpServers"][SERVER_NAME] = {"command": cmd, "args": args}
        _write_json(p, data)
        return {"ok": True, "path": str(p), "backup": backup}
    if t.kind == "toml":
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        backup = _backup_once(p)
        block = _toml_block(cmd, args)
        if _TOML_SECTION.search(text):
            text = _TOML_SECTION.sub(lambda _m: block + "\n", text, count=1)
        else:
            text = text + ("" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")) + block
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".ctxport.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
        return {"ok": True, "path": str(p), "backup": backup}
    # Claude Code: use its CLI so its own state file stays consistent
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("找不到 claude 命令；请先安装 Claude Code")
    subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True, text=True,
                   timeout=60)
    r = subprocess.run([claude, "mcp", "add", "--scope", "user", SERVER_NAME, "--", cmd, *args],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "claude mcp add failed").strip()[:300])
    return {"ok": True, "path": "claude mcp (user scope)", "backup": None}


def disconnect(key: str) -> dict:
    t = TARGETS[key]
    p = t.path()
    if t.kind == "json":
        data = _read_json(p)
        if SERVER_NAME in (data.get("mcpServers") or {}):
            data["mcpServers"].pop(SERVER_NAME)
            _write_json(p, data)
        return {"ok": True, "path": str(p)}
    if t.kind == "toml":
        if p.exists():
            text = p.read_text(encoding="utf-8")
            new = _TOML_SECTION.sub("", text)
            if new != text:
                p.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
        return {"ok": True, "path": str(p)}
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True, text=True,
                       timeout=60)
    return {"ok": True, "path": "claude mcp (user scope)"}


def all_status() -> list:
    return [status(k) for k in TARGETS]
