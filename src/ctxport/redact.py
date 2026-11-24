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
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),                    # GitLab
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),                       # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"),                    # Google OAuth token
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                  # AWS access key id
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),               # Slack
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"),  # Slack webhook
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe
    re.compile(r"\bwhsec_[A-Za-z0-9]{24,}"),                       # Stripe webhook secret
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),                          # Hugging Face
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),                        # npm
    re.compile(r"\bpypi-[A-Za-z0-9_\-]{50,}"),                     # PyPI
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),  # SendGrid
    re.compile(r"\bgsk_[A-Za-z0-9]{40,}"),                         # Groq
    re.compile(r"\bxai-[A-Za-z0-9]{40,}"),                         # xAI
    re.compile(r"\br8_[A-Za-z0-9]{30,}"),                          # Replicate
    re.compile(r"\bdop_v1_[a-f0-9]{64}"),                          # DigitalOcean
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
]

# Authorization headers: "Bearer <token>", "Basic <base64>", "token <gh token>"
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic|token)(\s+)(?=[A-Za-z0-9\-._~+/]*\d)([A-Za-z0-9\-._~+/]{12,}=*)")

# Credentials embedded in URLs: scheme://user:password@host
_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@\"'<>]+:)([^\s@/\"'<>]+)(@)")

# Names whose value is a secret. Matched as the end of an identifier, so DB_PASSWORD, GITHUB_TOKEN,
# OPENAI_API_KEY, "client_secret" and X-Api-Key are all covered.
_SECRET_NAME = (r"(?:aws_secret_access_key|password|passwd|passphrase|pwd|secret|secret[_-]?key|token|"
                r"api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|auth|authorization|"
                r"credentials?|session[_-]?id|cookie)")

# key = value / key: value / "key": "value", with optional quotes around the key, optional `export`.
_ASSIGN = re.compile(
    r"(?i)((?<![A-Za-z0-9])[\"']?(?:[A-Za-z0-9]+[_\-.])*" + _SECRET_NAME + r"[\"']?\s*(?::=|=>|[:=])\s*)"
    r"([\"']?)((?!\[REDACTED\])(?!(?:bearer|basic|token)\s)[^\s\"',;}]{4,})\2"
)
# "my password is hunter2" (value must contain a digit or symbol, so "the token is valid" is left alone)
_EN_IS = re.compile(
    r"(?i)\b((?:password|passphrase|passcode|api[ _-]?key|secret[ _-]?key|access[ _-]?key|token)"
    r"\s+(?:is|was|=)\s*:?\s*)([\"']?)((?!\[REDACTED\])(?=[^\s\"',;]*[0-9!@#$%^&*_\-])[^\s\"',;]{4,})\2"
)
_ZH_ASSIGN = re.compile(r"(密码|口令|令牌|密钥|秘钥)(\s*[:：是为]\s*)(\S{4,})")


def _mask_assign(m: re.Match) -> str:
    return f"{m.group(1)}{m.group(2)}{MASK}{m.group(2)}"


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for rx in _PATTERNS:
        out = rx.sub(MASK, out)
    out = _URL_CREDS.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _ASSIGN.sub(_mask_assign, out)
    out = _EN_IS.sub(_mask_assign, out)
    out = _ZH_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    return out


def maybe(text: str, enabled: bool = True) -> str:
    return redact(text) if enabled else text
