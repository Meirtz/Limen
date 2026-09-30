"""Run-level checks, gates, holdout lineage, trace and leak."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import Project, record
from limen import Case, GateControlError, gate, run_controls
from limen.checks import check_run
from limen.leak import overlap, read_ids
from limen.store import Store


def codes(findings: list[Any], severity: str | None = None) -> set[str]:
    return {f.code for f in findings if severity is None or f.severity == severity}


def test_clean_run_has_no_findings() -> None:
    assert check_run(record()) == []


def test_incomplete_and_failed_runs() -> None:
    assert codes(check_run(record(status="running"))) == {"INCOMPLETE"}
    assert "RUN_FAILED" in codes(check_run(record(status="failed", exit_code=2)), "block")


def test_shadowed_and_ambiguous_imports() -> None:
    shadowed = {
        "pkg": {
            "loaded_from": "/p/scripts/pkg",
            "canonical": "/p/src/pkg",
            "candidates": ["/p/scripts/pkg", "/p/src/pkg"],
        }
    }
    assert "SHADOWED" in codes(check_run(record(imports={"packages": shadowed})), "block")
    ambiguous = {
        "pkg": {"loaded_from": "/p/src/pkg", "canonical": "/p/src/pkg", "candidates": ["/p/src/pkg", "/site/pkg"]}
    }
    assert codes(check_run(record(imports={"packages": ambiguous}))) == {"AMBIGUOUS_IMPORT"}


def test_stdlib_shadowing() -> None:
    mods = {
        "select": {"path": "/p/select.py", "origin": "project", "git": "clean", "sha256": "s", "shadows_stdlib": True}
    }
    assert "SHADOWS_STDLIB" in codes(check_run(record(modules=mods)), "block")


def test_code_lineage_warnings() -> None:
    mods = {
        "helper": {"path": "/tmp/scratch/helper.py", "origin": "external", "sha256": "h"},
        "pkg.a": {"path": "/p/src/pkg/a.py", "origin": "project", "git": "untracked", "sha256": "a"},
        "pkg.b": {"path": "/p/src/pkg/b.py", "origin": "project", "git": "modified", "sha256": "b"},
    }
    found = codes(check_run(record(modules=mods)))
    assert {"EXTERNAL_CODE", "UNTRACKED_CODE", "MODIFIED_CODE"} <= found


def test_treatment_not_read_overridden_or_unverified() -> None:
    decl = {"treatment": ["env:WIKI"]}
    assert "TREATMENT_NOT_READ" in codes(check_run(record(declared=decl)), "block")
    overridden = record(declared=decl, env={"WIKI": {"present": True, "value": "x", "set_by_run": True}})
    assert "TREATMENT_OVERRIDDEN" in codes(check_run(overridden), "block")
    spawned = record(declared=decl, subprocesses=[{"argv": ["node"], "env": "inherited"}])
    assert codes(check_run(spawned)) == {"TREATMENT_UNVERIFIED", "CHILD_PROCESSES"}
    ok = record(declared=decl, env={"WIKI": {"present": False}})
    assert check_run(ok) == []
    assert "TREATMENT_NOT_READ" in codes(check_run(record(declared={"treatment": ["param:lr"]})))
    assert check_run(record(declared={"treatment": ["param:lr"]}, params={"lr": 0.1})) == []


def test_gate_findings() -> None:
    assert "GATE_NOT_EXECUTED" in codes(check_run(record(declared={"gates": ["correct"]})), "block")
    bad = {
        "correct": {
            "calls": 3,
            "passed": 3,
            "failed": 0,
            "errors": 0,
            "controls": {"ok": False, "negatives": 1, "negatives_passed": ["null"], "positives_failed": []},
        }
    }
    assert "GATE_CONTROLS_FAILED" in codes(check_run(record(gates=bad)), "block")
    unctl = {"correct": {"calls": 3, "passed": 2, "failed": 1, "errors": 0, "controls": None}}
    assert codes(check_run(record(declared={"gates": ["correct"]}, gates=unctl))) == {"GATE_UNCONTROLLED"}
    rubber = {"g": {"calls": 50, "passed": 50, "failed": 0, "errors": 0, "controls": None}}
    assert "GATE_NEVER_FAILED" in codes(check_run(record(gates=rubber)), "warn")


def test_not_evaluated_and_duplicates() -> None:
    outs = {f"q{i}": {"status": "timeout" if i < 3 else "pass"} for i in range(10)}
    found = check_run(record(outcomes=outs, duplicate_outcomes=2))
    assert {"NOT_EVALUATED", "DUPLICATE_OUTCOMES"} <= codes(found, "warn")


def test_run_controls_and_gate_decorator() -> None:
    def correct(x: int) -> bool:
        return x % 2 == 0

    assert run_controls(correct, positives=[2, 4], negatives=[3])["ok"]
    res = run_controls(lambda x: True, positives=[1], negatives=[Case((0,), label="null")])
    assert not res["ok"] and res["negatives_passed"] == ["null"]
    res = run_controls(lambda x: 1 / 0, positives=[1], negatives=[2])
    assert res["positives_failed"] == ["positive[0]"] and res["negatives_passed"] == []

    @gate("always", positives=[1], negatives=[0])
    def always(x: int) -> bool:
        return True

    with pytest.raises(GateControlError, match="accepted known-bad"):
        always(5)

    @gate("lenient", negatives=[0], on_control_failure="record")
    def lenient(x: int) -> bool:
        return True

    assert lenient(5) is True
    assert always.limen_gate == "always"


def test_gate_is_recorded_in_a_run(project: Project) -> None:
    project.write(
        "main.py",
        """
        import limen
        @limen.gate("even", positives=[2], negatives=[3])
        def even(x):
            return x % 2 == 0
        for i in range(5):
            limen.outcome(i, "pass" if even(i) else "fail")
    """,
    )
    project.limen("run", "--gate", "even", "--gate", "never", "main.py", check=True)
    g = project.last()["gates"]["even"]
    assert (g["calls"], g["passed"], g["failed"]) == (5, 3, 2)
    assert g["controls"]["ok"] and g["controls"]["negatives"] == 1
    out = project.limen("check")
    assert out.returncode == 1 and "GATE_NOT_EXECUTED [never]" in out.stdout


def test_holdout_lineage_through_derived_files(project: Project) -> None:
    project.write("limen.toml", 'holdout = ["data/test.jsonl"]\n')
    project.write("data/test.jsonl", '{"id": "t1"}\n')
    project.write("data/train.jsonl", '{"id": "a"}\n')
    project.write("evaluate.py", "open('data/test.jsonl').read()\nopen('scores.json', 'w').write('{\"acc\": 0.9}')\n")
    project.write("choose.py", "open('scores.json').read()\nopen('choice.txt', 'w').write('b')\n")
    project.write("train.py", "open('choice.txt').read()\nopen('data/train.jsonl').read()\n")
    project.limen("run", "--role", "eval", "evaluate.py", check=True)
    assert project.limen("check", "latest").returncode == 0
    project.limen("run", "--role", "select", "choose.py", check=True)
    out = project.limen("check", "latest", "-v")
    assert out.returncode == 1 and "HOLDOUT_DERIVED" in out.stdout
    project.limen("run", "--role", "train", "train.py", check=True)
    found = check_run(project.last(), Store(project.root / ".limen"))
    chain = next(f for f in found if f.code == "HOLDOUT_DERIVED").evidence["chain"]
    assert len(chain) == 4 and chain[-1].endswith("data/test.jsonl")


def test_trace(project: Project) -> None:
    project.write("data/in.txt", "x")
    project.write("make.py", "open('data/in.txt').read()\nopen('out.txt', 'w').write('y')\n")
    project.write("broken.py", "open('bad.txt', 'w').write('z')\nraise SystemExit(4)\n")
    project.limen("run", "--name", "mk", "make.py", check=True)
    out = project.limen("trace", "out.txt", check=True)
    assert "make.py" in out.stdout and "data/in.txt" in out.stdout and "(no record)" in out.stdout
    project.limen("run", "broken.py")
    out = project.limen("trace", "bad.txt")
    assert out.returncode == 1 and "FROM_FAILED_RUN" in out.stdout
    (project.root / "out.txt").write_text("edited")
    out = project.limen("trace", "out.txt")
    assert "CHANGED_SINCE_RUN" in out.stdout
    (project.root / "loose.txt").write_text("?")
    assert "NO_RECORD" in project.limen("trace", "loose.txt").stdout


def test_leak_ids(tmp_path: Path, project: Project) -> None:
    (tmp_path / "hold.jsonl").write_text('{"meta": {"pid": "p1"}}\n{"meta": {"pid": "p2"}}\n')
    (tmp_path / "train.csv").write_text("pid,x\np3,1\np2,0\n")
    (tmp_path / "ids.txt").write_text("p9\n")
    (tmp_path / "list.json").write_text(json.dumps([{"pid": "p1"}]))
    held = read_ids(tmp_path / "hold.jsonl", "meta.pid")
    assert held == {"p1", "p2"}
    assert overlap(held, read_ids(tmp_path / "train.csv", "pid")) == {"p2"}
    assert overlap(held, read_ids(tmp_path / "ids.txt", None)) == set()
    assert overlap(held, read_ids(tmp_path / "list.json", "pid")) == {"p1"}
    out = project.limen(
        "leak",
        "--holdout",
        str(tmp_path / "hold.jsonl"),
        "--holdout-key",
        "meta.pid",
        "--key",
        "pid",
        str(tmp_path / "train.csv"),
    )
    assert out.returncode == 1 and "LEAK" in out.stdout and "p2" in out.stdout
    with pytest.raises(ValueError, match="missing key"):
        read_ids(tmp_path / "hold.jsonl", "pid")
