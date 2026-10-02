"""AppWorld: an ecosystem of apps (spotify, venmo, gmail, phone, file_system, ...) behind a Python ``apis``
object. The worker completes a user's task turn by turn, writing code cells into a persistent shell.

The image's library is loaded into the shell before the first turn, and its functions are traced.
After a solved stream task, a consolidation call rewrites the episode's cells into general helpers
plus ``solve(apis)``. The rewrite is replayed in a fresh copy of the task's world: it is kept only if
it completes the task there, and each helper only if the task fails when that helper is stubbed out.

AppWorld is not thread-safe and needs Python 3.11, so episodes run in worker processes with the
``appworld`` package installed. Its task data must never be published unencrypted, so nothing from
a world is written into this repository.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import random
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from aosr import config
from aosr.domains import Task
from aosr.kernel import Attempt, Episode, Settings, with_policy
from aosr.llm import CallCache, Ledger, Model, make_model
from aosr.store import ACTIVE, CapEntry, Head, Image, Store

SYSTEM = """You are an agent that completes a user's task by writing Python code against the `apis` object of a phone's app ecosystem (spotify, amazon, venmo, gmail, phone, file_system, simple_note, splitwise, todoist, ...).
Each reply: one or two short sentences of reasoning, then exactly ONE ```python block. Only that block runs, in a persistent Python shell, and you then see what it printed. The shell does not echo values: print() everything you want to see.
How to work, one step at a time:
- Find the APIs you need: print(apis.api_docs.show_api_descriptions(app_name="venmo")), then print(apis.api_docs.show_api_doc(app_name="venmo", api_name="login")) for the exact parameters.
- Passwords: print(apis.supervisor.show_account_passwords()). Log in with apis.<app>.login(username=<email, or phone number for the phone app>, password=...) and pass the returned access_token to later calls.
- List APIs are paginated: call them with page_index=0, 1, 2, ... until a page comes back empty.
- Verify facts with the APIs; never guess values.
- When the task is done, call apis.supervisor.complete_task(answer=...) only if the task asks a question (a short value such as a number or a name); for any other task call apis.supervisor.complete_task() with no answer.
- Check every result before relying on it: an empty list or a zero where data is expected means you called the wrong API or missed a page.

Example, for a different task ("How many songs are in my Spotify song library?"):
Reply 1: I need the Spotify APIs.
```python
print(apis.api_docs.show_api_descriptions(app_name="spotify"))
```
Reply 2: Check how to log in and list the library.
```python
print(apis.api_docs.show_api_doc(app_name="spotify", api_name="login"))
print(apis.api_docs.show_api_doc(app_name="spotify", api_name="show_song_library"))
```
Reply 3: Log in with the supervisor's email and password, then read every page.
```python
passwords = {a["account_name"]: a["password"] for a in apis.supervisor.show_account_passwords()}
token = apis.spotify.login(username="joyce-weav@gmail.com", password=passwords["spotify"])["access_token"]
songs, page = [], 0
while True:
    batch = apis.spotify.show_song_library(access_token=token, page_index=page, page_limit=20)
    if not batch:
        break
    songs += batch
    page += 1
print(len(songs))
```
Reply 4: The library has that many songs.
```python
apis.supervisor.complete_task(answer=len(songs))
```"""

CONSOLIDATE = """Below is code that just completed an app task successfully (the cells ran in order in one shell).
Rewrite it as reusable library code:
- small, general helper functions that would help with OTHER users' similar tasks: each takes `apis` as its first parameter, has a one-line docstring that states exactly what it returns and which apis.<app>.<api> calls it makes, and hard-codes no user names, emails, ids, dates or answers (take them as parameters; look up credentials and the user's profile with apis.supervisor inside the helper when needed);
- then def solve(apis): that completes exactly this task by calling the helpers, ending with apis.supervisor.complete_task(...).
Library functions listed above already exist: call them instead of rewriting them.
Reply with exactly one ```python block containing the new helpers and solve."""

TRACER = """
import json as _aosr_json
_aosr_calls = {}
def _aosr_wrap(_n, _f):
    def _w(*a, **k):
        _aosr_calls[_n] = _aosr_calls.get(_n, 0) + 1
        return _f(*a, **k)
    _w.__doc__ = _f.__doc__
    return _w
for _n in %s:
    globals()[_n] = _aosr_wrap(_n, globals()[_n])
"""

WORDS = re.compile(r"[a-z]+")


def first_block(text: str) -> str | None:
    m = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return str(m[0]) if m else None


def words(text: str) -> set[str]:
    return set(WORDS.findall(text.lower().replace("_", " ")))


def context_text(
    index: list[dict[str, Any]],
    precedents: list[dict[str, Any]],
    instruction: str,
    policy: dict[str, Any] | None = None,
) -> str:
    """Library index ranked by word overlap with the task, the top sources, and the closest solved task."""
    policy = policy or {}
    lines_n, top_k, prec_k = policy.get("index_lines", 40), policy.get("retrieve_k", 3), policy.get("precedent_k", 1)
    parts = []
    w = words(instruction)
    if index and (lines_n or top_k):
        ranked = sorted(index, key=lambda c: (-len(w & words(c["name"] + " " + c["doc"])), -c["uses"], c["name"]))
        lines = "\n".join(f"- {c['sig']}: {c['doc']} [reused in {c['uses']} later tasks]" for c in ranked[:lines_n])
        top = "\n\n".join(c["src"] for c in ranked[:top_k])
        parts.append(
            "LIBRARY: functions this system wrote while solving earlier tasks; they are already defined in the shell. "
            "Each was right for the task it came from but may not fit this one exactly: read its code, print what it "
            "returns, and use the raw APIs when a result looks empty or wrong.\n"
            f"{lines}\n\nMost relevant library code:\n```python\n{top}\n```"
        )
    if precedents and prec_k:
        best = max(precedents, key=lambda p: (len(w & words(p["instruction"])), p["task"]))
        if len(w & words(best["instruction"])) >= 3:
            parts.append(
                f'A similar task solved earlier: "{best["instruction"]}"\nIts solution:\n```python\n{best["src"]}\n```'
            )
    return "\n\n".join(parts)


def _policy_dict(src: str | None) -> dict[str, Any]:
    s = with_policy(Settings(), src)
    return {"index_lines": s.index_lines, "retrieve_k": s.retrieve_k, "precedent_k": s.precedent_k}


def _appworld() -> Any:
    return importlib.import_module("appworld")


def _model_in_worker(spec: str, cache: str, ledger: str) -> Model:
    return make_model(spec, cache=CallCache(Path(cache)), ledger=Ledger(Path(ledger)))


def episode_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run one task in a fresh world (in a worker process); return a serialized episode."""
    os.environ["APPWORLD_ROOT"] = job["root"]
    aw = _appworld()
    model = _model_in_worker(job["model"], job["cache"], job["ledger"])
    attempts: list[dict[str, Any]] = []
    with aw.AppWorld(task_id=job["task"], experiment_name=job["experiment"]) as world:
        sup = world.task.supervisor
        if job["library"]:
            world.execute(job["library"])
            world.execute(TRACER % json.dumps(job["names"]))
        head = (
            f"Task (from {sup['first_name']} {sup['last_name']}, email {sup['email']}, phone {sup['phone_number']}):"
            f"\n{world.task.instruction}"
        )
        others = [p for p in job["precedents"] if p["task"] != job["task"]]
        ctx = context_text(job["index"], others, world.task.instruction, job.get("policy")) if job["show"] else ""
        system = SYSTEM
        if job.get("notes"):
            system += "\n\nOperating notes (written by this system from its own experience):\n" + job["notes"]
        history: list[tuple[str, str]] = []
        for turn in range(job["turns"]):
            past = [
                f"```python\n{c}\n```\nOutput:\n{o if k >= len(history) - 4 else o[:300]}"
                for k, (c, o) in enumerate(history)
            ]
            user = "\n\n".join([head] + ([ctx] if ctx else []) + past + ["Write the next code cell."])
            c = model.complete(
                system,
                user,
                sample=job["sample"] + turn,
                role="worker",
                meta={**job["meta"], "task": job["task"], "attempt": str(turn)},
            )
            code = first_block(c.text) if c.ok else None
            out = world.execute(code) if code else "No ```python block found."
            history.append((code or c.text[:300], out[-2500:]))
            attempts.append(
                {
                    "index": turn,
                    "kind": "turn",
                    "source": code or "",
                    "train_ok": [],
                    "test_outputs": [],
                    "status": "ok" if code else ("no_code" if c.ok else "infra_error"),
                    "call_key": c.key,
                    "in_tokens": c.usage.input_tokens + c.usage.cache_read + c.usage.cache_write,
                    "out_tokens": c.usage.output_tokens,
                    "usd": c.usd(),
                    "latency_s": c.latency_s,
                }
            )
            if world.task_completed():
                break
        used: dict[str, int] = {}
        if job["library"]:
            try:
                used = json.loads(world.execute("print(_aosr_json.dumps(_aosr_calls))").strip() or "{}")
            except (json.JSONDecodeError, ValueError):
                used = {}
        result = world.evaluate()
        return {
            "task": job["task"],
            "attempts": attempts,
            "success": bool(result.success),
            "used": used,
            "instruction": world.task.instruction,
            "outputs": [o[:400] for _, o in history],
            "passes": result.pass_count,
            "tests": result.num_tests,
        }


def _replay(aw: Any, task: str, library: str, code: str, stub: str | None, experiment: str) -> bool:
    with aw.AppWorld(task_id=task, experiment_name=experiment) as world:
        if library:
            world.execute(library)
        world.execute(code)
        if stub:
            world.execute(f"def {stub}(*args, **kwargs):\n    return None\n")
        world.execute("solve(apis)")
        return bool(world.evaluate().success)


def consolidate_job(job: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a solved episode into helpers + solve(apis); keep what replays and is needed."""
    os.environ["APPWORLD_ROOT"] = job["root"]
    aw = _appworld()
    model = _model_in_worker(job["model"], job["cache"], job["ledger"])
    out: dict[str, Any] = {
        "task": job["task"],
        "ok": False,
        "caps": [],
        "program": None,
        "used": job["used"],
        "instruction": job["instruction"],
        "calls": 1,
    }
    cells = "\n\n".join(f"# cell {k + 1}\n{c}" for k, c in enumerate(job["cells"]) if c)
    index = "\n".join(f"- {c['sig']}: {c['doc']}" for c in job["index"])
    user = (f"LIBRARY:\n{index}\n\n" if index else "") + f"```python\n{cells}\n```"
    c = model.complete(CONSOLIDATE, user, sample=0, role="consolidator", meta={**job["meta"], "task": job["task"]})
    code = first_block(c.text) if c.ok else None
    out["usd"] = c.usd()
    if not code:
        return out
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return out
    if not _replay(aw, job["task"], job["library"], code, None, job["experiment"]):
        return out
    out["ok"], out["program"] = True, code
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name != "solve"]
    known = set(job["names"])
    for fn in defs[:6]:
        if fn.name in known or _replay(aw, job["task"], job["library"], code, fn.name, job["experiment"]):
            continue  # already in the library, or not needed for this task
        doc = (ast.get_docstring(fn) or "").strip().splitlines()
        args = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        loads = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        out["caps"].append(
            {
                "name": fn.name,
                "src": ast.unparse(fn),
                "doc": doc[0][:160] if doc else "(no docstring)",
                "sig": f"{fn.name}({', '.join(args)})",
                "arity": len(args),
                "deps": sorted(loads & (known | {d.name for d in defs})),
            }
        )
    return out


class AppEngine:
    """AppWorld episodes and growth, run in worker processes."""

    name = "appworld"
    policy_doc = (
        "index_lines (0-100): library functions listed by signature and docstring (0 hides the list). "
        "retrieve_k (0-10): most relevant library functions shown with full source. "
        "precedent_k (0-5): 0 hides, 1 or more shows, the most similar earlier solved task and its program."
    )

    def __init__(self, store: Store, data_root: Path, parallel: int = 10) -> None:
        self.store, self.root, self.parallel = store, data_root, parallel

    def _ids(self, split: str) -> list[str]:
        path = self.root / "data" / "datasets" / f"{'train' if split == 'stream' else split}.txt"
        ids = [x.strip() for x in path.read_text().splitlines() if x.strip()]
        if split == "stream":
            random.Random(0).shuffle(ids)
        return ids

    def load(self, split: str, n: int | None = None, offset: int = 0) -> list[Task]:
        ids = self._ids(split)[offset:]
        return [Task(t, split, (), ()) for t in (ids[:n] if n else ids)]

    def _common(self, image: Image, model: Model, meta: dict[str, str]) -> dict[str, Any]:
        spec = getattr(model, "spec", None)
        if spec is None:
            raise ValueError("AppWorld workers need a model built by make_model (with a spec)")
        names = image.names(None)
        index = [
            {**{k: image.cap(n)[k] for k in ("sig", "doc", "src")}, "name": n, "uses": image.head.caps[n].uses}
            for n in image.names()
        ]
        precedents = [
            {"task": t, "src": s, "instruction": self.store.get(image.head.programs[t]).get("instruction", "")}
            for t, s in image.programs()
        ]
        tag = meta.get("run") or meta.get("arm") or "x"
        return {
            "root": str(self.root),
            "model": spec,
            "cache": str(config.llm_cache_path()),
            "ledger": str(config.home() / "ledgers" / f"{tag}.jsonl"),
            "library": image.library_source() if names else "",
            "names": names,
            "index": index,
            "precedents": precedents,
            "notes": image.slot_source("notes") or "",
            "policy": _policy_dict(image.slot_source("policy")),
            "meta": meta,
            "experiment": f"aosr-{tag}",
        }

    def run(
        self, tasks: list[Task], image: Image, model: Model, settings: Settings, meta: dict[str, str]
    ) -> list[Episode]:
        common = self._common(image, model, meta)
        if settings.context == "none":
            common.update(library="", names=[], index=[], precedents=[])
        jobs = [
            {
                **common,
                "task": t.id,
                "turns": settings.budget,
                "sample": settings.sample_offset,
                "show": settings.context != "none",
            }
            for t in tasks
        ]
        with ProcessPoolExecutor(max(1, min(self.parallel, len(jobs)))) as ex:
            results = list(ex.map(episode_job, jobs))
        return [self._episode(r) for r in results]

    @staticmethod
    def _episode(r: dict[str, Any]) -> Episode:
        atts = [Attempt(**a) for a in r["attempts"]]
        if atts:
            atts[-1].used = dict(r["used"])
        ep = Episode(r["task"], atts, retrieved=sorted(r["used"]))
        ep.success = r["success"]
        ep.extra = {
            "instruction": r["instruction"],
            "passes": r["passes"],
            "tests": r["tests"],
            "outputs": r["outputs"],
        }
        return ep

    def digest(self, task: Task, episode: Episode) -> str:
        verdict = "SOLVED" if episode.success else "FAILED"
        lines = [f'Task: "{episode.extra.get("instruction", "")}" -- {verdict} after {episode.calls} turns.']
        outs = episode.extra.get("outputs", [])
        for a, o in list(zip(episode.attempts, outs, strict=False))[-5:]:
            code = "\n".join(a.source.splitlines()[:12])
            lines.append(f"```python\n{code}\n```\nOutput: {o[:300]}")
        return "\n".join(lines)

    def judge(self, task: Task, episode: Episode) -> bool:
        return bool(episode.success)

    def calls_to_solution(self, episode: Episode) -> int | None:
        return episode.calls if episode.success else None

    def contribute(
        self, image: Image, solved: list[tuple[Task, Episode, int]], model: Model, meta: dict[str, str]
    ) -> list[Any]:
        if not solved:
            return []
        common = self._common(image, model, meta)
        jobs = [
            {
                **common,
                "task": t.id,
                "cells": [a.source for a in ep.attempts],
                "used": sorted(ep.retrieved),
                "instruction": ep.extra.get("instruction", ""),
                "experiment": f"{common['experiment']}-replay",
            }
            for t, ep, _ in solved
        ]
        with ProcessPoolExecutor(max(1, min(self.parallel, len(jobs)))) as ex:
            return list(ex.map(consolidate_job, jobs))

    def apply(self, head: Head, contribution: Any, origin: dict[str, Any]) -> dict[str, Any]:
        c: dict[str, Any] = contribution
        for n in c["used"]:
            if n in head.caps:
                head.caps[n].uses += 1
        added = []
        if c["ok"]:
            position = len(head.programs)
            for cap in c["caps"]:
                if cap["name"] in head.caps:
                    continue
                obj = {"type": "cap", "imports": [], "origin": {**origin, "task": c["task"]}, **cap}
                head.caps[cap["name"]] = CapEntry(self.store.put(obj), ACTIVE, 0, position)
                added.append(cap["name"])
            head.programs[c["task"]] = self.store.put(
                {
                    "type": "program",
                    "task": c["task"],
                    "src": c["program"],
                    "instruction": c["instruction"],
                    "calls": c["used"],
                }
            )
        return {"task": c["task"], "replayed": c["ok"], "added": added, "used": c["used"]}
