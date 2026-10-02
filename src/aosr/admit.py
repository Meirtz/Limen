"""Admission: what a verified stream solution adds to the image.

A solution that is correct on its task's held-back test pair contributes:
- its helper functions, each admitted only if the solution breaks when that helper is replaced by an
  identity stub (the helper is needed, so its behaviour is pinned by the task), together with the
  helpers it calls; helpers nested in solve are lifted to module level first when they do not use
  solve's local variables;
- the whole program, kept as a precedent that later tasks are probed against.

Name collisions are resolved by renaming the newcomer; a helper that behaves exactly like an
existing capability on every recorded call is not admitted again.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from aosr.domains import Task
from aosr.kernel import Attempt
from aosr.sandbox import Sandbox
from aosr.store import ACTIVE, CapEntry, Head, Image, Store


@dataclass
class Admission:
    task: str
    added: list[str] = field(default_factory=list)
    duplicates: dict[str, str] = field(default_factory=dict)  # new helper -> existing capability
    vacuous: list[str] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)
    lifted: bool = False
    program: str | None = None


def free_loads(fn: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def bound_in(fn: ast.FunctionDef) -> set[str]:
    a = fn.args
    bound = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    bound |= {x.arg for x in (a.vararg, a.kwarg) if x}
    bound |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {n.name for n in ast.walk(fn) if isinstance(n, ast.FunctionDef) and n is not fn}
    return bound


def lift(src: str) -> str:
    """Move functions defined directly inside solve to module level when they do not use solve's locals."""
    tree = ast.parse(src)
    solve = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "solve"), None)
    if solve is None:
        return src
    inner = [s for s in solve.body if isinstance(s, ast.FunctionDef)]
    if not inner:
        return src
    local = {x.arg for x in solve.args.args + solve.args.kwonlyargs}
    for s in solve.body:
        if not isinstance(s, ast.FunctionDef):
            local |= {n.id for n in ast.walk(s) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    liftable = {f.name: f for f in inner if not ((free_loads(f) - bound_in(f)) & local)}
    changed = True
    while changed:  # a helper that calls a sibling which must stay cannot move either
        changed = False
        staying = {f.name for f in inner} - set(liftable)
        for name, f in list(liftable.items()):
            if (free_loads(f) - bound_in(f)) & staying:
                del liftable[name]
                changed = True
    if not liftable:
        return src
    solve.body = [s for s in solve.body if not (isinstance(s, ast.FunctionDef) and s.name in liftable)] or [ast.Pass()]
    at = tree.body.index(solve)
    tree.body[at:at] = list(liftable.values())
    return ast.unparse(tree)


def arity(fn: ast.FunctionDef) -> int:
    a = fn.args
    return len(a.posonlyargs) + len(a.args) - len(a.defaults)


def signature(fn: ast.FunctionDef) -> str:
    args = [x.arg for x in fn.args.posonlyargs + fn.args.args]
    return f"{fn.name}({', '.join(args)})"


class _Rename(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.mapping:
            return ast.copy_location(ast.Name(self.mapping[node.id], node.ctx), node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        if node.name in self.mapping:
            node.name = self.mapping[node.name]
        return node


def rename(src: str, mapping: dict[str, str]) -> str:
    return ast.unparse(_Rename(mapping).visit(ast.parse(src))) if mapping else src


class Admitter:
    def __init__(self, store: Store, sandbox: Sandbox) -> None:
        self.store, self.sandbox = store, sandbox

    def admit(self, head: Head, task: Task, attempt: Attempt, position: int, origin: dict[str, Any]) -> Admission:
        """Mutate ``head`` with what ``attempt`` (verified and test-correct on ``task``) contributes."""
        out = Admission(task.id)
        image = Image(self.store, head)
        library = image.library_source()
        src = attempt.source
        try:
            lifted = lift(src)
        except SyntaxError:
            return out
        if lifted != src and self.sandbox.run(library, lifted, task.train_inputs).matches(task.train_outputs) == [
            True
        ] * len(task.train):
            src, out.lifted = lifted, True
        tree = ast.parse(src)
        imports = [ast.unparse(n) for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name != "solve"}

        # the whole program is kept as a precedent
        calls = sorted(n for n in attempt.used if n in head.caps)
        out.program = self.store.put({"type": "program", "task": task.id, "src": src, "calls": calls,
                                      "origin": {**origin, "position": position}})  # fmt: skip
        head.programs[task.id] = out.program
        for n in calls:
            head.caps[n].uses += 1

        # helpers: needed ones plus what they call
        fresh = {n: d for n, d in defs.items() if n not in head.caps or self._source(image, n) != ast.unparse(d)}
        needed = []
        for name in fresh:
            stub = src + f"\n\ndef {name}(*args, **kwargs):\n    return args[0] if args else None\n"
            if self.sandbox.run(library, stub, task.train_inputs).matches(task.train_outputs) != [True] * len(
                task.train
            ):
                needed.append(name)
            else:
                out.vacuous.append(name)
        closure: list[str] = []
        todo = list(needed)
        while todo:
            n = todo.pop(0)
            if n in closure:
                continue
            closure.append(n)
            todo += sorted(m for m in free_loads(fresh[n]) if m in fresh and m not in closure)
        mapping = {n: f"{n}_{task.id[:6]}" for n in closure if n in head.caps}
        out.renamed = mapping
        records = attempt.records
        for name in closure:
            new = mapping.get(name, name)
            fn = ast.parse(rename(ast.unparse(fresh[name]), mapping)).body[0]
            assert isinstance(fn, ast.FunctionDef)
            twin = self._twin(image, library, fn, records.get(name, []))
            if twin is not None:
                out.duplicates[new] = twin
                continue
            doc = (ast.get_docstring(fn) or "").strip().splitlines()
            deps = sorted(m for m in free_loads(fn) if m in head.caps or m in {mapping.get(c, c) for c in closure})
            cap = {
                "type": "cap", "name": new, "src": ast.unparse(fn), "imports": imports,
                "doc": doc[0][:160] if doc else "(no docstring)", "sig": signature(fn), "arity": arity(fn),
                "deps": [d for d in deps if d != new], "origin": {**origin, "task": task.id, "position": position},
            }  # fmt: skip
            head.caps[new] = CapEntry(self.store.put(cap), ACTIVE, 0, position)
            out.added.append(new)
        return out

    def _source(self, image: Image, name: str) -> str:
        return str(image.cap(name)["src"]) if name in image.head.caps else ""

    def _twin(self, image: Image, library: str, fn: ast.FunctionDef, records: list[Any]) -> str | None:
        """An existing active capability of the same arity that returns the same value on every recorded call."""
        if not records or arity(fn) != 1:
            return None
        inputs = [args[0] for args, _ in records if isinstance(args, list) and len(args) == 1]
        wanted = [result for args, result in records if isinstance(args, list) and len(args) == 1]
        if not inputs:
            return None
        same = [n for n in image.names() if image.cap(n)["arity"] == 1]
        outs = self.sandbox.apply(library, same, inputs)
        return next((n for n in same if outs.get(n) == wanted), None)
