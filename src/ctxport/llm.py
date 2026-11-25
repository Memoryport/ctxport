"""Minimal OpenAI-compatible chat client (urllib only). Defaults to a local Ollama server."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:7b"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


class RemoteNotAllowed(RuntimeError):
    pass


# OpenAI-compatible endpoints. The user chooses the provider and the model; ctxport ships no model names,
# because they change too often to hard-code.
PRESETS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"label": "Anthropic (Claude, OpenAI-compatible endpoint)", "base_url": "https://api.anthropic.com/v1"},
    "gemini": {"label": "Google Gemini (OpenAI-compatible endpoint)",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
    "moonshot": {"label": "Moonshot / Kimi", "base_url": "https://api.moonshot.cn/v1"},
    "qwen": {"label": "Qwen (DashScope compatible mode)", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    "ollama": {"label": "Ollama (local)", "base_url": "http://localhost:11434/v1"},
    "lmstudio": {"label": "LM Studio (local)", "base_url": "http://localhost:1234/v1"},
}


def config_path():
    from .store import default_home
    return default_home() / "llm.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def save_config(cfg: dict) -> None:
    """~/.ctxport/llm.json with 0600 permissions. The API key is stored only if the user passes one."""
    from .store import ensure_private_dir, private_write_text
    ensure_private_dir(config_path().parent)
    clean = {k: v for k, v in cfg.items() if k in ("preset", "base_url", "model", "api_key", "allow_remote") and v not in (None, "")}
    private_write_text(config_path(), json.dumps(clean, ensure_ascii=False, indent=2))


def client_from_config(**overrides) -> "LLMClient":
    """Build a client from ~/.ctxport/llm.json, environment variables and explicit overrides (in that order of
    increasing priority). Raises if no model was chosen: ctxport never picks a model for the user."""
    cfg = load_config()
    base_url = overrides.get("base_url") or os.environ.get("CTXPORT_LLM_BASE_URL") or cfg.get("base_url") or ""
    preset = overrides.get("preset") or cfg.get("preset")
    if not base_url and preset in PRESETS:
        base_url = PRESETS[preset]["base_url"]
    model = overrides.get("model") or os.environ.get("CTXPORT_LLM_MODEL") or cfg.get("model") or ""
    if not base_url or not model:
        raise RuntimeError("还没有设置模型：运行 `ctxport llm set --preset <服务商> --model <模型名> [--api-key …]`，"
                           "或在 `ctxport app` 里设置。")
    api_key = overrides.get("api_key") or os.environ.get("CTXPORT_LLM_API_KEY") or cfg.get("api_key") or ""
    allow_remote = bool(overrides.get("allow_remote") or cfg.get("allow_remote"))
    return LLMClient(base_url=base_url, model=model, api_key=api_key, allow_remote=allow_remote)


def is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


@dataclass
class LLMClient:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    allow_remote: bool = False
    timeout: float = 300.0
    usage: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or os.environ.get("CTXPORT_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = self.model or os.environ.get("CTXPORT_LLM_MODEL") or DEFAULT_MODEL
        self.api_key = self.api_key or os.environ.get("CTXPORT_LLM_API_KEY") or ""
        if not is_local(self.base_url) and not self.allow_remote:
            raise RemoteNotAllowed(
                f"{self.base_url} is not a local address. ctxport only sends your data to a remote LLM when you "
                "pass --allow-remote explicitly.")

    def chat(self, messages: list, temperature: float = 0.1, max_tokens: Optional[int] = None) -> str:
        body = {"model": self.model, "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            body["max_tokens"] = max_tokens
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach LLM at {self.base_url}: {e.reason}") from e
        usage = data.get("usage") or {}
        self.usage["calls"] = self.usage.get("calls", 0) + 1
        for k in ("prompt_tokens", "completion_tokens"):
            self.usage[k] = self.usage.get(k, 0) + int(usage.get(k) or 0)
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def parse_json_array(text: str) -> list:
    """Tolerate chatter / code fences around the JSON array the model was asked for."""
    if not text:
        return []
    t = text.strip()
    for candidate in (t,):
        try:
            v = json.loads(candidate)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k in ("memories", "items", "facts", "data"):
                    if isinstance(v.get(k), list):
                        return v[k]
        except ValueError:
            pass
    start = t.find("[")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, list):
                            return v
                    except ValueError:
                        pass
                    break
        start = t.find("[", start + 1)
    return []


def parse_json_object(text: str) -> dict:
    """The first JSON object in a reply (tolerates code fences and chatter around it)."""
    if not text:
        return {}
    t = text.strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {}
    except ValueError:
        pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        pass
                    break
        start = t.find("{", start + 1)
    return {}
