"""The kernel: how one task is solved on top of an image.

1. Probe: run every active one-argument capability, and every stored program, on the task's example
   inputs, and score how close each comes to the example outputs.
2. Presolve: if a capability or program already reproduces every example, verify it and stop. This
   costs no model call; it is how grown capability replaces reasoning.
3. Otherwise ask the worker model, alternating fresh attempts and repairs, until a program reproduces
   every example or the call budget is spent. The prompt shows a compact index of the library, the
   sources of the closest capabilities, and the closest earlier programs.

Every attempt is recorded with its cost, its outcome on the examples, its outputs on the test inputs,
and the dynamic call graph of library functions it executed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from aosr import config
from aosr.domains import Task, World
from aosr.llm import Model
from aosr.sandbox import Sandbox
from aosr.store import Image

LIBRARY_HEADER = (
    "LIBRARY (already defined in your namespace and verified; call these directly, never redefine or import them):"
)
RETRIEVED_HEADER = "CLOSEST LIBRARY FUNCTIONS on these examples (full source):"
PRECEDENT_HEADER = "CLOSEST PROGRAMS from earlier solved tasks, run on these examples (reuse what fits):"
PREV_PROGRAM = "Your previous program:"


def system_prompt(world: World) -> str:
    imports = ", ".join(config.ALLOWED_IMPORTS)
    return (
        f"You are an expert Python programmer. {world.rules}\n"
        "Reply with exactly one ```python block that defines solve(...).\n"
        f"Only these imports are allowed: {imports}. No numpy, no file or network access.\n"
        "Put reusable logic in small, general helper functions defined at module top level (never nested inside "
        "solve), each with a one-line docstring; useful helpers are kept in a shared library for future tasks. "
        "solve should mostly compose helpers. When a library function fits, call it."
    )


@dataclass
class Settings:
    budget: int = 2  # model calls per task
    context: str = "full"  # full | none
    probe_cap: int = 300  # most-used capabilities probed per task
    retrieve_k: int = 3
    retrieve_min: float = 0.3
    index_lines: int = 40
    precedent_k: int = 2
    precedent_min: float = 0.5
    sample_offset: int = 0  # separates independent samples of the same prompt across arms


@dataclass
class Attempt:
    index: int
    kind: str  # presolve | synth | repair
    source: str
    status: str  # ok | no_code | rejected | crash | timeout | llm_timeout | infra_error
    train_ok: list[bool]
    test_outputs: list[Any]
    used: dict[str, int] = field(default_factory=dict)
    depth: int = 0
    edges: list[tuple[str, str]] = field(default_factory=list)
    records: dict[str, list[Any]] = field(default_factory=dict)
    call_key: str | None = None
    in_tokens: int = 0
    out_tokens: int = 0
    usd: float = 0.0
    latency_s: float = 0.0

    @property
    def verified(self) -> bool:
        return bool(self.train_ok) and all(self.train_ok)


@dataclass
class Episode:
    task: str
    attempts: list[Attempt]
    retrieved: list[str] = field(default_factory=list)
    precedents: list[str] = field(default_factory=list)

    @property
    def verified(self) -> Attempt | None:
        return next((a for a in self.attempts if a.verified), None)

    @property
    def calls(self) -> int:
        return sum(1 for a in self.attempts if a.call_key is not None)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for a in d["attempts"]:
            a["records"] = {}  # bulky; the admission step reads them from the live episode
        return d


def code_of(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if blocks:
        return str(blocks[-1])
    return text if "def solve" in text else None


def top_level_defs(src: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name != "solve"]


class Kernel:
    def __init__(self, world: World, sandbox: Sandbox) -> None:
        self.world, self.sandbox = world, sandbox

    # ---------------------------------------------------------------- probing

    def score(self, outputs: list[Any], task: Task) -> float:
        wanted = task.train_outputs
        if len(outputs) != len(wanted):
            return 0.0
        return sum(self.world.similarity(o, w) for o, w in zip(outputs, wanted, strict=True)) / len(wanted)

    def probe(self, image: Image, task: Task, library: str, settings: Settings) -> list[tuple[str, float]]:
        names = [n for n in image.names() if image.cap(n)["arity"] == 1]
        names.sort(key=lambda n: (-image.head.caps[n].uses, image.head.caps[n].admitted))
        outs = self.sandbox.apply(library, names[: settings.probe_cap], task.train_inputs)
        scores = [(n, self.score(o, task)) for n, o in outs.items()]
        return sorted(scores, key=lambda s: (-s[1], -image.head.caps[s[0]].uses, image.head.caps[s[0]].admitted))

    def precedents(self, image: Image, task: Task, library: str) -> list[tuple[str, float]]:
        progs = [(t, src) for t, src in image.programs() if t != task.id]
        outs = self.sandbox.programs(library, progs, task.train_inputs)
        return sorted(((t, self.score(o, task)) for t, o in outs.items()), key=lambda s: (-s[1], s[0]))

    # ---------------------------------------------------------------- prompting

    def context(
        self, image: Image, probes: list[tuple[str, float]], precs: list[tuple[str, float]], settings: Settings
    ) -> tuple[str, list[str], list[str]]:
        if settings.context == "none" or not (image.names() or image.head.programs):
            return "", [], []
        parts: list[str] = []
        rank = {n: i for i, (n, _) in enumerate(probes)}
        listed = sorted(image.names(), key=lambda n: (rank.get(n, 10**6), -image.head.caps[n].uses))
        listed = listed[: settings.index_lines]
        if listed:
            lines = [f"- {image.cap(n)['sig']}: {image.cap(n)['doc']}" for n in listed]
            parts.append(LIBRARY_HEADER + "\n" + "\n".join(lines))
        retrieved = [n for n, s in probes[: settings.retrieve_k] if s >= settings.retrieve_min]
        if retrieved:
            blocks = [
                f"# {n}: {s:.0%} of example cells right\n{image.cap(n)['src']}" for n, s in probes if n in retrieved
            ]
            parts.append(RETRIEVED_HEADER + "\n\n" + "\n\n".join(blocks))
        close = [(t, s) for t, s in precs[: settings.precedent_k] if s >= settings.precedent_min]
        if close:
            src = dict(image.programs())
            blocks = [f"# program for task {t}: {s:.0%} of example cells right\n{src[t].strip()}" for t, s in close]
            parts.append(PRECEDENT_HEADER + "\n\n" + "\n\n".join(blocks))
        return "\n\n".join(parts), retrieved, [t for t, _ in close]

    def user_prompt(self, task: Task, context: str, prev: Attempt | None, feedback: str) -> str:
        r = self.world.render
        parts = [
            "\n\n".join(
                f"Example {k + 1} input:\n{r(x)}\nExample {k + 1} output:\n{r(y)}"
                for k, (x, y) in enumerate(task.train)
            )
        ]
        if context:
            parts.append(context)
        if prev is not None:
            parts.append(f"{PREV_PROGRAM}\n```python\n{prev.source}\n```\nIts result on the examples:\n{feedback}")
            parts.append("Fix the program.")
        else:
            parts.append("Write solve(grid) for this task.")
        return "\n\n".join(parts)

    def feedback(self, task: Task, attempt: Attempt, outputs: list[Any], errors: list[str | None]) -> str:
        lines = []
        for k, ((_, want), out, err) in enumerate(zip(task.train, outputs, errors, strict=False)):
            if attempt.train_ok[k]:
                lines.append(f"Example {k + 1}: correct.")
            elif err:
                lines.append(f"Example {k + 1}: error: {err}")
            else:
                diff = getattr(self.world, "diff", None)
                lines.append(f"Example {k + 1}: " + (diff(out, want) if diff else "wrong output."))
        return "\n".join(lines)

    # ---------------------------------------------------------------- running

    def execute(
        self, image: Image, library: str, task: Task, source: str, kind: str, index: int
    ) -> tuple[Attempt, list[Any], list[str | None]]:
        n = len(task.train)
        defs = top_level_defs(source)
        res = self.sandbox.run(
            library, source, task.train_inputs + list(task.test_inputs), trace=image.names(None), record=defs
        )
        outs, errs = res.outputs[:n], res.errors[:n]
        train_ok = [e is None and o == w for o, e, w in zip(outs, errs, task.train_outputs, strict=True)]
        a = Attempt(
            index, kind, source, res.status, train_ok, res.outputs[n:], used=res.trace.calls,
            depth=res.trace.max_depth, edges=res.trace.edges, records=res.trace.records,
        )  # fmt: skip
        return a, outs, errs

    def solve(
        self, task: Task, image: Image, model: Model, settings: Settings, meta: dict[str, str] | None = None
    ) -> Episode:
        library = image.library_source()
        probes = self.probe(image, task, library, settings) if image.names() else []
        precs = self.precedents(image, task, library) if image.head.programs else []
        attempts: list[Attempt] = []
        candidates = [f"def solve(grid):\n    return {n}(grid)\n" for n, s in probes if s == 1.0][:20]
        prog_src = dict(image.programs())
        candidates += [prog_src[t] for t, s in precs if s == 1.0][:5]
        for cand in candidates:  # presolve: zero model calls
            a, _, _ = self.execute(image, library, task, cand, "presolve", len(attempts))
            attempts.append(a)
            if a.verified:
                return Episode(task.id, attempts)
        context, retrieved, used_precs = self.context(image, probes, precs, settings)
        system = system_prompt(self.world)
        prev: Attempt | None = None
        feedback = ""
        for i in range(settings.budget):
            repair = i % 2 == 1 and prev is not None
            user = self.user_prompt(task, context, prev if repair else None, feedback)
            c = model.complete(
                system, user, sample=settings.sample_offset + i, role="worker",
                meta={**(meta or {}), "task": task.id, "attempt": str(i)},
            )  # fmt: skip
            kind = "repair" if repair else "synth"
            src = code_of(c.text) if c.ok else None
            if src is None:
                status = "no_code" if c.ok else ("llm_timeout" if c.status == "timeout" else "infra_error")
                a = Attempt(len(attempts), kind, c.text[:2000], status, [], [])
                outs: list[Any] = []
                errs: list[str | None] = []
            else:
                a, outs, errs = self.execute(image, library, task, src, kind, len(attempts))
            a.call_key, a.out_tokens, a.usd, a.latency_s = c.key, c.usage.output_tokens, c.usd(), c.latency_s
            a.in_tokens = c.usage.input_tokens + c.usage.cache_read + c.usage.cache_write
            attempts.append(a)
            if src is None:
                continue
            if a.verified:
                break
            prev, feedback = a, self.feedback(task, a, outs, errs)
        return Episode(task.id, attempts, retrieved, used_precs)
