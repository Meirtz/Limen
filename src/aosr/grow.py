"""Growth: run the task stream through the kernel and let verified experience extend the image.

Tasks are processed in batches against a fixed image; admissions from a batch are applied in stream
order before the next batch starts, and each batch commits a new image whose parent is the previous
one. Model calls and sandbox results are cached, so a growth run replays exactly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any

from aosr.admit import Admitter
from aosr.domains import Task, World
from aosr.kernel import Episode, Kernel, Settings
from aosr.llm import Model
from aosr.sandbox import Sandbox
from aosr.store import Head, Image, Store


@dataclass
class GrowConfig:
    batch: int = 10
    settings: Settings | None = None
    checkpoints: tuple[int, ...] = ()  # stream positions at which to name an image (refs <run>/n<pos>)
    run: str = "grow"


def boot(store: Store, seed_slots: dict[str, str] | None = None) -> str:
    """Create head_0: no capabilities, no programs, the seed kernel slots."""
    head = Head(note="head_0: empty store")
    for slot, src in sorted((seed_slots or {}).items()):
        head.slots[slot] = store.put({"type": "slot", "slot": slot, "src": src, "origin": "seed"})
    digest = store.commit(head)
    store.set_ref("head_0", digest)
    return digest


def grow(
    store: Store,
    start: str,
    stream: list[Task],
    world: World,
    model: Model,
    sandbox: Sandbox,
    out_dir: Path,
    cfg: GrowConfig,
    progress: Callable[[str], None] = print,
) -> str:
    """Grow from image ``start`` over ``stream``; return the final image digest."""
    kernel = Kernel(world, sandbox)
    admitter = Admitter(store, sandbox)
    settings = cfg.settings or Settings()
    head = store.head(start)
    head.parent, digest = start, start
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "grow.jsonl"
    done = _done(log_path)
    if done:  # resume: replay the logged positions from the caches
        progress(f"resuming after {len(done)} logged tasks (calls replay from the cache)")
    log_path.write_text("")
    solved = 0
    for i in range(0, len(stream), cfg.batch):
        batch = stream[i : i + cfg.batch]
        image = Image(store, head.copy())
        meta = {"run": cfg.run, "phase": "grow"}
        with ThreadPoolExecutor(len(batch)) as ex:
            episodes = list(
                ex.map(partial(kernel.solve, image=image, model=model, settings=settings, meta=meta), batch)
            )
        for j, (task, ep) in enumerate(zip(batch, episodes, strict=True)):
            pos = i + j
            att = ep.verified
            correct = bool(att and att.test_outputs and all(world.judge(task, att.test_outputs)))
            rec: dict[str, Any] = {"position": pos, "task": task.id, "verified": att is not None, "correct": correct,
                                   "calls": ep.calls, "presolved": bool(att and att.kind == "presolve")}  # fmt: skip
            if att is not None and correct:
                solved += 1
                adm = admitter.admit(head, task, att, pos, {"run": cfg.run, "call": att.call_key or "presolve"})
                rec["admission"] = asdict(adm)
                store.log({"kind": "admit", "run": cfg.run, **rec})
            rec["episode"] = ep.to_json()
            with log_path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        head.seq += 1
        head.note = f"{cfg.run}: after {i + len(batch)} stream tasks"
        digest = store.commit(head)
        head.parent = digest
        end = i + len(batch)
        store.set_ref(f"{cfg.run}/latest", digest)
        for c in cfg.checkpoints:
            if i < c <= end:
                store.set_ref(f"{cfg.run}/n{c}", digest)
        s = Image(store, head).summary()
        progress(f"grow {end}/{len(stream)}: solved {solved}, capabilities {s['capabilities']}, "
                 f"programs {s['programs']}, depth {s['max_depth']}")  # fmt: skip
    store.set_ref(f"{cfg.run}/n{len(stream)}", digest)
    return digest


def _done(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def episodes_of(path: Path) -> list[Episode]:
    """Load logged episodes (records are dropped in the log)."""
    from aosr.kernel import Attempt

    out = []
    for rec in _done(path):
        e = rec["episode"]
        atts = [Attempt(**{**a, "edges": [tuple(x) for x in a["edges"]]}) for a in e["attempts"]]
        out.append(Episode(e["task"], atts, e.get("retrieved", []), e.get("precedents", [])))
    return out
