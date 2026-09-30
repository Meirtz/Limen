"""Item-level leakage: do training or tuning files contain held-out items?"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def read_ids(path: str | Path, key: str | None) -> set[str]:
    """Item ids from ``.jsonl``, ``.json``, ``.csv`` or plain-text (one id per line) files."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".jsonl":
        ids = set()
        with p.open(encoding="utf-8-sig") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    ids.add(_get(json.loads(line), key, f"{p}:{n}"))
        return ids
    if suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and key is None:
            return {str(k) for k in data}
        rows: list[Any] | None
        if isinstance(data, list):
            rows = data
        else:
            lists = [v for v in data.values() if isinstance(v, list)]
            rows = data.get("items") or data.get("data") or (lists[0] if len(lists) == 1 else None)
        if rows is None:
            raise ValueError(f"{p}: expected a list of rows or an object holding one list")
        return {_get(row, key, str(p)) for row in rows}
    if suffix in (".csv", ".tsv"):
        if key is None:
            raise ValueError(f"{p}: --key is required for {suffix} files")
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter="\t" if suffix == ".tsv" else ",")
            if key not in (reader.fieldnames or []):
                raise ValueError(f"{p}: missing column {key!r}")
            return {_norm(row[key]) for row in reader}
    return {_norm(line) for line in p.read_text(encoding="utf-8-sig").splitlines() if line.strip()}


def _norm(value: Any) -> str:
    """Ids compare as text; ``7``, ``7.0`` and ``" 7 "`` are the same id."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    try:
        f = float(text)
        if f.is_integer() and ("." in text or "e" in text.lower()):
            return str(int(f))
    except ValueError:
        pass
    return text


def _get(row: Any, key: str | None, where: str) -> str:
    if key is None:
        if isinstance(row, (str, int, float)):
            return _norm(row)
        raise ValueError(f"{where}: rows are objects; pass --key")
    value: Any = row
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"{where}: missing key {key!r}")
        value = value[part]
    return _norm(value)


def overlap(holdout: set[str], data: set[str]) -> set[str]:
    return holdout & data
