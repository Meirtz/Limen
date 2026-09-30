"""The CLI end to end: a placebo A/B, a valid A/B, selection syntax and exit codes."""

from __future__ import annotations

from conftest import Project

EVAL = """
import os, limen
root = os.environ.get("KB_ROOT", "kb/base")
n = len(os.listdir(root))
for i in range(12):
    limen.outcome(f"q{i}", "pass" if i < 4 + n else "fail")
limen.metric("n_docs", n)
"""

STALE = """
import os, limen
root = "kb/base"                     # predates the KB_ROOT switch
n = len(os.listdir(root))
for i in range(12):
    limen.outcome(f"q{i}", "pass" if i < 4 + n else "fail")
limen.metric("n_docs", n)
"""


def setup(project: Project) -> None:
    project.write("kb/base/a.md", "a")
    for x in "abcd":
        project.write(f"kb/rich/{x}.md", x)
    project.write("eval.py", EVAL)
    project.write("eval_stale.py", STALE)
    project.commit()


def test_valid_ab(project: Project) -> None:
    setup(project)
    for seed in range(2):
        project.limen(
            "run",
            "-q",
            "--name",
            "kb",
            "--arm",
            "base",
            "--treatment",
            "env:KB_ROOT",
            "--protocol",
            f"seed={seed}",
            "eval.py",
            check=True,
        )
        project.limen(
            "run",
            "-q",
            "--name",
            "kb",
            "--arm",
            "rich",
            "--treatment",
            "env:KB_ROOT",
            "--protocol",
            f"seed={seed}",
            "eval.py",
            env={"KB_ROOT": "kb/rich"},
            check=True,
        )
    out = project.limen("compare", "kb:base", "kb:rich")
    assert out.returncode == 0, out.stdout
    assert "env:KB_ROOT: A=<unset> | B=kb/rich" in out.stdout
    assert "effect item_pass_rate" in out.stdout and "0 blocking" in out.stdout


def test_placebo_ab(project: Project) -> None:
    setup(project)
    project.limen(
        "run", "-q", "--name", "st", "--arm", "base", "--treatment", "env:KB_ROOT", "eval_stale.py", check=True
    )
    project.limen(
        "run",
        "-q",
        "--name",
        "st",
        "--arm",
        "rich",
        "--treatment",
        "env:KB_ROOT",
        "eval_stale.py",
        env={"KB_ROOT": "kb/rich"},
        check=True,
    )
    out = project.limen("compare", "st:base", "st:rich")
    assert out.returncode == 1
    assert "PLACEBO [env:KB_ROOT]" in out.stdout and "TREATMENT_NOT_READ" in out.stdout
    js = project.limen("compare", "st:base", "st:rich", "--json")
    assert '"blocking": true' in js.stdout


def test_confound_between_arms(project: Project) -> None:
    setup(project)
    project.limen("run", "-q", "--name", "cf", "--arm", "base", "--protocol", "tolerance=0.01", "eval.py", check=True)
    project.limen(
        "run",
        "-q",
        "--name",
        "cf",
        "--arm",
        "rich",
        "--protocol",
        "tolerance=0.0001",
        "eval.py",
        env={"KB_ROOT": "kb/rich"},
        check=True,
    )
    out = project.limen("compare", "cf:base", "cf:rich", "--treatment", "env:KB_ROOT")
    assert out.returncode == 1 and "CONFOUND [protocol:tolerance]" in out.stdout
    ok = project.limen("compare", "cf:base", "cf:rich", "--treatment", "env:KB_ROOT", "--waive", "protocol:tolerance")
    assert ok.returncode == 0


def test_usage_errors(project: Project) -> None:
    assert project.limen("compare", "nope", "nada").returncode == 2
    assert project.limen("run", "--treatment", "bogus", "x.py").returncode == 2
    assert project.limen("run", "missing.py").returncode == 2
    assert project.limen("run", "--protocol", "noequals", "x.py").returncode == 2
    assert "limen" in project.limen("--version").stdout


def test_ls_show_select(project: Project) -> None:
    setup(project)
    project.limen("run", "-q", "--name", "one", "eval.py", check=True)
    rid = project.last()["id"]
    assert rid in project.limen("ls").stdout
    assert f'"id": "{rid}"' in project.limen("show", rid[:18]).stdout
    assert project.limen("check", "one").returncode == 0
    assert project.limen("check", str(project.root / ".limen" / "runs" / f"{rid}.json")).returncode == 0


def test_omitted_runs_warning(project: Project) -> None:
    setup(project)
    for _ in range(3):
        project.limen("run", "-q", "--name", "sel", "--arm", "b", "eval.py", env={"KB_ROOT": "kb/rich"}, check=True)
    project.limen("run", "-q", "--name", "sel", "--arm", "a", "eval.py", check=True)
    ids = [r["id"] for r in project.runs() if r["declared"]["arm"] == "b"]
    a_id = project.last()["id"]
    out = project.limen("compare", a_id, ids[1], "--treatment", "env:KB_ROOT")
    assert "OMITTED_RUNS [sel:b]" in out.stdout and "2 other completed run(s)" in out.stdout
    assert "OMITTED_RUNS" not in project.limen("compare", "sel:a", "sel:b", "--treatment", "env:KB_ROOT").stdout
