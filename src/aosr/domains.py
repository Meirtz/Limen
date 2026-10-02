"""Task worlds. A world supplies tasks given as input/output examples, renders values for prompts,
scores partial agreement, and judges held-back test answers in the parent process only.

ARC-AGI-1 is the first world: a task is a few grid pairs, the solver is ``solve(grid) -> grid``.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aosr import config


@dataclass(frozen=True)
class Task:
    id: str
    split: str
    train: tuple[tuple[Any, Any], ...]
    test_inputs: tuple[Any, ...]

    @property
    def train_inputs(self) -> list[Any]:
        return [x for x, _ in self.train]

    @property
    def train_outputs(self) -> list[Any]:
        return [y for _, y in self.train]


class World(Protocol):
    name: str
    rules: str  # domain rules for the system prompt

    def load(self, ids: list[str], split: str) -> list[Task]: ...

    def render(self, value: Any) -> str: ...

    def similarity(self, output: Any, wanted: Any) -> float: ...

    def judge(self, task: Task, outputs: list[Any]) -> list[bool]: ...


def is_grid(g: Any) -> bool:
    return (
        isinstance(g, list)
        and 0 < len(g) <= 30
        and all(isinstance(r, list) and 0 < len(r) <= 30 and len(r) == len(g[0]) for r in g)
        and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 9 for r in g for c in r)
    )


class Arc:
    """ARC-AGI-1 from a checkout of github.com/fchollet/ARC-AGI under ``<data>/ARC-AGI``."""

    name = "arc"
    rules = (
        "Grids are lists of lists of ints 0-9 (rows of cells). Write solve(grid) that maps an input grid to its "
        "output grid."
    )
    LOCKED = "evaluation"  # the held-out split; loading its tasks needs AOSR_UNLOCK_EVAL=1

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or config.data_dir() / "ARC-AGI" / "data"

    def _path(self, split: str, task_id: str) -> Path:
        return self.root / split / f"{task_id}.json"

    def ids(self, split: str) -> list[str]:
        return sorted(p.stem for p in (self.root / split).glob("*.json"))

    def load(self, ids: list[str], split: str) -> list[Task]:
        if split == self.LOCKED and os.environ.get("AOSR_UNLOCK_EVAL") != "1":
            raise PermissionError("the evaluation split is held out; set AOSR_UNLOCK_EVAL=1 to read it")
        tasks = []
        for tid in ids:
            d = json.loads(self._path(split, tid).read_text())
            train = tuple((p["input"], p["output"]) for p in d["train"])
            tasks.append(Task(tid, split, train, tuple(p["input"] for p in d["test"])))
        return tasks

    def judge(self, task: Task, outputs: list[Any]) -> list[bool]:
        d = json.loads(self._path(task.split, task.id).read_text())
        wanted = [p["output"] for p in d["test"]]
        return [o == w for o, w in zip(outputs, wanted, strict=False)] + [False] * (len(wanted) - len(outputs))

    def render(self, value: Any) -> str:
        if is_grid(value):
            return "\n".join("".join(str(c) for c in row) for row in value)
        return repr(value)[:400]

    def similarity(self, output: Any, wanted: Any) -> float:
        if not is_grid(output) or len(output) != len(wanted) or len(output[0]) != len(wanted[0]):
            return 0.0
        cells = sum(len(r) for r in wanted)
        same: int = sum(a == b for ro, rw in zip(output, wanted, strict=True) for a, b in zip(ro, rw, strict=True))
        return same / cells

    def diff(self, output: Any, wanted: Any) -> str:
        """Feedback for one example: shape mismatch, or the wrong cells marked against the expected grid."""
        if not is_grid(output):
            return f"not a grid: {self.render(output)[:200]}"
        if len(output) != len(wanted) or len(output[0]) != len(wanted[0]):
            return f"wrong shape {len(output)}x{len(output[0])}, expected {len(wanted)}x{len(wanted[0])}"
        wrong = sum(a != b for ro, rw in zip(output, wanted, strict=True) for a, b in zip(ro, rw, strict=True))
        marks = "\n".join(
            "".join("." if a == b else str(b) for a, b in zip(ro, rw, strict=True))
            for ro, rw in zip(output, wanted, strict=True)
        )
        return f"{wrong} wrong cells. Expected digits where you were wrong ('.' = correct):\n{marks}"


def splits(world: Arc, seed: int = 0) -> dict[str, list[str]]:
    """Stream and dev splits over ARC training tasks: a fixed shuffle; held-out = the evaluation split."""
    order = world.ids("training")
    random.Random(seed).shuffle(order)
    return {"stream": order[:300], "dev": order[300:], "order": order}
