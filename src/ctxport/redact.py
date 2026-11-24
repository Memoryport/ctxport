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
    re.compile(r"\bLTAI[A-Za-z0-9]{12,30}\b"),                       # Alibaba Cloud AccessKey ID
    re.compile(r"\bAKID[A-Za-z0-9]{13,40}\b"),                       # Tencent Cloud SecretId
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
    r"(?i)\b((?:password|passwd|pwd|passphrase|passcode|api[ _-]?key|secret[ _-]?key|access[ _-]?key|token)"
    r"\s*(?:\s(?:is|was)\s|=|：|:|是|为|\s)\s*)([\"']?)((?!\[REDACTED\])(?=[^\s\"',;]*[0-9!@#$%^&*_\-])[^\s\"',;]{4,})\2"
)
_ZH_ASSIGN = re.compile(r"(密码|口令|令牌|密钥|秘钥)(\s*[:：是为]\s*)(\S{4,})")
# "密码 hunter22": a space alone separates the value, so it must look like one (a Latin letter, digit or symbol)
_ZH_SPACE = re.compile(r"(密码|口令|令牌|密钥|秘钥)(\s+)((?!\[REDACTED\])(?=\S*[A-Za-z0-9!@#$%^&*_\-])[^\s，。,;；]{4,})")
# "护照号是 E12345678" / "passport no. E12345678": only after the word, as the bare number looks like many codes
_PASSPORT = re.compile(r"(?i)((?:护照|passport)(?:号码|号|\s*(?:no\.?|number|#))?\s*(?:[:：是为]|\s(?:is|was)\s)?\s*)"
                       r"([A-Z]{1,2}\d{7,8})(?![A-Za-z0-9])")
# a street address down to the house number: "中关村大街27号", "长安路 1-3 号 5 栋 201 室"
_ZH_ADDRESS = re.compile(r"[^\s\d\x00-\x2f\x3a-\x40在于住到是的我你他她，。、,：:；（(]{1,16}?(?:路|街|大道|巷|弄|胡同)"
                         r"\s*\d+(?:-\d+)?\s*号(?:\s*\d+\s*(?:栋|幢|号楼|单元|楼|层|室))*")


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
    out = _ZH_SPACE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _PASSPORT.sub(lambda m: f"{m.group(1)}{MASK}", out)
    out = _ZH_ADDRESS.sub(MASK, out)
    return _mask_pii(out)


def maybe(text: str, enabled: bool = True) -> str:
    return redact(text) if enabled else text


# Personal identifiers: masked like secrets, and a sentence holding one never becomes a memory. Card and ID numbers
# are checked (Luhn / ID checksum), so long numbers such as nanosecond timestamps are left alone.
_CN_ID = re.compile(r"(?<![\dA-Za-z])\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dA-Za-z])")
_CN_MOBILE = re.compile(r"(?<![\d+])(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<![\d.])\d(?:[ -]?\d){14,18}(?![\d.])")
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)


def _cn_id_ok(s: str) -> bool:
    total = sum(int(c) * w for c, w in zip(s[:17], _ID_WEIGHTS))
    return "10X98765432"[total % 11] == s[17].upper()


def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in s if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask_pii(text: str) -> str:
    text = _CN_ID.sub(lambda m: MASK if _cn_id_ok(m.group(0)) else m.group(0), text)
    text = _CARD.sub(lambda m: MASK if _luhn_ok(m.group(0)) else m.group(0), text)
    return _CN_MOBILE.sub(MASK, text)


_SENSITIVE_WORDS = re.compile(
    r"(?i)(密码|口令|密钥|秘钥|私钥|令牌|验证码|身份证|护照|银行卡|信用卡|卡号|社保号|"
    r"\bpassword\b|\bpasswd\b|\bpassphrase\b|\bapi[ _-]?key\b|\bprivate key\b|"
    r"\bssn\b|social security|passport|credit card|\biban\b)")


# Contact handles and other identifiers that are fine in a transcript but must not become a memory that is pasted
# into every assistant: e-mail addresses, WeChat / QQ ids, licence plates.
_EMAIL = re.compile(r"(?<![\w.+-])(?!git@)[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_CN_PLATE = re.compile(r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领][A-HJ-NP-Z]\s?[A-HJ-NP-Z0-9]{5,6}"
                       r"(?![A-Za-z0-9])")
_IDENTIFIER_WORDS = re.compile(
    r"(?i)(微信号|微信id|\bwxid_|qq号|qq 号|车牌|手机号|电话号码|家庭住址|家庭地址|门牌号|\be-?mail address\b|"
    r"\bphone number\b|\blicen[cs]e plate\b|\bhome address\b)")


def is_sensitive(text: str) -> bool:
    """True when `text` holds a secret, a personal identifier (both masked by redact), a contact handle or plate,
    or a credential-like word. Used to keep such sentences out of memories (extraction, questionnaire answers,
    imported lists)."""
    if not text:
        return False
    if redact(text) != text:
        return True
    return bool(_SENSITIVE_WORDS.search(text) or _EMAIL.search(text) or _CN_PLATE.search(text)
                or _IDENTIFIER_WORDS.search(text))
