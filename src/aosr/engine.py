"""Engines bind a world to the kernel: how a batch of tasks is solved, judged, and turned into growth.

Growth and evaluation are written against this interface, so the same store, images, statistics and
command line serve every world. ``ArcEngine`` solves input/output program-synthesis tasks in a local
sandbox; the AppWorld engine (``aosr.apps``) runs interactive episodes in app worlds.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from functools import partial
from typing import Any, Protocol

from aosr.admit import Admitter
from aosr.domains import Arc, Task
from aosr.kernel import Episode, Kernel, Settings
from aosr.llm import Model
from aosr.sandbox import Sandbox
from aosr.store import Head, Image, Store


class Engine(Protocol):
    name: str
    policy_doc: str  # what the kernel policy knobs mean in this world

    def load(self, split: str, n: int | None = None, offset: int = 0) -> list[Task]: ...

    def run(
        self, tasks: list[Task], image: Image, model: Model, settings: Settings, meta: dict[str, str]
    ) -> list[Episode]: ...

    def judge(self, task: Task, episode: Episode) -> bool:
        """Whether the episode solved the task, checked against held-back answers."""
        ...

    def calls_to_solution(self, episode: Episode) -> int | None:
        """Model calls spent before the solution that is judged, or None when nothing was submitted."""
        ...

    def contribute(
        self, image: Image, solved: list[tuple[Task, Episode, int]], model: Model, meta: dict[str, str]
    ) -> list[Any]:
        """Prepare what each solved stream task contributes, one item per task in order (may run in parallel)."""
        ...

    def apply(self, head: Head, contribution: Any, origin: dict[str, Any]) -> dict[str, Any]:
        """Apply one contribution to ``head`` in stream order; return a JSON-able record of what changed."""
        ...

    def digest(self, task: Task, episode: Episode) -> str:
        """A short account of an episode for the kernel engineer."""
        ...


class ArcEngine:
    """ARC-AGI-1: write ``solve(grid)``; verify on the example pairs; judge on the held-back test pairs."""

    name = "arc"
    policy_doc = (
        "index_lines (0-100): library functions listed by name and docstring (0 hides the list). "
        "retrieve_k (0-10) and retrieve_min (0-1): library functions whose outputs come closest on the examples, "
        "shown in full, and the share of example cells they must get right. precedent_k (0-5) and precedent_min "
        "(0-1): earlier solved programs shown in full, and the share of example cells they must get right here."
    )

    def __init__(self, store: Store, sandbox: Sandbox, world: Arc | None = None, parallel: int = 10) -> None:
        self.store, self.sandbox = store, sandbox
        self.world = world or Arc()
        self.kernel = Kernel(self.world, sandbox)
        self.admitter = Admitter(store, sandbox)
        self.parallel = parallel

    def load(self, split: str, n: int | None = None, offset: int = 0) -> list[Task]:
        from aosr.domains import splits

        if split in ("stream", "dev"):
            ids, source = splits(self.world)[split], "training"
        elif split == "evaluation":
            ids, source = self.world.ids("evaluation"), "evaluation"
        else:
            raise ValueError(f"unknown split {split}")
        ids = ids[offset:]
        return self.world.load(ids[:n] if n else ids, source)

    def run(
        self, tasks: list[Task], image: Image, model: Model, settings: Settings, meta: dict[str, str]
    ) -> list[Episode]:
        solve = partial(self.kernel.solve, image=image, model=model, settings=settings, meta=meta)
        with ThreadPoolExecutor(max(1, min(self.parallel, len(tasks)))) as ex:
            return list(ex.map(solve, tasks))

    def judge(self, task: Task, episode: Episode) -> bool:
        a = episode.verified
        return bool(a and a.test_outputs and all(self.world.judge(task, a.test_outputs)))

    def calls_to_solution(self, episode: Episode) -> int | None:
        calls = 0
        for a in episode.attempts:
            calls += a.call_key is not None
            if a.verified:
                return calls
        return None

    def contribute(
        self, image: Image, solved: list[tuple[Task, Episode, int]], model: Model, meta: dict[str, str]
    ) -> list[Any]:
        return solved  # admission needs the evolving head, so it happens in apply

    def apply(self, head: Head, contribution: Any, origin: dict[str, Any]) -> dict[str, Any]:
        task, episode, position = contribution
        attempt = episode.verified
        assert attempt is not None
        adm = self.admitter.admit(head, task, attempt, position, {**origin, "call": attempt.call_key or "presolve"})
        return asdict(adm)

    def digest(self, task: Task, episode: Episode) -> str:
        r = self.world.render
        ex = task.train[0]
        lines = [
            f"Task {task.id}: {'SOLVED' if self.judge(task, episode) else 'FAILED'} after {episode.calls} model calls.",
            f"First example ({len(task.train)} in total):\n{r(ex[0])}\n->\n{r(ex[1])}",
        ]
        last = next((a for a in reversed(episode.attempts) if a.source), None)
        if last is not None:
            code = "\n".join(last.source.splitlines()[:40])
            ok = sum(last.train_ok)
            lines.append(f"Last program ({last.status}; {ok}/{len(task.train)} examples right):\n{code}")
        return "\n".join(lines)
