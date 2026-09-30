"""Where did this file come from? Walk run records back from an output to its inputs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .capture import file_identity, identity_token
from .config import DEFAULT_MAX_HASH_BYTES
from .findings import BLOCK, WARN, Finding
from .store import Store


@dataclass
class Node:
    path: str
    token: str
    producer: dict[str, Any] | None = None
    stale: bool = False  # a recorded run wrote this path, but with different content
    inputs: list[Node] = field(default_factory=list)


def _index(store: Store) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_token: dict[str, list[dict[str, Any]]] = {}
    by_path: dict[str, list[dict[str, Any]]] = {}
    for rec in store.all():
        for path, ident in ((rec.get("files") or {}).get("written") or {}).items():
            by_token.setdefault(identity_token(ident), []).append(rec)
            by_path.setdefault(path, []).append(rec)
    return by_token, by_path


def trace(
    path: str, store: Store, *, depth: int = 6, max_bytes: int = DEFAULT_MAX_HASH_BYTES
) -> tuple[Node, list[Finding]]:
    by_token, by_path = _index(store)
    findings: list[Finding] = []
    seen: set[str] = set()

    def visit(p: str, token: str, level: int) -> Node:
        node = Node(p, token)
        producers = [r for r in by_token.get(token, []) if not token.startswith("<")]
        if producers:
            node.producer = producers[-1]
        elif by_path.get(p):
            node.producer = by_path[p][-1]
            node.stale = True
        rec = node.producer
        if rec is None:
            if level == 0:
                findings.append(Finding(WARN, "NO_RECORD", "no recorded run produced this content", subject=p))
            return node
        rid = rec.get("id", "?")
        if node.stale and level == 0:
            findings.append(
                Finding(
                    WARN,
                    "CHANGED_SINCE_RUN",
                    "the file was written by a recorded run but has changed since",
                    subject=p,
                    run=rid,
                )
            )
        if rec.get("status") != "ok":
            findings.append(
                Finding(
                    BLOCK, "FROM_FAILED_RUN", f"produced by a run with status {rec.get('status')!r}", subject=p, run=rid
                )
            )
        if rid in seen or level >= depth:
            return node
        seen.add(rid)
        for ip, ident in ((rec.get("files") or {}).get("read") or {}).items():
            if not ident.get("self_written"):
                node.inputs.append(visit(ip, identity_token(ident), level + 1))
        return node

    ap = os.path.abspath(path)
    token = identity_token(file_identity(ap, max_bytes))
    return visit(ap, token, 0), findings


def render(node: Node, indent: int = 0) -> str:
    pad = "  " * indent
    if node.producer is None:
        line = f"{pad}{node.path}  (no record)"
    else:
        r = node.producer
        d = r.get("declared") or {}
        label = "/".join(x for x in (d.get("name"), d.get("arm")) if x)
        main = (r.get("main") or {}).get("path") or " ".join((r.get("argv") or [])[:1])
        line = (
            f"{pad}{node.path}\n{pad}  <- run {r.get('id')} {label} [{r.get('status')}]"
            f"{' (content changed since)' if node.stale else ''}: {main}"
        )
    return "\n".join([line, *[render(c, indent + 2) for c in node.inputs]])
