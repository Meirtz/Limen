"""A tiny grid world and an oracle model for offline AOSR tests."""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Callable
from pathlib import Path

Grid = list[list[int]]

OPS: dict[str, Callable[[Grid], Grid]] = {
    "flip_rows": lambda g: [list(reversed(r)) for r in g],
    "flip_cols": lambda g: [list(r) for r in reversed(g)],
    "transpose": lambda g: [list(r) for r in zip(*g, strict=True)],
    "recolor": lambda g: [[2 if c == 1 else c for c in r] for r in g],
}
DOCS = {
    "flip_rows": ("Mirror each row left to right.", "[list(reversed(r)) for r in grid]"),
    "flip_cols": ("Reverse the order of the rows.", "[list(r) for r in reversed(grid)]"),
    "transpose": ("Swap rows and columns.", "[list(r) for r in zip(*grid)]"),
    "recolor": ("Paint every 1 as 2.", "[[2 if c == 1 else c for c in r] for r in grid]"),
}
SOURCES = {n: f'def {n}(grid):\n    """{doc}"""\n    return {body}' for n, (doc, body) in DOCS.items()}


def _grid(rng: random.Random) -> Grid:
    h, w = rng.randint(2, 4), rng.randint(2, 4)
    return [[rng.randint(0, 3) for _ in range(w)] for _ in range(h)]


def make_world(root: Path, tasks: dict[str, list[str]], split: str = "training", seed: int = 0) -> None:
    """Write one ARC-format task per entry: ``id -> [op, ...]`` applied left to right."""
    rng = random.Random(seed)
    d = root / split
    d.mkdir(parents=True, exist_ok=True)
    for tid, ops in tasks.items():
        pairs = []
        for _ in range(4):
            g = _grid(rng)
            out = g
            for op in ops:
                out = OPS[op](out)
            pairs.append({"input": g, "output": out})
        (d / f"{tid}.json").write_text(json.dumps({"train": pairs[:3], "test": pairs[3:]}))


def _parse(user: str) -> list[tuple[Grid, Grid]]:
    blocks = re.findall(r"Example \d+ input:\n([\d\n]+?)\nExample \d+ output:\n([\d\n]+?)(?:\n\n|\n?$)", user)
    to = lambda s: [[int(c) for c in row] for row in s.strip().split("\n")]  # noqa: E731
    return [(to(a), to(b)) for a, b in blocks]


def solve_ops(pairs: list[tuple[Grid, Grid]]) -> list[str] | None:
    names = list(OPS)
    for depth in (1, 2):
        cands = [[a] for a in names] if depth == 1 else [[a, b] for a in names for b in names]
        for ops in cands:
            ok = True
            for x, y in pairs:
                out = x
                try:
                    for op in ops:
                        out = OPS[op](out)
                except Exception:
                    ok = False
                    break
                if out != y:
                    ok = False
                    break
            if ok:
                return ops
    return None


def oracle(buggy_first: bool = False) -> Callable[[str, str, int, str], str]:
    """Writes helpers for unknown ops and calls library functions it is shown."""

    def fn(system: str, user: str, sample: int, role: str) -> str:
        ops = solve_ops(_parse(user))
        if ops is None:
            return "```python\ndef solve(grid):\n    return grid\n```"
        listed = user.split("LIBRARY", 1)[1].split("\n\n", 1)[0] if "LIBRARY" in user else ""
        defs = [SOURCES[op] for op in dict.fromkeys(ops) if f"{op}(" not in listed]
        expr = "grid"
        for op in ops:
            expr = f"{op}({expr})"
        repair = "Your previous program" in user
        h = int(hashlib.sha256(user.encode()).hexdigest(), 16)
        if buggy_first and not repair and h % 2 == 0:
            expr = "grid"
        body = "\n\n".join([*defs, f"def solve(grid):\n    return {expr}"])
        return f"```python\n{body}\n```"

    return fn
