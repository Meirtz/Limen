"""The whole loop offline: boot an empty image, grow it on a stream, evaluate frozen images."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aosr import evaluate
from aosr.domains import Arc, Task
from aosr.engine import ArcEngine
from aosr.evaluate import Arm, load_arm, run_arm, summarize
from aosr.grow import GrowConfig, boot, grow
from aosr.kernel import PREV_PROGRAM, Kernel, Settings, code_of
from aosr.llm import FakeModel
from aosr.sandbox import Sandbox
from aosr.store import Image, Store
from aosr_world import oracle

STREAM = {"s1": ["flip_rows"], "s2": ["flip_cols"], "s3": ["transpose"], "s4": ["recolor"]}
DEV = {
    "d1": ["flip_rows", "recolor"],
    "d2": ["transpose", "flip_cols"],
    "d3": ["flip_rows"],
    "d4": ["recolor", "transpose"],
}


@pytest.fixture
def world(tmp_path: Path) -> Arc:
    from aosr_world import make_world

    make_world(tmp_path / "arc", STREAM, "training", seed=1)
    make_world(tmp_path / "arc", DEV, "training", seed=2)
    return Arc(tmp_path / "arc")


def tasks(world: Arc, ids: dict[str, list[str]]) -> list[Task]:
    return world.load(list(ids), "training")


def test_kernel_budget_schedule_and_repair_prompt(world: Arc) -> None:
    store = Store(world.root.parent / "store")
    image = Image(store, store.head(boot(store)))
    prompts: list[str] = []

    def bad(system: str, user: str, sample: int, role: str) -> str:
        prompts.append(user)
        return "```python\ndef solve(grid):\n    return grid\n```" if sample != 2 else "no code here"

    ep = Kernel(world, Sandbox(None)).solve(tasks(world, STREAM)[0], image, FakeModel(bad), Settings(budget=3))
    assert ep.calls == 3 and ep.verified is None
    assert [a.kind for a in ep.attempts] == ["synth", "repair", "synth"] and ep.attempts[2].status == "no_code"
    assert PREV_PROGRAM in prompts[1] and "Example 1: " in prompts[1] and PREV_PROGRAM not in prompts[2]
    assert "LIBRARY" not in prompts[0], "the empty image shows no library"
    assert code_of("x\n```python\na = 1\n```\n```python\nb = 2\n```") == "b = 2\n"


def test_grow_then_evaluate_shows_reuse_and_presolve(world: Arc, tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    h0 = boot(store)
    model = FakeModel(oracle(buggy_first=True))
    sb = Sandbox(tmp_path / "sb.sqlite")
    final = grow(
        store,
        h0,
        tasks(world, STREAM),
        ArcEngine(store, sb, world),
        model,
        tmp_path / "grow",
        GrowConfig(batch=2, settings=Settings(budget=2), checkpoints=(2,)),
        progress=lambda s: None,
    )
    image = Image(store, store.head(final))
    assert set(image.names()) == {"flip_rows", "flip_cols", "transpose", "recolor"}
    assert len(image.head.programs) == 4 and store.resolve("grow/n2") != final
    log = [json.loads(x) for x in (tmp_path / "grow" / "grow.jsonl").read_text().splitlines()]
    assert all(r["correct"] for r in log)

    dev = tasks(world, DEV)
    engine = ArcEngine(store, sb, world)
    seed = run_arm(
        store, Arm("SEED", h0, FakeModel(oracle()), Settings(budget=2)), dev, engine, tmp_path / "e" / "seed.jsonl"
    )
    lib = run_arm(
        store, Arm("LIB", final, FakeModel(oracle()), Settings(budget=2)), dev, engine, tmp_path / "e" / "lib.jsonl"
    )
    s_seed = summarize({"arm": "SEED", "budget": 2}, seed)
    s_lib = summarize({"arm": "LIB", "budget": 2}, lib)
    assert s_seed["solved"] == 4 and s_seed["solved_with_library"] == 0
    assert s_lib["solved"] == 4 and s_lib["solved_with_library"] == 4
    d3 = next(r for r in lib if r["task"] == "d3")
    assert d3["calls"] == 0 and d3["first_verified"]["kind"] == "presolve", "an exact capability needs no model call"
    assert s_lib["calls"] < s_seed["calls"]
    iv = evaluate.compare(load_arm(tmp_path / "e" / "seed.jsonl"), load_arm(tmp_path / "e" / "lib.jsonl"), 2, 2)
    assert iv.estimate == 0

    # knocking out a capability removes it, its dependents and the programs that call it
    ko = Image(store, Image(store, store.head(final)).knockout({"recolor"}))
    assert "recolor" not in ko.names()
    assert all("recolor" not in store.get(d)["calls"] for d in ko.head.programs.values())

    # growth replays exactly from the caches
    again = grow(
        store,
        h0,
        tasks(world, STREAM),
        ArcEngine(store, sb, world),
        FakeModel(oracle(buggy_first=True)),
        tmp_path / "grow2",
        GrowConfig(batch=2, settings=Settings(budget=2)),
        progress=lambda s: None,
    )
    assert again == final


def test_no_context_arm_hides_the_library(world: Arc, tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    h0 = boot(store)
    sb = Sandbox(None)
    final = grow(
        store,
        h0,
        tasks(world, STREAM)[:1],
        ArcEngine(store, sb, world),
        FakeModel(oracle()),
        tmp_path / "g",
        GrowConfig(batch=1),
        progress=lambda s: None,
    )
    seen: list[str] = []

    def spy(system: str, user: str, sample: int, role: str) -> str:
        seen.append(user)
        return oracle()(system, user, sample, role)

    dev = [t for t in tasks(world, DEV) if t.id == "d1"]
    run_arm(
        store,
        Arm("NOCTX", final, FakeModel(spy), Settings(budget=1, context="none")),
        dev,
        ArcEngine(store, sb, world),
        tmp_path / "x.jsonl",
    )
    assert seen and "LIBRARY" not in seen[0]


def test_evaluation_split_is_locked(world: Arc, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PermissionError):
        world.load(["x"], "evaluation")


def test_cli_boot_show_lineage(tmp_path: Path) -> None:
    env_store = str(tmp_path / "store")
    run = lambda *a: subprocess.run([sys.executable, "-m", "aosr", *a], capture_output=True, text=True)  # noqa: E731
    assert run("--help").returncode == 0
    h0 = run("boot", "--store", env_store).stdout.strip()
    assert len(h0) == 64
    shown = run("show", "--store", env_store, "head_0")
    assert shown.returncode == 0 and '"capabilities": 0' in shown.stdout
    assert h0[:12] in run("lineage", "--store", env_store, "head_0").stdout


def test_appworld_context_and_parsing() -> None:
    from aosr.apps import context_text, first_block

    assert first_block("a\n```python\nx = 1\n```\n```python\ny = 2\n```") == "x = 1\n"
    index = [
        {
            "name": "login_app",
            "sig": "login_app(apis, app)",
            "doc": "Log in to an app.",
            "src": "def login_app(apis, app): ...",
            "uses": 3,
        },
        {
            "name": "list_songs",
            "sig": "list_songs(apis)",
            "doc": "All songs in the Spotify library.",
            "src": "def list_songs(apis): ...",
            "uses": 1,
        },
    ]
    precedents = [
        {
            "task": "t1",
            "instruction": "How many songs are in my Spotify playlist named Chill?",
            "src": "def solve(apis): ...",
        }
    ]
    text = context_text(index, precedents, "How many songs are in my Spotify library?")
    assert text.index("list_songs(apis)") < text.index("login_app(apis, app)"), "ranked by overlap with the task"
    assert "A similar task solved earlier" in text
    assert context_text([], [], "anything") == ""


@pytest.mark.parametrize("helps", [True, False])
def test_kernel_evolution_gates_notes_on_an_ab_window(world: Arc, tmp_path: Path, helps: bool) -> None:
    store = Store(tmp_path / "store")
    h0 = boot(store)

    def worker(system: str, user: str, sample: int, role: str) -> str:
        if helps and "Operating notes" in system:
            return oracle()(system, user, sample, role)
        return "```python\ndef solve(grid):\n    return grid\n```"

    engineer = FakeModel(lambda s, u, k, r: "<notes>1. Check every example before answering.</notes>")
    cfg = GrowConfig(batch=2, epoch_every=2, engineer=engineer, window=2, margin=1)
    final = grow(store, h0, tasks(world, STREAM)[:2], ArcEngine(store, Sandbox(None), world), FakeModel(worker),
                 tmp_path / "g", cfg, progress=lambda s: None)  # fmt: skip
    image = Image(store, store.head(final))
    epochs = [json.loads(x) for x in (tmp_path / "g" / "grow.jsonl").read_text().splitlines() if '"epoch"' in x]
    assert len(epochs) == 1 and epochs[0]["accepted"] is helps
    if helps:
        assert image.slot_source("notes") == "1. Check every example before answering."
        assert (epochs[0]["old"], epochs[0]["new"]) == (0, 2)
    else:
        assert image.slot_source("notes") is None
    assert any(e["kind"] == "epoch" for e in store.events())


def test_context_modes_select_parts_of_the_image(world: Arc, tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    sb = Sandbox(None)
    final = grow(store, boot(store), tasks(world, STREAM)[:1], ArcEngine(store, sb, world), FakeModel(oracle()),
                 tmp_path / "g", GrowConfig(batch=1), progress=lambda s: None)  # fmt: skip
    image = Image(store, store.head(final))
    kernel = Kernel(world, sb)
    task = next(t for t in tasks(world, DEV) if t.id == "d1")
    probes = [("flip_rows", 0.5)]
    precs = [("s1", 0.9)]
    full, _, shown = kernel.context(image, probes, precs, Settings(retrieve_min=0.0))
    assert "LIBRARY" in full and "CLOSEST PROGRAMS" in full and shown == ["s1"]
    helpers, _, shown = kernel.context(image, probes, precs, Settings(context="helpers", retrieve_min=0.0))
    assert "LIBRARY" in helpers and "CLOSEST PROGRAMS" not in helpers and shown == []
    progs, retrieved, _ = kernel.context(image, probes, precs, Settings(context="precedents"))
    assert "LIBRARY" not in progs and "CLOSEST PROGRAMS" in progs and retrieved == []
    ep = kernel.solve(task, image, FakeModel(oracle()), Settings(context="none", budget=1))
    assert all(a.kind != "presolve" for a in ep.attempts), "the none arm gets no presolve either"


def test_policy_slot_is_clamped_and_retunes_the_kernel(world: Arc, tmp_path: Path) -> None:
    from aosr.kernel import with_policy

    s = with_policy(Settings(), '{"index_lines": 500, "retrieve_min": -1, "precedent_k": 0, "bogus": 3}')
    assert (s.index_lines, s.retrieve_min, s.precedent_k) == (100, 0.0, 0)
    assert with_policy(Settings(), "not json") == Settings() == with_policy(Settings(), None)

    store = Store(tmp_path / "store")
    sb = Sandbox(None)

    def worker(system: str, user: str, sample: int, role: str) -> str:
        return "```python\ndef solve(grid):\n    return grid\n```"

    reply = '<notes>1. Look closely.</notes><policy>{"index_lines": 0, "precedent_k": 0}</policy>'
    cfg = GrowConfig(batch=2, epoch_every=2, engineer=FakeModel(lambda s, u, k, r: reply), window=2, margin=0)
    final = grow(store, boot(store), tasks(world, STREAM)[:2], ArcEngine(store, sb, world), FakeModel(worker),
                 tmp_path / "g", cfg, progress=lambda s: None)  # fmt: skip
    image = Image(store, store.head(final))
    assert json.loads(image.slot_source("policy") or "{}") == {"index_lines": 0, "precedent_k": 0}
    assert image.slot_source("notes") == "1. Look closely."


def test_demo_page_renders_growth(world: Arc, tmp_path: Path) -> None:
    from aosr.demo import render

    store = Store(tmp_path / "store")
    sb = Sandbox(None)
    grow(store, boot(store), tasks(world, STREAM), ArcEngine(store, sb, world), FakeModel(oracle()), tmp_path / "g",
         GrowConfig(batch=2, run="demo"), progress=lambda s: None)  # fmt: skip
    page = render(store, "demo", tmp_path / "g" / "grow.jsonl", [], "test")
    assert page.startswith("<!doctype html>") and "flip_rows" in page and "<svg" in page
    assert "stream tasks solved" in page and "No kernel epochs" in page


def test_appworld_context_modes_change_what_the_worker_sees(tmp_path: Path) -> None:
    from aosr.apps import AppEngine
    from aosr.llm import CallCache, make_model
    from aosr.store import CapEntry

    store = Store(tmp_path / "store")
    head = store.head(boot(store))
    cap = {"type": "cap", "name": "login", "src": "def login(apis):\n    return 1", "imports": [], "doc": "Log in.",
           "sig": "login(apis)", "arity": 1, "deps": [], "origin": {}}  # fmt: skip
    head.caps["login"] = CapEntry(store.put(cap))
    head.programs["t0"] = store.put({"type": "program", "task": "t0", "src": "def solve(apis): ...", "calls": []})
    image = Image(store, head)
    model = make_model("replay:haiku-nothink", cache=CallCache(tmp_path / "c.sqlite"))
    engine = AppEngine(store, tmp_path)
    task = [Task("t1", "dev", (), ())]

    def job(mode: str) -> dict[str, object]:
        return engine.jobs(task, image, model, Settings(context=mode), {"arm": mode})[0]

    full, helpers, precedents, none = job("full"), job("helpers"), job("precedents"), job("none")
    assert full["index"] and full["precedents"] and full["library"]
    assert helpers["index"] and not helpers["precedents"]
    assert not precedents["index"] and precedents["precedents"] and precedents["library"]
    assert not none["library"] and not none["index"] and not none["precedents"] and none["show"] is False


def test_summaries_can_be_cut_at_a_common_budget() -> None:
    header = {"arm": "A", "budget": 4}
    rows = [
        {"solved_at": [False, False, False, True, True], "calls": 3, "call_usd": [0.1, 0.1, 0.1], "usd": 0.3,
         "errors": 0, "first_verified": {"used": [], "kind": "synth"}},
        {"solved_at": [False, True, True, True, True], "calls": 1, "call_usd": [0.2], "usd": 0.2, "errors": 0,
         "first_verified": {"used": ["f"], "kind": "synth"}},
    ]  # fmt: skip
    full, cut = summarize(header, rows), summarize(header, rows, 2)
    assert (full["solved"], full["calls"], full["usd"]) == (2, 4, 0.5)
    assert (cut["solved"], cut["calls"], cut["usd"]) == (1, 3, 0.4) and cut["solved_with_library"] == 1
    with pytest.raises(ValueError):
        summarize(header, rows, 5)


def test_ab_window_cannot_see_its_own_tasks(world: Arc, tmp_path: Path) -> None:
    from aosr.evolve import blind

    store = Store(tmp_path / "store")
    final = grow(store, boot(store), tasks(world, STREAM), ArcEngine(store, Sandbox(None), world), FakeModel(oracle()),
                 tmp_path / "g", GrowConfig(batch=2), progress=lambda s: None)  # fmt: skip
    head = store.head(final)
    window = [t for t in tasks(world, STREAM) if t.id in ("s1", "s2")]
    seen = blind(store, head, window)
    assert "s1" not in seen.programs and "s2" not in seen.programs and "s3" in seen.programs
    assert "flip_rows" not in seen.caps and "flip_cols" not in seen.caps and "transpose" in seen.caps
