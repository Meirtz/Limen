"""Run records on disk: one JSON file per run, plus a content-addressed copy of executed code.

The store is private to its owner: directories are 0700, files 0600, and it ignores itself in
git. It also holds a random key used to digest secret values, so digests cannot be reversed by
guessing and do not match across stores.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = "limen.run/1"
MAX_OBJECT_BYTES = 2 * 1024 * 1024


class RecordNotFound(LookupError):
    pass


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.runs = self.path / "runs"
        self.objects = self.path / "objects"
        self._key: bytes | None = None

    def _ensure(self) -> None:
        """Create the store; it ignores itself so records are not committed by accident."""
        _private_dir(self.path)
        _private_dir(self.runs)
        ignore = self.path / ".gitignore"
        if not ignore.exists():
            try:
                ignore.write_text("# Limen run records: private by default (see SECURITY.md)\n*\n")
            except OSError:
                pass

    def key(self) -> bytes:
        """The store's secret key for digesting secret values (created on first use)."""
        if self._key is None:
            self._ensure()
            path = self.path / "key"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
            self._key = path.read_bytes()
        return self._key

    def save(self, record: dict[str, Any]) -> Path:
        self._ensure()
        target = self.runs / f"{record['id']}.json"
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=1, sort_keys=True, default=str)
                f.write("\n")
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return target

    def put_object(self, src: str, sha256: str) -> None:
        """Keep a copy of an executed source file, so the code survives its original location."""
        dst = self.objects / sha256[:2] / sha256
        if dst.exists():
            return
        try:
            if os.path.getsize(src) > MAX_OBJECT_BYTES:
                return
            self._ensure()
            _private_dir(dst.parent)
            tmp = dst.with_suffix(f".{os.getpid()}.tmp")
            shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, dst)
        except OSError:
            pass

    def object_path(self, sha256: str) -> Path | None:
        p = self.objects / sha256[:2] / sha256
        return p if p.exists() else None

    def all(self) -> list[dict[str, Any]]:
        if not self.runs.is_dir():
            return []
        out = []
        skipped = 0
        for p in sorted(self.runs.glob("*.json")):
            try:
                out.append(load_file(p))
            except (OSError, ValueError, RecursionError):
                skipped += 1
        if skipped:
            print(f"limen: skipped {skipped} unreadable record(s) in {self.runs}", file=sys.stderr)
        out.sort(key=lambda r: (r.get("started_at") or "", r.get("id", "")))
        return out

    def select(self, ref: str) -> list[dict[str, Any]]:
        """Resolve a reference to one or more runs.

        Accepted forms: a path to a record file; a run id or unique id prefix; ``latest``;
        a run name; or ``name:arm``.
        """
        if ref.endswith(".json") and os.path.isfile(ref):
            return [load_file(Path(ref))]
        runs = self.all()
        if ref == "latest":
            if not runs:
                raise RecordNotFound(f"no runs recorded yet in {self.runs}")
            return [runs[-1]]
        by_id = [r for r in runs if r["id"].startswith(ref)]
        if len(by_id) == 1:
            return by_id
        if ":" in ref:
            name, arm = ref.split(":", 1)
            hits = [r for r in runs if _decl(r, "name") == name and _decl(r, "arm") == arm]
        else:
            hits = [r for r in runs if _decl(r, "name") == ref]
        if hits:
            return hits
        if len(by_id) > 1:
            raise RecordNotFound(f"run id prefix {ref!r} is ambiguous ({len(by_id)} matches)")
        raise RecordNotFound(f"no run matches {ref!r} in {self.runs}")


def _decl(record: dict[str, Any], key: str) -> Any:
    return (record.get("declared") or {}).get(key)


_DICT_FIELDS = ("declared", "files", "env", "modules", "params", "metrics", "outcomes", "gates", "imports")


def load_file(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as f:
        record: Any = json.load(f)
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} record")
    if not isinstance(record.get("id"), str) or any(
        not isinstance(record.get(k), dict) for k in _DICT_FIELDS if record.get(k) is not None
    ):
        raise ValueError(f"{path}: malformed {SCHEMA} record")
    return record
