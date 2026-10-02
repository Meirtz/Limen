"""Run grown code in a child process: restricted builtins and imports, per-call time limits, call tracing.

Every request is answered by a fresh ``python -I`` child that receives the library source, the
program and the inputs as JSON on stdin. Library functions are wrapped so each call is counted and
the caller-to-callee edges and the deepest nesting are recorded; that dynamic call graph is how
reuse and depth are measured. Results are cached by request content, which makes growth decisions
replayable independently of machine load.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aosr import config

BANNED_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        "globals",
        "locals",
        "vars",
        "memoryview",
        "__import__",
        "__builtins__",
        "__loader__",
        "__spec__",
    }
)

CHILD = r"""
import builtins, copy, json, signal, sys
req = json.loads(sys.stdin.read())
sys.setrecursionlimit(4000)
_exec = exec
_real_import = builtins.__import__
ALLOWED = set(req["allowed"])

def _import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in ALLOWED:
        raise ImportError("import of %r is not allowed" % name)
    return _real_import(name, globals, locals, fromlist, level)

BANNED = set(req["banned"])
safe = {k: v for k, v in vars(builtins).items() if k not in BANNED}
safe["__import__"] = _import

class Timeout(BaseException):
    pass

def _alarm(*_):
    raise Timeout()

signal.signal(signal.SIGALRM, _alarm)
CALLS, EDGES, STACK, MAXD, RECORDS = {}, set(), [], [0], {}
RECORD = set(req.get("record", ()))
TRACED = set(req.get("trace", ()))

def norm(v, d=0):
    if d > 64:
        raise ValueError("value nested too deeply")
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else repr(v)
    if isinstance(v, (list, tuple)):
        return [norm(x, d + 1) for x in v]
    if isinstance(v, dict):
        return {str(k): norm(x, d + 1) for k, x in v.items()}
    if isinstance(v, (set, frozenset)):
        return sorted((norm(x, d + 1) for x in v), key=repr)
    return "<%s>" % type(v).__name__

def wrap(name, f):
    counted = name in TRACED
    def traced(*a, **k):
        if counted:
            CALLS[name] = CALLS.get(name, 0) + 1
            if STACK:
                EDGES.add((STACK[-1], name))
            STACK.append(name)
            if len(STACK) > MAXD[0]:
                MAXD[0] = len(STACK)
        before = None
        if name in RECORD and len(RECORDS.setdefault(name, [])) < 8:
            try:
                before = norm(list(a))  # arguments as passed, before any in-place change
            except Exception:
                before = None
        try:
            out = f(*a, **k)
        finally:
            if counted:
                STACK.pop()
        if before is not None:
            try:
                RECORDS[name].append([before, norm(out)])
            except Exception:
                pass
        return out
    traced.__name__, traced.__doc__, traced.__wrapped__ = name, f.__doc__, f
    return traced

def call(f, x):
    x = copy.deepcopy(x)  # each call gets its own copy, so in-place edits cannot leak between calls
    signal.setitimer(signal.ITIMER_REAL, req["call_s"])
    try:
        v = f(x)
        signal.setitimer(signal.ITIMER_REAL, 0)
        return {"ok": norm(v)}
    except Timeout:
        return {"err": "Timeout: exceeded %.2fs" % req["call_s"]}
    except BaseException as e:
        signal.setitimer(signal.ITIMER_REAL, 0)
        return {"err": "%s: %s" % (type(e).__name__, str(e)[:200])}

def load(src, ns, what):
    try:
        _exec(compile(src, what, "exec"), ns)
        return None
    except BaseException as e:
        return "%s while loading %s: %s" % (type(e).__name__, what, str(e)[:200])

ns = {"__builtins__": safe, "__name__": "aosr_lib"}
lib = req["library"]
out = {"error": None, "lib_errors": []}
if isinstance(lib, str):
    out["error"] = load(lib, ns, "<library>")
else:
    # one chunk per capability: a capability that fails to load is reported and skipped, not fatal
    for k, chunk in enumerate(lib):
        err = load(chunk, ns, "<library chunk %d>" % k)
        if err:
            out["lib_errors"].append(err)
for n in req.get("trace", ()):
    if callable(ns.get(n)):
        ns[n] = wrap(n, ns[n])
mode = req["mode"]
if out["error"] is None and mode == "run":
    out["error"] = load(req["program"], ns, "<program>")
    f = ns.get(req["entry"])
    if out["error"] is None and not callable(f):
        out["error"] = "program does not define %s()" % req["entry"]
    if out["error"] is None:
        for n in req.get("record", ()):
            if n not in req.get("trace", ()) and callable(ns.get(n)):
                ns[n] = wrap(n, ns[n])
        out["results"] = [call(f, x) for x in req["inputs"]]
elif out["error"] is None and mode == "apply":
    out["by_name"] = {n: [call(ns[n], x) for x in req["inputs"]] for n in req["names"] if callable(ns.get(n))}
elif out["error"] is None and mode == "programs":
    res = {}
    for pid, src in req["programs"]:
        ns2 = dict(ns)
        err = load(src, ns2, "<program %s>" % pid)
        f = ns2.get(req["entry"])
        if err is None and callable(f):
            res[pid] = [call(f, x) for x in req["inputs"]]
    out["by_program"] = res
out["trace"] = {"calls": CALLS, "edges": sorted(EDGES), "max_depth": MAXD[0], "records": RECORDS}
sys.stdout.write(json.dumps(out))
"""


def check_source(src: str, allowed: Iterable[str] = config.ALLOWED_IMPORTS) -> str | None:
    """Return why a source is not acceptable, or None. A measurement-hygiene check, not a security boundary."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"syntax error: {e.msg} (line {e.lineno})"
    allowed = set(allowed)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if a.name.split(".")[0] not in allowed]
            if bad:
                return f"import of {bad[0]} is not allowed"
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in allowed:
                return f"import from {node.module} is not allowed"
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            return f"use of {node.id} is not allowed"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            return f"use of attribute {node.attr} is not allowed"
    return None


@dataclass
class Trace:
    calls: dict[str, int] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    max_depth: int = 0
    records: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class RunResult:
    status: str  # ok | rejected | crash | timeout
    outputs: list[Any]
    errors: list[str | None]
    trace: Trace
    detail: str = ""

    def matches(self, wanted: list[Any]) -> list[bool]:
        return [e is None and o == w for o, e, w in zip(self.outputs, self.errors, wanted, strict=False)]


def _jail() -> list[str]:
    """On macOS, deny the child reads of task data and any network access (sandbox-exec); elsewhere, nothing."""
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        return []
    denied = " ".join(f'(subpath "{p.resolve()}")' for p in (config.data_dir(), config.appworld_root()) if p.exists())
    profile = "(version 1)(allow default)(deny network*)" + (f"(deny file-read* {denied})" if denied else "")
    return ["/usr/bin/sandbox-exec", "-p", profile]


class Sandbox:
    CHUNK = 40  # capabilities or programs per child request

    def __init__(
        self,
        cache_path: Path | None = None,
        *,
        allowed: Iterable[str] = config.ALLOWED_IMPORTS,
        call_s: float = 2.0,
        wall_s: float = 60.0,
    ) -> None:
        self.allowed = tuple(sorted(allowed))
        self.call_s, self.wall_s = call_s, wall_s
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(cache_path), check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, result TEXT)")

    def _child(self, req: dict[str, Any]) -> dict[str, Any]:
        req = {
            **req,
            "allowed": list(self.allowed),
            "banned": sorted(BANNED_NAMES - {"__builtins__"}),
            "call_s": self.call_s,
        }
        blob = json.dumps(req, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(blob.encode()).hexdigest()
        if self._db is not None:
            with self._lock:
                row = self._db.execute("SELECT result FROM results WHERE key = ?", (key,)).fetchone()
            if row:
                cached: dict[str, Any] = json.loads(row[0])
                return cached
        n = max(1, len(req.get("inputs", ())))
        k = max(1, len(req.get("names", ())) + len(req.get("programs", ())))
        wall = min(self.wall_s, 5.0 + self.call_s * n * k * 1.5)
        try:
            proc = subprocess.run(
                [*_jail(), sys.executable, "-I", "-c", CHILD],
                input=blob,
                capture_output=True,
                text=True,
                timeout=wall,
                env={"PYTHONHASHSEED": "0", "PATH": "/usr/bin:/bin"},
                cwd=str(config.empty_cwd()),
            )
            out: dict[str, Any] = json.loads(proc.stdout) if proc.stdout else {"error": proc.stderr[-300:] or "crash"}
        except subprocess.TimeoutExpired:
            out = {"error": f"Timeout: the sandbox exceeded {wall:.0f}s", "timeout": True}
        except (json.JSONDecodeError, OSError) as e:
            out = {"error": f"crash: {e}"}
        timed_out = out.get("timeout") or "Timeout" in json.dumps(
            out.get("results") or out.get("by_name") or out.get("by_program") or ""
        )
        if self._db is not None and not timed_out:  # time limits depend on load, so timeouts are retried later
            with self._lock:
                self._db.execute("INSERT OR IGNORE INTO results VALUES (?, ?)", (key, json.dumps(out)))
        return out

    @staticmethod
    def _trace(d: dict[str, Any]) -> Trace:
        t = d.get("trace") or {}
        return Trace(
            calls=dict(t.get("calls", {})),
            edges=[(a, b) for a, b in t.get("edges", [])],
            max_depth=int(t.get("max_depth", 0)),
            records=dict(t.get("records", {})),
        )

    def run(
        self,
        library: str | list[str],
        program: str,
        inputs: list[Any],
        *,
        entry: str = "solve",
        trace: Iterable[str] = (),
        record: Iterable[str] = (),
    ) -> RunResult:
        """Load the library, then the program, and call ``entry`` on each input."""
        config.empty_cwd().mkdir(parents=True, exist_ok=True)
        reason = check_source(program, self.allowed)
        if reason:
            return RunResult("rejected", [None] * len(inputs), [reason] * len(inputs), Trace(), reason)
        d = self._child(
            {
                "mode": "run",
                "library": library,
                "program": program,
                "entry": entry,
                "inputs": inputs,
                "trace": sorted(set(trace)),
                "record": sorted(set(record)),
            }
        )
        if d.get("error") or "results" not in d:
            err = str(d.get("error") or "crash")
            status = "timeout" if d.get("timeout") else "crash"
            return RunResult(status, [None] * len(inputs), [err] * len(inputs), self._trace(d), err)
        outs = [r.get("ok") for r in d["results"]]
        errs = [r.get("err") for r in d["results"]]
        status = "timeout" if any(e and e.startswith("Timeout") for e in errs) else "ok"
        return RunResult(status, outs, errs, self._trace(d))

    def apply(self, library: str | list[str], names: list[str], inputs: list[Any]) -> dict[str, list[Any]]:
        """Call each named library function on each input; failed calls give None."""
        if not names or not inputs:
            return {}
        config.empty_cwd().mkdir(parents=True, exist_ok=True)
        out: dict[str, list[Any]] = {}
        for i in range(0, len(names), self.CHUNK):  # bounded requests, so a large image is not cut off by time
            d = self._child({"mode": "apply", "library": library, "names": names[i : i + self.CHUNK], "inputs": inputs})
            out.update({n: [r.get("ok") for r in rs] for n, rs in (d.get("by_name") or {}).items()})
        return out

    def programs(
        self, library: str | list[str], programs: list[tuple[str, str]], inputs: list[Any], *, entry: str = "solve"
    ) -> dict[str, list[Any]]:
        """Run several whole programs (each in its own namespace on top of the library) on the inputs."""
        if not programs or not inputs:
            return {}
        config.empty_cwd().mkdir(parents=True, exist_ok=True)
        out: dict[str, list[Any]] = {}
        for i in range(0, len(programs), self.CHUNK):
            chunk = [list(p) for p in programs[i : i + self.CHUNK]]
            d = self._child(
                {"mode": "programs", "library": library, "programs": chunk, "inputs": inputs, "entry": entry}
            )
            out.update({p: [r.get("ok") for r in rs] for p, rs in (d.get("by_program") or {}).items()})
        return out
