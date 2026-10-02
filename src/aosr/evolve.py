"""Kernel evolution: the OS rewrites its own operating notes from experience, under an A/B gate.

The notes are a slot of the image: text the kernel adds to the worker's system prompt. Every epoch an
engineer model reads what recently failed and succeeded and proposes new notes. The proposal is
accepted only if, on the same recent stream tasks with fresh samples, the worker solves at least
``margin`` more of them with the new notes than with the current ones. Each decision is recorded with
its evidence, and an accepted version becomes part of the next image.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from aosr.domains import Task
from aosr.engine import Engine
from aosr.kernel import POLICY, Episode, Settings
from aosr.llm import Model
from aosr.store import Head, Image, Store

ENGINEER = (
    "You maintain the operating notes of an agent system. The notes are added to the worker model's "
    "instructions for every task in this world. Write notes that would have prevented the recent failures "
    "without breaking the successes: concrete, general procedures and checks (never task-specific answers, ids "
    "or names). Keep what still helps from the current notes. At most 250 words, plain text, as a numbered "
    "list.\nReply with the complete new notes between <notes> and </notes>. You may also retune the kernel policy "
    "(how much of the system's library and earlier programs the worker is shown) by giving a JSON object between "
    "<policy> and </policy>; omit it to keep the current policy."
)


@dataclass
class EpochResult:
    accepted: bool
    old: int
    new: int
    window: list[str]
    notes: str
    cost_usd: float
    policy: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "old": self.old,
            "new": self.new,
            "window": self.window,
            "notes": self.notes,
            "policy": self.policy,
            "cost_usd": round(self.cost_usd, 4),
        }


def notes_of(image: Image) -> str:
    return image.slot_source("notes") or ""


def propose(
    engineer: Model, engine: Engine, current: str, policy: str, digests: list[str], meta: dict[str, str]
) -> tuple[str, str, float]:
    """Ask the engineer for new notes and, optionally, a new kernel policy (JSON)."""
    user = (
        f"World: {engine.name}\n\nCURRENT NOTES:\n{current or '(none yet)'}\n\n"
        f"KERNEL POLICY (current values: {policy or 'defaults'}):\n{engine.policy_doc}\n\n"
        "RECENT EPISODES:\n\n" + "\n\n---\n\n".join(digests)
    )
    c = engineer.complete(ENGINEER, user, sample=0, role="engineer", meta=meta)
    notes = re.search(r"<notes>(.*?)</notes>", c.text, re.S) if c.ok else None
    pol = re.search(r"<policy>(.*?)</policy>", c.text, re.S) if c.ok else None
    new_policy = ""
    if pol:
        try:
            d = json.loads(pol.group(1))
            if isinstance(d, dict):
                new_policy = json.dumps({k: v for k, v in sorted(d.items()) if k in POLICY})
        except json.JSONDecodeError:
            new_policy = ""
    return (notes.group(1).strip() if notes else current), (new_policy or policy), c.usd()


def blind(store: Store, head: Head, window: list[Task]) -> Head:
    """The head without what the window's own tasks contributed, so the A/B cannot replay their solutions."""
    ids = {t.id for t in window}
    born = {n for n, e in head.caps.items() if store.get(e.digest).get("origin", {}).get("task") in ids}
    h = Image(store, head).knockout(born) if born else head.copy()
    h.programs = {t: d for t, d in h.programs.items() if t not in ids}
    h.slots = dict(head.slots)
    return h


def solved(engine: Engine, tasks: list[Task], eps: list[Episode]) -> int:
    return sum(engine.judge(t, e) for t, e in zip(tasks, eps, strict=True))


def epoch(
    store: Store,
    head: Head,
    engine: Engine,
    worker: Model,
    engineer: Model,
    window: list[Task],
    recent: list[tuple[Task, Episode]],
    settings: Settings,
    meta: dict[str, str],
    margin: int = 3,
) -> EpochResult:
    """Propose new notes from ``recent`` and gate them on ``window``; mutate ``head`` if accepted."""
    image = Image(store, head)
    current, policy = notes_of(image), image.slot_source("policy") or ""
    digests = [engine.digest(t, e) for t, e in recent]
    proposal, new_policy, cost = propose(engineer, engine, current, policy, digests, {**meta, "phase": "evolve"})
    if (proposal, new_policy) == (current, policy) or not proposal:
        return EpochResult(False, 0, 0, [t.id for t in window], proposal, cost, new_policy)
    candidate = head.copy()
    candidate.slots["notes"] = store.put({"type": "slot", "slot": "notes", "src": proposal, "origin": meta})
    if new_policy:
        candidate.slots["policy"] = store.put({"type": "slot", "slot": "policy", "src": new_policy, "origin": meta})
    ab = replace(settings, sample_offset=settings.sample_offset + 7000 + head.seq * 100)
    old_eps = engine.run(window, Image(store, blind(store, head, window)), worker, ab, {**meta, "phase": "evolve-old"})
    new_eps = engine.run(
        window, Image(store, blind(store, candidate, window)), worker, ab, {**meta, "phase": "evolve-new"}
    )
    old, new = solved(engine, window, old_eps), solved(engine, window, new_eps)
    cost += sum(a.usd for e in old_eps + new_eps for a in e.attempts)
    accepted = new >= old + margin
    if accepted:
        head.slots = dict(candidate.slots)
    return EpochResult(accepted, old, new, [t.id for t in window], proposal, cost, new_policy)
