"""Growth: run the task stream through an engine and let verified experience extend the image.

Tasks are processed in batches against a fixed image. What the batch's solved tasks contribute is
prepared against that image, then applied in stream order; each batch commits a new image whose
parent is the previous one. Model calls and sandbox results are cached, so a growth run replays
exactly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aosr.domains import Task
from aosr.engine import Engine
from aosr.evolve import epoch
from aosr.kernel import Episode, Settings
from aosr.llm import Model
from aosr.store import Head, Image, Store


@dataclass
class GrowConfig:
    batch: int = 10
    settings: Settings | None = None
    checkpoints: tuple[int, ...] = ()  # stream positions at which to name an image (refs <run>/n<pos>)
    run: str = "grow"
    epoch_every: int = 0  # kernel evolution every N stream tasks (0 = never)
    engineer: Model | None = None
    window: int = 20
    margin: int = 2


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
    engine: Engine,
    model: Model,
    out_dir: Path,
    cfg: GrowConfig,
    progress: Callable[[str], None] = print,
) -> str:
    """Grow from image ``start`` over ``stream``; return the final image digest."""
    settings = cfg.settings or Settings()
    head = store.head(start)
    head.parent, digest = start, start
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "grow.jsonl"
    log_path.write_text("")
    solved = 0
    meta = {"run": cfg.run, "phase": "grow"}
    seen: list[tuple[Task, Episode]] = []
    for i in range(0, len(stream), cfg.batch):
        batch = stream[i : i + cfg.batch]
        image = Image(store, head.copy())
        episodes = engine.run(batch, image, model, settings, meta)
        verdicts = [engine.judge(t, e) for t, e in zip(batch, episodes, strict=True)]
        good = [(t, e, i + j) for j, (t, e, ok) in enumerate(zip(batch, episodes, verdicts, strict=True)) if ok]
        prepared = engine.contribute(image, good, model, meta)  # one per solved task, in order
        contributions = {t.id: c for (t, _, _), c in zip(good, prepared, strict=True)}
        for j, (task, ep, ok) in enumerate(zip(batch, episodes, verdicts, strict=True)):
            pos = i + j
            rec: dict[str, Any] = {
                "position": pos,
                "task": task.id,
                "correct": ok,
                "calls": ep.calls,
                "presolved": any(a.kind == "presolve" and a.verified for a in ep.attempts),
            }
            if ok:
                solved += 1
                rec["admission"] = engine.apply(head, contributions[task.id], {"run": cfg.run})
                store.log(
                    {"kind": "admit", "run": cfg.run, "position": pos, "task": task.id, "admission": rec["admission"]}
                )
            rec["episode"] = ep.to_json()
            seen.append((task, ep))
            with log_path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        end = i + len(batch)
        if cfg.epoch_every and cfg.engineer is not None and end // cfg.epoch_every > i // cfg.epoch_every:
            recent = seen[-cfg.epoch_every :]
            failed = [(t, e) for t, e in recent if not engine.judge(t, e)]
            passed = [(t, e) for t, e in recent if engine.judge(t, e)]
            window = [t for t, _ in recent[-cfg.window :]]
            result = epoch(
                store,
                head,
                engine,
                model,
                cfg.engineer,
                window,
                failed[:6] + passed[:2],
                settings,
                {"run": cfg.run, "epoch": str(end)},
            )
            store.log({"kind": "epoch", "run": cfg.run, "position": end, **result.to_json()})
            with log_path.open("a") as f:
                f.write(json.dumps({"epoch": end, **result.to_json()}) + "\n")
            progress(
                f"epoch at {end}: notes {'ACCEPTED' if result.accepted else 'rejected'} "
                f"({result.old} -> {result.new} of {len(window)} solved)"
            )
        head.seq += 1
        head.note = f"{cfg.run}: after {end} stream tasks"
        digest = store.commit(head)
        head.parent = digest
        store.set_ref(f"{cfg.run}/latest", digest)
        for c in cfg.checkpoints:
            if i < c <= end:
                store.set_ref(f"{cfg.run}/n{c}", digest)
        s = Image(store, head).summary()
        progress(
            f"grow {end}/{len(stream)}: solved {solved}, capabilities {s['capabilities']}, "
            f"programs {s['programs']}, depth {s['max_depth']}"
        )
    store.set_ref(f"{cfg.run}/n{len(stream)}", digest)
    return digest


def episodes_of(path: Path) -> list[Episode]:
    """Load logged episodes (bulky call records are not logged)."""
    from aosr.kernel import Attempt

    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "episode" not in rec:
            continue
        e = rec["episode"]
        atts = [Attempt(**{**a, "edges": [tuple(x) for x in a["edges"]]}) for a in e["attempts"]]
        out.append(Episode(e["task"], atts, e.get("retrieved", []), e.get("precedents", [])))
    return out
