"""Mask secrets before anything leaves the local store."""
from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS = [
    # private key blocks
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),                 # Anthropic
    re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),  # OpenAI
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),     # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),                       # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"),                    # Google OAuth token
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                  # AWS access key id
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),               # Slack
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
]

_ASSIGN = re.compile(
    r"(?i)\b((?:aws_secret_access_key|password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|auth)"
    r"\s*[:=]\s*)([\"']?)([^\s\"',;]{4,})\2"
)
_ZH_ASSIGN = re.compile(r"(密码|口令|令牌|密钥)(\s*[:：是为]\s*)(\S{4,})")


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for rx in _PATTERNS:
        out = rx.sub(MASK, out)
    out = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(2)}", out)
    out = _ZH_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    return out


def maybe(text: str, enabled: bool = True) -> str:
    return redact(text) if enabled else text
