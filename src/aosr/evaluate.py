"""Evaluation: run a frozen image on held-out tasks and score every budget prefix.

Attempts follow a fixed schedule, so the outcome of a run with budget B also gives the outcome at
every smaller budget b: a task counts as solved at b when the first program that reproduces all
examples appears within the first b model calls and is correct on the held-back test inputs.
Presolve attempts cost no call.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aosr import stats
from aosr.domains import Task, World
from aosr.kernel import Episode, Kernel, Settings
from aosr.llm import Model
from aosr.sandbox import Sandbox
from aosr.store import Image, Store


@dataclass
class Arm:
    name: str
    image: str  # digest
    model: Model
    settings: Settings


def score_episode(world: World, task: Task, ep: Episode, budget: int) -> dict[str, Any]:
    calls = 0
    first: dict[str, Any] | None = None
    for a in ep.attempts:
        if a.call_key is not None:
            calls += 1
        if a.verified:
            correct = bool(a.test_outputs) and all(world.judge(task, a.test_outputs))
            first = {"at": calls, "correct": correct, "kind": a.kind, "used": sorted(a.used), "depth": a.depth}
            break
    solved_at = [bool(first and first["correct"] and first["at"] <= b) for b in range(budget + 1)]
    return {
        "task": task.id,
        "solved_at": solved_at,
        "calls": ep.calls,
        "in_tokens": sum(a.in_tokens for a in ep.attempts),
        "out_tokens": sum(a.out_tokens for a in ep.attempts),
        "usd": round(sum(a.usd for a in ep.attempts), 6),
        "errors": sum(1 for a in ep.attempts if a.status in ("llm_timeout", "infra_error")),
        "first_verified": first,
        "retrieved": ep.retrieved,
        "precedents": ep.precedents,
    }


def run_arm(
    store: Store, arm: Arm, tasks: list[Task], world: World, sandbox: Sandbox, out: Path, parallel: int = 10
) -> list[dict[str, Any]]:
    kernel = Kernel(world, sandbox)
    image = Image(store, store.head(arm.image))
    meta = {"arm": arm.name, "phase": "eval"}
    with ThreadPoolExecutor(parallel) as ex:
        eps = list(ex.map(lambda t: kernel.solve(t, image, arm.model, arm.settings, meta), tasks))
    rows = [score_episode(world, t, e, arm.settings.budget) for t, e in zip(tasks, eps, strict=True)]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write(json.dumps({"arm": arm.name, "image": arm.image, "model": arm.model.name,
                            "budget": arm.settings.budget, "context": arm.settings.context}) + "\n")  # fmt: skip
        for r, e in zip(rows, eps, strict=True):
            f.write(json.dumps({**r, "episode": e.to_json()}, default=str) + "\n")
    return rows


def load_arm(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = path.read_text().splitlines()
    return json.loads(lines[0]), [json.loads(x) for x in lines[1:] if x.strip()]


def summarize(header: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    budget = header["budget"]
    solved = sum(r["solved_at"][budget] for r in rows)
    calls = sum(r["calls"] for r in rows)
    usd = sum(r["usd"] for r in rows)
    reuse = sum(1 for r in rows if r["solved_at"][budget] and r["first_verified"] and r["first_verified"]["used"])
    presolved = sum(1 for r in rows if r["solved_at"][budget] and r["first_verified"]["kind"] == "presolve")
    return {
        "arm": header["arm"],
        "n": n,
        "budget": budget,
        "solved": solved,
        "rate": solved / n if n else 0.0,
        "by_budget": [sum(r["solved_at"][b] for r in rows) for b in range(budget + 1)],
        "calls": calls,
        "usd": round(usd, 4),
        "usd_per_solve": round(usd / solved, 4) if solved else None,
        "calls_per_solve": round(calls / solved, 2) if solved else None,
        "solved_with_library": reuse,
        "presolved": presolved,
        "errors": sum(r["errors"] for r in rows),
    }


def compare(
    a: tuple[dict[str, Any], list[dict[str, Any]]],
    b: tuple[dict[str, Any], list[dict[str, Any]]],
    budget_a: int,
    budget_b: int,
) -> stats.Interval:
    """Paired difference (b minus a) in solve rate, arm a at budget_a and arm b at budget_b."""
    ra = {r["task"]: r for r in a[1]}
    rb = {r["task"]: r for r in b[1]}
    items = sorted(set(ra) & set(rb))
    if len(items) != len(ra) or len(items) != len(rb):
        raise ValueError("arms were evaluated on different items")
    xa = [float(ra[t]["solved_at"][budget_a]) for t in items]
    xb = [float(rb[t]["solved_at"][budget_b]) for t in items]
    return stats.paired_diff(xa, xb)
