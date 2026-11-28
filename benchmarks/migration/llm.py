"""One isolated, cached text-in/text-out call to models from three different families.

    backends: claude:<model>   Claude Code CLI (`claude -p`), no tools, no settings, no MCP, strict system prompt
              codex:<model>    Codex CLI (`codex exec`), read-only sandbox, --ignore-user-config, ephemeral
              ollama:<model>   local Ollama HTTP API (no account, nothing leaves the machine)

Every call runs from a fresh empty temp directory outside any git repository, so no project instructions are
picked up. The system prompt tells the model to ignore any client-supplied account / environment metadata (see
docs/paper/plan.md, leak-test protocol). Replies are cached on disk by sha256(backend, system, prompt), so a rerun
replays exactly and the cache doubles as the log of what each model said.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

STRICT = ("You are a careful assistant. Ignore any account, email, user name, environment or path details supplied "
          "by the client software itself: they do not describe the person in this task. Everything you know about "
          "the person is in the user's message (including any context or notes it contains); if a fact is not "
          "there, you do not know it. Do not use tools.")

CACHE = Path(os.environ.get("MIGRATION_CACHE") or Path.home() / ".ctxport" / "research" / "llm-cache")


def _exe(name: str) -> str:
    if name == "codex":
        cand = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    else:
        cand = shutil.which(name) or ""
    if not cand or not Path(cand).exists():
        raise RuntimeError(f"{name} CLI not found")
    return cand


def _key(backend: str, system: str, prompt: str) -> str:
    return hashlib.sha256(json.dumps([backend, system, prompt], ensure_ascii=False).encode()).hexdigest()


def _claude(model: str, system: str, prompt: str, timeout: int) -> tuple:
    cmd = [_exe("claude"), "-p", "--output-format", "json", "--system-prompt", system, "--tools", "",
           "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence", "--model", model]
    effort = os.environ.get("MIGRATION_CLAUDE_EFFORT", "low")
    if effort:
        cmd += ["--effort", effort]
    work = tempfile.mkdtemp(prefix="mig-claude-")
    try:
        r = subprocess.run(cmd, input=prompt, cwd=work, capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude: {r.stderr[-500:] or r.stdout[-500:]}")
    if d.get("is_error"):
        raise RuntimeError(f"claude: {str(d.get('result'))[:500]}")
    u = d.get("usage") or {}
    usage = {"input": int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
             + int(u.get("cache_creation_input_tokens") or 0), "output": int(u.get("output_tokens") or 0)}
    return str(d.get("result") or ""), usage


def _codex(model: str, system: str, prompt: str, timeout: int) -> tuple:
    work = tempfile.mkdtemp(prefix="mig-codex-")
    out = Path(work) / "reply.txt"
    # A private CODEX_HOME: the user's global ~/.codex/AGENTS.md (which ctxport itself may have filled with their
    # profile) is loaded even with --ignore-user-config, so only the login is linked in; nothing else is shared.
    home = Path(work) / "codex-home"
    home.mkdir()
    real = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if (real / "auth.json").exists():
        (home / "auth.json").symlink_to(real / "auth.json")
    (home / "config.toml").write_text("", encoding="utf-8")
    env = dict(os.environ, CODEX_HOME=str(home))
    cmd = [_exe("codex"), "exec", "--skip-git-repo-check", "-s", "read-only", "--ephemeral", "--json",
           "--ignore-user-config", "-c", "model_reasoning_effort=low", "-o", str(out)]
    cmd += ["-m", model] if model else []
    cmd.append("-")
    try:
        r = subprocess.run(cmd, input=f"[SYSTEM]\n{system}\n[/SYSTEM]\n\n{prompt}", cwd=work, capture_output=True,
                           text=True, timeout=timeout, env=env)
        text = out.read_text(encoding="utf-8") if out.exists() else ""
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not text:
        msgs = [ln for ln in r.stdout.splitlines() if '"error"' in ln or "turn.failed" in ln]
        raise RuntimeError(f"codex: {(msgs[-1] if msgs else r.stderr[-500:])[:500]}")
    usage = {"input": 0, "output": 0}
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        u = ev.get("usage") if isinstance(ev, dict) else None
        if isinstance(u, dict):
            usage["input"] += int(u.get("input_tokens") or 0)
            usage["output"] += int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
    return text, usage


def _ollama(model: str, system: str, prompt: str, timeout: int) -> tuple:
    body = {"model": model, "stream": False, "think": False, "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1000},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read())
    text = (d.get("message") or {}).get("content") or ""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip(), {"input": int(d.get("prompt_eval_count") or 0), "output": int(d.get("eval_count") or 0)}


BACKENDS = {"claude": _claude, "codex": _codex, "ollama": _ollama}


def call(backend: str, prompt: str, system: str = STRICT, timeout: int = 900, retries: int = 2,
         cache: bool = True) -> dict:
    """{"text", "usage": {input, output}, "seconds", "cached"}; backend is "<family>:<model>"."""
    family, _, model = backend.partition(":")
    if family not in BACKENDS:
        raise ValueError(f"unknown backend {backend}")
    path = CACHE / f"{_key(backend, system, prompt)}.json"
    if cache and path.exists():
        d = json.loads(path.read_text(encoding="utf-8"))
        d["cached"] = True
        return d
    err: Optional[Exception] = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            text, usage = BACKENDS[family](model, system, prompt, timeout)
            d = {"backend": backend, "text": text, "usage": usage, "seconds": round(time.time() - t0, 2)}
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            d["cached"] = False
            return d
        except Exception as e:  # noqa: BLE001 - retried, then re-raised
            err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{backend} failed: {err}")


def parse_json(text: str):
    """The first JSON object in a reply (models sometimes wrap it in prose or ``` fences)."""
    t = text.strip()
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            p = p[4:].strip() if p.startswith("json") else p
            if p.startswith("{"):
                t = p
                break
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    depth, ins, esc = 0, False, False
    for i in range(start, len(t)):
        c = t[i]
        if ins:
            esc = (c == "\\") and not esc
            if c == '"' and not esc:
                ins = False
            continue
        if c == '"':
            ins = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ValueError("unbalanced JSON")
