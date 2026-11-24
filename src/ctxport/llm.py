"""Minimal OpenAI-compatible chat client (urllib only). Defaults to a local Ollama server."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:7b"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


class RemoteNotAllowed(RuntimeError):
    pass


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
