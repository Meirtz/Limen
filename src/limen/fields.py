"""A run's effective identity as comparable fields, and the treatment/waiver spec language.

Every field is ``kind:name`` (``env:WIKI_ROOT``, ``module:pkg.retrieval``, ``file:data/train.jsonl``,
``arg:--lr``, ``param:lr``, ``dist:torch``, ``protocol:tolerance``) or one of the bare fields
``python``, ``platform`` and ``gpu``. A spec selects fields: ``env:WIKI_ROOT`` selects one field,
a trailing ``*`` or ``/`` selects a prefix (``file:data/wiki/``, ``env:WIKI_*``), ``module:pkg``
also selects its submodules, and ``code`` selects every ``module:`` field.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from typing import Any

from .capture import identity_token

KINDS = ("env", "param", "arg", "file", "module", "dist", "protocol")
BARE = ("python", "platform", "gpu", "code")
ABSENT = "<absent>"
UNSET = "<unset>"


class SpecError(ValueError):
    pass


def parse_spec(spec: str) -> str:
    spec = spec.strip()
    if spec in BARE:
        return spec
    kind, sep, name = spec.partition(":")
    if not sep or kind not in KINDS or not name:
        raise SpecError(
            f"bad spec {spec!r}: expected one of {', '.join(k + ':NAME' for k in KINDS)} or {', '.join(BARE)}"
        )
    return spec


def matches(spec: str, field: str) -> bool:
    if spec == "code":
        return field.startswith("module:")
    if spec.endswith("*"):
        return field.startswith(spec[:-1])
    if spec.endswith("/"):
        return field.startswith(spec)
    if field == spec:
        return True
    return spec.startswith("module:") and field.startswith(spec + ".")


def _short(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


_OVERRIDE = re.compile(r"^(\+\+|\+|~)?([A-Za-z_][\w.\-/@]*)=(.*)$", re.S)


def parse_argv(args: list[str]) -> dict[str, str]:
    """Command-line arguments as ``arg:`` fields.

    ``--k v``, ``--k=v`` and ``-k v`` give ``arg:--k``; repeated flags keep every value (as a JSON
    list); ``--flag`` alone is ``true``; Hydra/OmegaConf overrides ``key=value`` give ``arg:key``;
    other positionals are ``arg:#0``, ``arg:#1``... A boolean flag followed by a positional cannot
    be told apart from a flag with a value: prefer ``--flag=value``, or report values with
    ``limen.param()``.
    """
    vals: dict[str, list[str]] = {}
    pos = 0

    def put(k: str, v: str) -> None:
        vals.setdefault(k, []).append(v)

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            for rest in args[i + 1 :]:
                put(f"arg:#{pos}", rest)
                pos += 1
            break
        if tok.startswith("-") and len(tok) > 1 and not _is_number(tok):
            if "=" in tok and tok.startswith("--"):
                k, v = tok.split("=", 1)
                put(f"arg:{k}", v)
            elif i + 1 < len(args) and not (args[i + 1].startswith("-") and not _is_number(args[i + 1])):
                put(f"arg:{tok}", args[i + 1])
                i += 1
            else:
                put(f"arg:{tok}", "true")
        else:
            m = _OVERRIDE.match(tok)
            if m:
                put(f"arg:{m.group(2)}", m.group(3))
            else:
                put(f"arg:#{pos}", tok)
                pos += 1
        i += 1
    return {k: v[0] if len(v) == 1 else json.dumps(v) for k, v in vals.items()}


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _rel(record: dict[str, Any], path: str) -> str:
    root = record.get("root") or ""
    ap = os.path.abspath(path)
    if root and (ap == root or ap.startswith(root.rstrip(os.sep) + os.sep)):
        return os.path.relpath(ap, root).replace(os.sep, "/")
    return ap


def _output_values(record: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Paths the run wrote, and paths it read as inputs (not its own outputs)."""
    files = record.get("files") or {}
    read = {p for p, v in (files.get("read") or {}).items() if not v.get("self_written")}
    return set(files.get("written") or {}), read


def _names(path: str) -> set[str]:
    parts = [x for x in path.split(os.sep) if x]
    return set(parts) | {os.path.splitext(x)[0] for x in parts}


_LABEL_WORDS = frozenset(
    [
        "out",
        "outs",
        "output",
        "outputs",
        "dir",
        "path",
        "name",
        "tag",
        "label",
        "arm",
        "run",
        "exp",
        "experiment",
        "log",
        "logs",
        "save",
        "ckpt",
        "checkpoint",
        "dest",
        "prefix",
        "suffix",
        "id",
    ]
)


def is_label_key(key: str) -> bool:
    """``--run-name``, ``OUTPUT_DIR``, ``RUN_ID``, ``ARM``: every word names an output or a label.
    (``--model-name`` or ``DATASET_ID`` do not qualify: they name inputs.)"""
    words = [w for w in re.split(r"[^a-z]+", key.lower()) if w]
    return bool(words) and all(w in _LABEL_WORDS for w in words)


def _is_output(record: dict[str, Any], key: str, value: str, written: set[str], read: set[str]) -> bool:
    """Is ``key=value`` an output location or label rather than an input?

    A path value qualifies when the run wrote under it and never read an input under it. A plain
    value qualifies only when the key is a label (``ARM``, ``--run-name``) and the value appears in
    written paths (``out/<arm>.json``) but in no input path.
    """
    if not isinstance(value, str) or not value or len(value) > 4096 or not written:
        return False
    if os.sep in value or value.startswith("."):
        p = os.path.abspath(os.path.join(record.get("cwd") or "", value))
        prefix = p.rstrip(os.sep) + os.sep
        wrote = any(w == p or w.startswith(prefix) for w in written)
        return wrote and not any(r == p or r.startswith(prefix) for r in read)
    if not is_label_key(key):
        return False
    return any(value in _names(w) for w in written) and not any(value in _names(r) for r in read)


def identity(record: dict[str, Any], keep: Iterable[str] = ()) -> dict[str, str]:
    """The run's effective identity: every input that could make two runs differ.

    Fields selected by the run's declared treatment or by ``keep`` are never dropped as outputs.
    """
    out: dict[str, str] = {}
    written, read = _output_values(record)
    specs = [*((record.get("declared") or {}).get("treatment") or []), *keep]

    def output(key: str, name: str, value: str) -> bool:
        return not any(matches(s, key) for s in specs) and _is_output(record, name, value, written, read)

    for name, info in (record.get("modules") or {}).items():
        if info.get("changed_during_run"):
            out[f"module:{name}"] = f"<changed during run {record.get('id')}>"
        else:
            out[f"module:{name}"] = info.get("sha256") or info.get("path") or "?"
    main = record.get("main")
    if main:
        if main.get("changed_during_run"):
            out["module:__main__"] = f"<changed during run {record.get('id')}>"
        else:
            out["module:__main__"] = main.get("sha256") or main.get("path") or "?"
    for dist, ver in (record.get("distributions") or {}).items():
        out[f"dist:{dist}"] = str(ver)

    py = record.get("python") or {}
    if py:
        out["python"] = f"{py.get('implementation', '')} {py.get('version', '')}".strip()
    plat = record.get("platform") or {}
    if plat:
        out["platform"] = f"{plat.get('system', '')}-{plat.get('machine', '')}"
        out["gpu"] = ", ".join(plat.get("gpus") or []) or "none"

    for name, entry in (record.get("env") or {}).items():
        if entry.get("set_by_run"):
            continue
        key = f"env:{name}"
        if not entry.get("present"):
            out[key] = UNSET
            continue
        value = entry.get("value")
        if value is not None and output(key, name, value):
            continue
        out[key] = value if value is not None else str(entry.get("digest") or "sha256:" + str(entry.get("sha256")))

    for name, value in (record.get("params") or {}).items():
        key = f"param:{name}"
        if isinstance(value, str) and output(key, name, value):
            continue
        out[key] = _short(value)

    for path, ident in ((record.get("files") or {}).get("read") or {}).items():
        if ident.get("self_written"):
            continue
        rel = _rel(record, path)
        if ident.get("dir"):
            rel = rel.rstrip("/") + "/"
        out[f"file:{rel}"] = identity_token(ident)

    for k, value in ((record.get("declared") or {}).get("protocol") or {}).items():
        out[f"protocol:{k}"] = _short(value)

    for key, value in parse_argv(list(record.get("argv") or [])[1:]).items():
        if output(key, key[len("arg:") :], value):
            continue
        out[key] = value
    return out


def declared_env_value(record: dict[str, Any], name: str) -> str | None:
    """The value a declared env treatment had when the run started (read or not), if recorded."""
    entry = (record.get("declared_env") or {}).get(name)
    if entry is None:
        return None
    if not entry.get("present"):
        return UNSET
    return str(entry.get("value") if entry.get("value") is not None else entry.get("digest"))
