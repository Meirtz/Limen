"""The store: content-addressed objects and bootable OS images.

Everything the system grows is an immutable object addressed by the SHA-256 of its canonical JSON:
capabilities (callable helpers), programs (verified whole solutions, kept as precedents) and kernel
slot versions. An image ("head") names one version of everything plus the mutable bookkeeping
(status, use counts) and points at its parent, so every state the OS passed through can be booted,
diffed or knocked out. ``head_0`` is the empty image: no capabilities, no programs, seed slots.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACTIVE, DORMANT = "active", "dormant"


def canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_of(obj: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


@dataclass
class CapEntry:
    digest: str
    status: str = ACTIVE
    uses: int = 0  # verified stream solutions that executed this capability
    admitted: int = 0  # stream position at admission


@dataclass
class Head:
    """A working copy of an image. ``commit`` freezes it into the store."""

    parent: str | None = None
    seq: int = 0
    caps: dict[str, CapEntry] = field(default_factory=dict)  # insertion order = admission order
    programs: dict[str, str] = field(default_factory=dict)  # task id -> program digest
    slots: dict[str, str] = field(default_factory=dict)  # slot name -> slot digest
    note: str = ""

    def to_obj(self) -> dict[str, Any]:
        return {
            "type": "head",
            "parent": self.parent,
            "seq": self.seq,
            "caps": [[n, e.digest, e.status, e.uses, e.admitted] for n, e in self.caps.items()],
            "programs": dict(sorted(self.programs.items())),
            "slots": dict(sorted(self.slots.items())),
            "note": self.note,
        }

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> Head:
        return cls(
            parent=obj["parent"],
            seq=obj["seq"],
            caps={n: CapEntry(d, s, u, a) for n, d, s, u, a in obj["caps"]},
            programs=dict(obj["programs"]),
            slots=dict(obj["slots"]),
            note=obj.get("note", ""),
        )

    def copy(self) -> Head:
        return Head.from_obj(self.to_obj())


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "objects").mkdir(parents=True, exist_ok=True)
        (root / "refs").mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, obj: dict[str, Any]) -> str:
        d = digest_of(obj)
        path = self.root / "objects" / d[:2] / f"{d}.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(canonical(obj))
            tmp.replace(path)
        with self._lock:
            self._cache[d] = obj
        return d

    def get(self, digest: str) -> dict[str, Any]:
        with self._lock:
            hit = self._cache.get(digest)
        if hit is not None:
            return hit
        obj: dict[str, Any] = json.loads((self.root / "objects" / digest[:2] / f"{digest}.json").read_text())
        with self._lock:
            self._cache[digest] = obj
        return obj

    def commit(self, head: Head) -> str:
        return self.put(head.to_obj())

    def head(self, digest: str) -> Head:
        return Head.from_obj(self.get(digest))

    def set_ref(self, name: str, digest: str) -> None:
        path = self.root / "refs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(digest + "\n")

    def ref(self, name: str) -> str:
        return (self.root / "refs" / name).read_text().strip()

    def resolve(self, name_or_digest: str) -> str:
        if (self.root / "refs" / name_or_digest).exists():
            return self.ref(name_or_digest)
        if len(name_or_digest) < 64:
            matches = list((self.root / "objects" / name_or_digest[:2]).glob(f"{name_or_digest}*.json"))
            if len(matches) == 1:
                return matches[0].stem
        return name_or_digest

    def log(self, event: dict[str, Any]) -> None:
        with self._lock, (self.root / "events.jsonl").open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), **event}, ensure_ascii=False) + "\n")

    def events(self) -> list[dict[str, Any]]:
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def lineage(self, digest: str) -> list[str]:
        out = []
        cur: str | None = digest
        while cur:
            out.append(cur)
            cur = self.get(cur)["parent"]
        return out


class Image:
    """A read-only view of a head: its library source, dependency graph, programs and slots."""

    def __init__(self, store: Store, head: Head) -> None:
        self.store, self.head = store, head

    def cap(self, name: str) -> dict[str, Any]:
        return self.store.get(self.head.caps[name].digest)

    def names(self, status: str | None = ACTIVE) -> list[str]:
        return [n for n, e in self.head.caps.items() if status is None or e.status == status]

    def dependents(self, names: set[str]) -> set[str]:
        """``names`` plus every capability that (transitively) calls one of them."""
        out = set(names)
        changed = True
        while changed:
            changed = False
            for n in self.head.caps:
                if n not in out and out & set(self.cap(n)["deps"]):
                    out.add(n)
                    changed = True
        return out

    def depths(self) -> dict[str, int]:
        """Static depth: 1 for a capability that calls no other capability, else 1 + its deepest callee."""
        memo: dict[str, int] = {}

        def depth(n: str, seen: frozenset[str]) -> int:
            if n in memo:
                return memo[n]
            deps = [d for d in self.cap(n)["deps"] if d in self.head.caps and d not in seen]
            memo[n] = 1 + max((depth(d, seen | {n}) for d in deps), default=0)
            return memo[n]

        return {n: depth(n, frozenset()) for n in self.head.caps}

    def library_source(self, exclude: set[str] | None = None) -> str:
        names = [n for n in self.head.caps if not exclude or n not in exclude]
        caps = [self.cap(n) for n in names]
        imports = sorted({i for c in caps for i in c["imports"]})
        return "\n".join(imports) + ("\n\n" if imports else "") + "\n\n\n".join(c["src"] for c in caps) + "\n"

    def programs(self) -> list[tuple[str, str]]:
        return [(t, self.store.get(d)["src"]) for t, d in sorted(self.head.programs.items())]

    def slot_source(self, slot: str) -> str | None:
        d = self.head.slots.get(slot)
        return None if d is None else str(self.store.get(d)["src"])

    def knockout(self, names: set[str]) -> Head:
        """A new head without ``names``, their dependents, and the programs that call any of them."""
        gone = self.dependents(names)
        h = self.head.copy()
        h.caps = {n: e for n, e in h.caps.items() if n not in gone}
        h.programs = {t: d for t, d in h.programs.items() if not (gone & set(self.store.get(d).get("calls", [])))}
        h.note = f"knockout of {len(gone)} capabilities"
        return h

    def summary(self) -> dict[str, Any]:
        depths = self.depths()
        return {
            "capabilities": len(self.names(ACTIVE)),
            "dormant": len(self.names(DORMANT)),
            "programs": len(self.head.programs),
            "slots": {k: v[:12] for k, v in self.head.slots.items()},
            "max_depth": max(depths.values(), default=0),
            "used": sum(1 for e in self.head.caps.values() if e.uses > 0),
        }
