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
