"""Keep secrets out of run records.

A value is treated as secret when its name looks like a credential (``*_TOKEN``, ``DB_PASS``...)
or when the value itself does (URL userinfo, well-known key prefixes, ``password=`` pairs). Secret
values are replaced by a keyed digest (HMAC-SHA256 with a per-store random key), so two runs in
the same store still compare equal or unequal, but the value cannot be recovered by guessing.
"""

from __future__ import annotations

import hashlib
import hmac
import re

_SECRET_WORDS = frozenset(
    [
        "key",
        "apikey",
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "pass",
        "passphrase",
        "auth",
        "credential",
        "credentials",
        "cookie",
        "sessionid",
        "jwt",
        "dsn",
        "webhook",
        "pat",
        "signature",
        "privatekey",
    ]
)
_NOT_SECRET_WORDS = frozenset(
    "max min num n count limit len length size budget per tokens tokenizer tokenizers".split()  # noqa: SIM905
)
_CONN_STRING = re.compile(r"CONN(ECTION)?_?STR", re.I)


def secret_name(name: str) -> bool:
    """Does a variable or flag name look like it holds a credential? (``HF_TOKEN``, ``--api-key``;
    not ``TOKENIZERS_PARALLELISM`` or ``--max-tokens``)."""
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    if _CONN_STRING.search(name):
        return True
    return any(w in _SECRET_WORDS for w in words) and not any(w in _NOT_SECRET_WORDS for w in words)


_URL_PASSWORD = re.compile(r"(://[^/\s:@]+:)([^/\s@]+)(@)")
_URL_USER = re.compile(r"(://)([^/\s:@]+)(@)")
_PAIR = re.compile(
    r"(?i)\b(password|passwd|pwd|accountkey|sharedaccesskey|sig|secret|token|apikey|api_key|access_token)=([^&\s'\";,]+)"
)
_AUTH = re.compile(r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._~+/=-]{8,})")
_KEY_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-|ghp_|gho_|ghs_|ghu_|github_pat_|hf_|xox[abprs]-|AKIA|ASIA|AIza|glpat-|eyJ)[A-Za-z0-9_\-.]{8,}"
)
_SLACK = re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+")
_FLAG = re.compile(r"^(--?[A-Za-z][\w.-]*)(?:=(.*))?$", re.S)
_INLINE_FLAG = re.compile(
    r"(?i)(--?(?:api[-_]?key|access[-_]?key|secret|password|passwd|pwd|token|auth[-_]?token)[=\s]+)([^\s'\",]+)"
)
_VALUE_PATTERNS = (_URL_PASSWORD, _PAIR, _KEY_PREFIX, _SLACK)


def secret_value(value: str) -> bool:
    """Does a value carry a credential (URL password, ``password=``, a known key format)?"""
    return any(p.search(value) for p in _VALUE_PATTERNS)


def digest(key: bytes, value: str) -> str:
    return "hmac:" + hmac.new(key, value.encode("utf-8", "surrogateescape"), hashlib.sha256).hexdigest()[:32]


def is_secret(name: str, value: str) -> bool:
    return secret_name(name) or secret_value(value)


def text(key: bytes, s: str) -> str:
    """Replace only the secret parts of free text: URL passwords, ``password=`` values, bearer
    tokens, well-known key formats, and the value after ``--api-key``/``--token``."""

    def d(v: str) -> str:
        return digest(key, v)

    s = _URL_PASSWORD.sub(lambda m: m.group(1) + d(m.group(2)) + m.group(3), s)
    s = _PAIR.sub(lambda m: f"{m.group(1)}={d(m.group(2))}", s)
    s = _AUTH.sub(lambda m: f"{m.group(1)} {d(m.group(2))}", s)
    s = _INLINE_FLAG.sub(lambda m: m.group(1) + d(m.group(2)), s)
    s = _KEY_PREFIX.sub(lambda m: d(m.group(0)), s)
    return _SLACK.sub(lambda m: d(m.group(0)), s)


def argv(key: bytes, tokens: list[str]) -> list[str]:
    """Redact the values of secret-looking flags (``--hf-token X``, ``--password=X``) and secret values."""
    out: list[str] = []
    hide_next = False
    for t in tokens:
        if hide_next and not t.startswith("-"):
            out.append(digest(key, t))
            hide_next = False
            continue
        hide_next = False
        m = _FLAG.match(t)
        if m and secret_name(m.group(1)) and m.group(2) is not None:
            out.append(f"{m.group(1)}={digest(key, m.group(2))}")
        elif m and secret_name(m.group(1)):
            hide_next = True
            out.append(t)
        else:
            out.append(text(key, t))
    return out
