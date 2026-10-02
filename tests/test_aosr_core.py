"""AOSR building blocks: sandbox, store and images, admission, model device, statistics."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from aosr import stats
from aosr.admit import Admitter, lift, rename
from aosr.domains import Task
from aosr.kernel import Attempt
from aosr.llm import (
    CachedModel,
    CacheMiss,
    CallCache,
    ClaudeCLI,
    Completion,
    FakeModel,
    LiveCallsDisabled,
    ReplayModel,
    Usage,
)
from aosr.sandbox import Sandbox, check_source
from aosr.store import Head, Image, Store

LIB = '''
def flip(g):
    """Reverse."""
    return g[::-1]


def twice(g):
    """Reverse twice."""
    return flip(flip(g))
'''


# ---------------------------------------------------------------- sandbox


def test_sandbox_runs_and_traces_library_calls() -> None:
    sb = Sandbox(None, call_s=1.0)
    r = sb.run(LIB, "def solve(x):\n    return twice(x)\n", [[1, 2], [3]], trace=["flip", "twice"], record=["flip"])
    assert r.status == "ok" and r.outputs == [[1, 2], [3]]
    assert r.trace.calls == {"twice": 2, "flip": 4}
    assert r.trace.edges == [("twice", "flip")] and r.trace.max_depth == 2
    assert r.trace.records["flip"][0] == [[[1, 2]], [2, 1]]


@pytest.mark.parametrize(
    "program",
    [
        "import os\ndef solve(x): return 1",
        "def solve(x):\n    return open('/etc/hosts').read()",
        "def solve(x):\n    return __import__('os')",
        "def solve(x):\n    return x.__class__",
        "from .x import y\ndef solve(x): return 1",
    ],
)
def test_sandbox_rejects_disallowed_programs(program: str) -> None:
    assert check_source(program) is not None
    assert Sandbox(None).run("", program, [1]).status == "rejected"


def test_sandbox_limits_time_and_reports_errors() -> None:
    sb = Sandbox(None, call_s=0.3)
    r = sb.run("", "def solve(x):\n    while True:\n        pass\n", [1])
    assert r.status == "timeout" and r.errors[0].startswith("Timeout")
    r = sb.run("", "def solve(x):\n    return 1 / x\n", [0, 1])
    assert r.errors[0].startswith("ZeroDivisionError") and r.outputs[1] == 1.0
    assert sb.run("", "x = 1\n", [1]).status == "crash"


def test_sandbox_allowed_imports_and_apply_programs() -> None:
    sb = Sandbox(None)
    assert sb.run("", "import heapq\ndef solve(x):\n    return heapq.nlargest(1, x)\n", [[3, 1]]).outputs == [[3]]
    assert sb.apply(LIB, ["flip", "missing"], [[1, 2]]) == {"flip": [[2, 1]]}
    progs = sb.programs(
        LIB, [("a", "def solve(g):\n    return flip(g)"), ("b", "def solve(g):\n    return 1/0")], [[1, 2]]
    )
    assert progs == {"a": [[2, 1]], "b": [None]}


def test_sandbox_cache_replays(tmp_path: Path) -> None:
    sb = Sandbox(tmp_path / "sb.sqlite")
    a = sb.run(LIB, "def solve(x):\n    return flip(x)\n", [[1, 2]])
    b = sb.run(LIB, "def solve(x):\n    return flip(x)\n", [[1, 2]])
    assert a == b and b.outputs == [[2, 1]]


# ---------------------------------------------------------------- store


def cap(store: Store, name: str, src: str, deps: list[str] | None = None) -> str:
    return store.put(
        {
            "type": "cap",
            "name": name,
            "src": src,
            "imports": [],
            "doc": name,
            "sig": f"{name}(g)",
            "arity": 1,
            "deps": deps or [],
            "origin": {},
        }
    )


def test_store_images_lineage_and_knockout(tmp_path: Path) -> None:
    from aosr.store import CapEntry

    store = Store(tmp_path / "s")
    h0 = store.commit(Head(note="head_0"))
    head = Head(parent=h0, seq=1)
    head.caps["flip"] = CapEntry(cap(store, "flip", "def flip(g):\n    return g[::-1]"), admitted=0)
    head.caps["twice"] = CapEntry(cap(store, "twice", "def twice(g):\n    return flip(flip(g))", ["flip"]), admitted=1)
    head.caps["other"] = CapEntry(cap(store, "other", "def other(g):\n    return g"), admitted=2)
    head.programs["t1"] = store.put(
        {"type": "program", "task": "t1", "src": "def solve(g):\n    return twice(g)", "calls": ["twice", "flip"]}
    )
    h1 = store.commit(head)
    store.set_ref("main", h1)
    assert store.resolve("main") == h1 and store.resolve(h1[:10]) == h1
    assert store.lineage(h1) == [h1, h0]
    image = Image(store, store.head(h1))
    assert image.depths() == {"flip": 1, "twice": 2, "other": 1}
    assert image.dependents({"flip"}) == {"flip", "twice"}
    ko = image.knockout({"flip"})
    assert list(ko.caps) == ["other"] and ko.programs == {}
    src = image.library_source()
    assert src.index("def flip") < src.index("def twice")
    assert store.commit(store.head(h1)) == h1, "commit is content-addressed"


# ---------------------------------------------------------------- admission


def test_lift_moves_only_closure_free_helpers() -> None:
    src = (
        "def solve(grid):\n    h = len(grid)\n    def flip(g):\n        return g[::-1]\n"
        "    def head(g):\n        return g[:h]\n    def both(g):\n        return flip(g)\n"
        "    return both(head(grid))\n"
    )
    out = lift(src)
    assert out.index("def flip") < out.index("def solve") and out.index("def both") < out.index("def solve")
    assert out.index("def head") > out.index("def solve")
    assert rename("def a(x):\n    return b(x)", {"b": "c", "a": "a2"}) == "def a2(x):\n    return c(x)"


MIRROR = (
    'def mirror(g):\n    """Mirror."""\n    return [r[::-1] for r in g]\n\ndef solve(grid):\n    return mirror(grid)\n'
)


def admit_setup(tmp_path: Path) -> tuple[Store, Admitter, Task]:
    store = Store(tmp_path / "s")
    task = Task("abcdef12", "training", (([[1, 2]], [[2, 1]]), ([[3, 4, 5]], [[5, 4, 3]])), ([[7, 8]],))
    return store, Admitter(store, Sandbox(None)), task


def attempt(src: str, records: dict[str, list[object]] | None = None) -> Attempt:
    return Attempt(0, "synth", src, "ok", [True, True], [[[8, 7]]], records=records or {})


def test_admission_keeps_needed_helpers_and_their_callees(tmp_path: Path) -> None:
    store, admitter, task = admit_setup(tmp_path)
    src = (
        'def rev(row):\n    """Reverse a row."""\n    return row[::-1]\n\n'
        'def mirror(g):\n    """Mirror rows."""\n    return [rev(r) for r in g]\n\n'
        'def unused(g):\n    """Not needed."""\n    return g\n\n'
        "def solve(grid):\n    unused(grid)\n    return mirror(grid)\n"
    )
    head = Head()
    adm = admitter.admit(head, task, attempt(src), 0, {})
    assert sorted(adm.added) == ["mirror", "rev"] and adm.vacuous == ["unused"]
    image = Image(store, head)
    assert image.cap("mirror")["deps"] == ["rev"] and image.depths()["mirror"] == 2
    assert list(head.programs) == ["abcdef12"]


def test_admission_lifts_nested_helpers_and_renames_collisions(tmp_path: Path) -> None:
    _, admitter, task = admit_setup(tmp_path)
    head = Head()
    first = MIRROR
    admitter.admit(head, task, attempt(first), 0, {})
    nested = (
        'def solve(grid):\n    def mirror(g):\n        """Mirror again, differently."""\n'
        "        return [list(reversed(r)) for r in g]\n    return mirror(grid)\n"
    )
    adm = admitter.admit(head, task, attempt(nested), 1, {})
    assert adm.lifted and adm.renamed == {"mirror": "mirror_abcdef"} and adm.added == ["mirror_abcdef"]


def test_admission_skips_behavioural_duplicates(tmp_path: Path) -> None:
    _, admitter, task = admit_setup(tmp_path)
    head = Head()
    first = MIRROR
    admitter.admit(head, task, attempt(first), 0, {})
    twin = (
        'def reflect(g):\n    """Same thing."""\n    return [list(reversed(r)) for r in g]\n\n'
        "def solve(grid):\n    return reflect(grid)\n"
    )
    recs = {"reflect": [[[[[1, 2]]], [[2, 1]]], [[[[3, 4, 5]]], [[5, 4, 3]]]]}
    adm = admitter.admit(head, task, attempt(twin, recs), 1, {})
    assert adm.duplicates == {"reflect": "mirror"} and adm.added == []


# ---------------------------------------------------------------- model device


def test_cached_and_replay_models(tmp_path: Path) -> None:
    cache = CallCache(tmp_path / "c.sqlite")
    fake = FakeModel(lambda s, u, k, r: f"echo {u} {k}")
    m = CachedModel(fake, cache, model="fake", thinking=False)
    a = m.complete("sys", "hello", sample=1)
    b = m.complete("sys", "hello", sample=1)
    assert a.text == b.text == "echo hello 1" and not a.cached and b.cached and fake.calls == 1
    replay = ReplayModel(cache, model="fake", thinking=False)
    assert replay.complete("sys", "hello", sample=1).text == "echo hello 1"
    with pytest.raises(CacheMiss):
        replay.complete("sys", "other")
    out = tmp_path / "cassette.jsonl"
    cache.export_jsonl(cache.keys(), out)
    assert CallCache(tmp_path / "d.sqlite").import_jsonl(out) == 1


def test_live_calls_are_refused_offline() -> None:
    with pytest.raises(LiveCallsDisabled):
        ClaudeCLI("claude-haiku-4-5", False, bin_path="claude", cwd=Path("."))


def test_claude_cli_parsing_with_a_fake_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\nsys.stdin.read()\n"
        "print(json.dumps({'result': 'think=' + os.environ.get('MAX_THINKING_TOKENS', 'unset'), 'usage': "
        "{'input_tokens': 10, 'output_tokens': 4, 'cache_read_input_tokens': 2}, 'total_cost_usd': 0.5, "
        "'modelUsage': {'claude-haiku-4-5-20251001': {}}}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AOSR_ALLOW_LIVE", "1")
    monkeypatch.delenv("AOSR_OFFLINE")
    cli = ClaudeCLI("claude-haiku-4-5", False, bin_path=str(script), cwd=tmp_path / "empty")
    c = cli.complete("sys", "user")
    assert c.ok and c.text == "think=0" and c.model_id == "claude-haiku-4-5-20251001"
    assert c.usage == Usage(10, 4, 2, 0) and c.usd() == pytest.approx((12 * 1.0 + 4 * 5.0) / 1e6)
    think = ClaudeCLI("claude-haiku-4-5", True, bin_path=str(script), cwd=tmp_path / "empty")
    assert think.complete("sys", "user").text == "think=unset"
    broken = ClaudeCLI("claude-haiku-4-5", False, bin_path=str(tmp_path / "missing"), cwd=tmp_path, attempts=1)
    assert broken.complete("s", "u").status == "infra_error"
    assert json.loads(json.dumps(c.to_json())) == Completion.from_json(c.to_json()).to_json()
    assert os.environ.get("AOSR_ALLOW_LIVE") == "1"


# ---------------------------------------------------------------- statistics


def test_paired_bootstrap_and_trend() -> None:
    a = [0.0] * 50 + [1.0] * 50
    b = [0.0] * 30 + [1.0] * 70
    iv = stats.paired_diff(a, b, resamples=2000)
    assert iv.estimate == pytest.approx(0.2) and iv.low > 0 and iv.p_le_zero < 0.01
    same = stats.paired_diff(a, a, resamples=500)
    assert same.estimate == 0 and same.low == 0 == same.high
    with pytest.raises(ValueError):
        stats.paired_diff([1.0], [1.0, 0.0])
    rows = [[0.0] * 80 + [1.0] * 20, [0.0] * 70 + [1.0] * 30, [0.0] * 60 + [1.0] * 40]
    tr = stats.log_trend([25, 50, 100], rows, resamples=1000)
    assert tr.estimate > 0 and tr.low > 0


def test_holm_and_wilson() -> None:
    adj = stats.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj == {"a": 0.03, "c": 0.06, "b": 0.06}
    lo, hi = stats.wilson(5, 10)
    assert lo < 0.5 < hi
