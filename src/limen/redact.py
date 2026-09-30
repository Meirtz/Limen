"""Keep secrets out of run records.

A value is treated as secret when its name looks like a credential (``HF_TOKEN``, ``PGPASSWORD``,
``--dbPassword``) or when the value itself does (URL passwords, well-known key formats). Secret
values are replaced by a keyed digest (HMAC-SHA256 with a per-store random key), so two runs in
the same store still compare equal or unequal, but the value cannot be recovered by guessing.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

# words that name a credential on their own, and words that make an otherwise-weak word innocent
_SECRET_WORDS = frozenset(
    [
        "key",
        "keys",
        "apikey",
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "pwd",
        "pass",
        "passphrase",
        "auth",
        "authtoken",
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
        "accesstoken",
    ]
)
_WEAK_WORDS = frozenset({"key", "keys", "token", "tokens", "pass", "auth"})
_NOT_SECRET_WORDS = frozenset(
    [
        "max",
        "min",
        "num",
        "n",
        "count",
        "limit",
        "len",
        "length",
        "size",
        "budget",
        "per",
        "tokenizer",
        "tokenizers",
        "id",
        "ids",
        "pad",
        "eos",
        "bos",
        "unk",
        "sep",
        "cls",
        "at",
        "dim",
        "dims",
        "type",
        "label",
        "sort",
        "metric",
        "field",
        "column",
        "col",
        "heads",
        "value",
        "values",
        "vocab",
        "embedding",
        "bucket",
        "prefix",
        "through",
        "map",
        "mapping",
        "name",
        "path",
        "file",
        "dir",
    ]
)
_STRONG = re.compile(r"passw|passphrase|secret|apikey|api_key|authtoken|accesstoken|credential|privatekey", re.I)
_CONN_STRING = re.compile(r"conn(ection)?_?str", re.I)


def _words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)  # dbPassword -> db_Password
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def secret_name(name: str) -> bool:
    """Does a variable, flag or key name look like it holds a credential?

    ``HF_TOKEN``, ``--api-key``, ``PGPASSWORD``, ``NGROK_AUTHTOKEN``, ``--dbPassword``: yes.
    ``TOKENIZERS_PARALLELISM``, ``--max-tokens``, ``pad_token_id``, ``KEY_FIELD``: no.
    """
    if _CONN_STRING.search(name) or _STRONG.search(name.replace("-", "_")):
        return True
    words = _words(name)
    hits = [w for w in words if w in _SECRET_WORDS]
    if not hits:
        return False
    if any(w not in _WEAK_WORDS for w in hits):
        return True
    return not any(w in _NOT_SECRET_WORDS for w in words)


# credential shapes inside values and free text
_URL_PASSWORD = re.compile(r"(://[^/\s:@]*:)([^\s@/]+)(@)")
_URL_TOKEN_USER = re.compile(r"(://)([A-Za-z0-9_\-.]{20,})(@)")
_KEY_FORMATS = re.compile(
    r"(?<![\w/.-])(?:"
    r"sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{36}|gh[ousr]_[A-Za-z0-9]{36}|"
    r"github_pat_\w{50,}|glpat-[A-Za-z0-9_-]{20}|xox[abprs]-[\w-]{10,}|A[KS]IA[0-9A-Z]{16}|AIza[\w-]{35}|"
    r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*"
    r")"
)
_SLACK = re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+")
_QUERY_PAIR = re.compile(
    r"(?i)([?&;](?:sig|signature|accountkey|sharedaccesskey|token|access_token|apikey|api_key|key|password)=)"
    r"([^&\s'\"]+)"
)
_ASSIGN = re.compile(r"(?<![\w.-])([A-Za-z_][\w.-]*)=([^\s&;'\",]+)")
_HEADER = re.compile(
    r"(?i)\b((?:x-)?(?:api[-_]?key|[\w-]*token|[\w-]*secret|authorization))\s*:\s*((?:bearer|basic|token)\s+)?(\S+)"
)
_AUTH = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})")
_PASSWORD_TOOLS = frozenset({"mysql", "mysqldump", "mysqladmin", "twine", "sshpass", "psql", "mariadb"})


def digest(key: bytes, value: str) -> str:
    return "hmac:" + hmac.new(key, value.encode("utf-8", "surrogateescape"), hashlib.sha256).hexdigest()[:32]


def secret_value(value: str) -> bool:
    """Does a value carry a credential (URL password, a known key format, a webhook)?"""
    return bool(_URL_PASSWORD.search(value) or _KEY_FORMATS.search(value) or _SLACK.search(value))


def is_secret(name: str, value: str) -> bool:
    return secret_name(name) or secret_value(value)


def text(key: bytes, s: str) -> str:
    """Replace only the secret parts of free text: URL passwords, secret assignments and query
    parameters, credential headers, bearer tokens, and well-known key formats."""

    def d(v: str) -> str:
        return digest(key, v)

    def assign(m: re.Match[str]) -> str:
        return f"{m.group(1)}={d(m.group(2))}" if secret_name(m.group(1).rsplit(".", 1)[-1]) else m.group(0)

    s = _URL_PASSWORD.sub(lambda m: m.group(1) + d(m.group(2)) + m.group(3), s)
    s = _URL_TOKEN_USER.sub(lambda m: m.group(1) + d(m.group(2)) + m.group(3), s)
    s = _QUERY_PAIR.sub(lambda m: m.group(1) + d(m.group(2)), s)
    s = _ASSIGN.sub(assign, s)
    s = _HEADER.sub(lambda m: f"{m.group(1)}: {m.group(2) or ''}{d(m.group(3))}", s)
    s = _AUTH.sub(lambda m: f"{m.group(1)} {d(m.group(2))}", s)
    s = _KEY_FORMATS.sub(lambda m: d(m.group(0)), s)
    return _SLACK.sub(lambda m: d(m.group(0)), s)


_FLAG = re.compile(r"^(--?[A-Za-z][\w.-]*)(?:=(.*))?$", re.S)


def argv(key: bytes, tokens: list[str]) -> list[str]:
    """Redact a command line: values of secret-looking flags (``--hf-token X``, ``--password=X``),
    the value after ``-u``/``--user``, glued ``-pPASSWORD`` for database clients, and secret text
    in any token."""
    out: list[str] = []
    hide_next = False
    tool = os.path.basename(tokens[0]) if tokens else ""
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
        elif t in ("-u", "--user") and tool not in ("python", "python3", "pip", "uv"):
            out.append(t)
            hide_next = True  # user:password for curl and friends
        elif tool in _PASSWORD_TOOLS and t.startswith("-p") and len(t) > 2:
            out.append("-p" + digest(key, t[2:]))
        else:
            out.append(text(key, t))
    return out
