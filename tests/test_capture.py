"""End-to-end capture through ``limen run`` in a subprocess (audit hooks are process-wide)."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time

import pytest

from conftest import Project


def test_records_env_reads_but_not_bulk_copies(project: Project) -> None:
    project.write(
        "main.py",
        """
        import os, subprocess, sys
        a = os.environ.get("WANTED")
        b = "PROBE" in os.environ
        env = dict(os.environ)              # bulk copy: not a read of every variable
        env2 = {**os.environ}
        os.environ["MINE"] = "x"            # set by the run itself
        c = os.environ["MINE"]
        d = os.getenv("VIA_GETENV", "default")
    """,
    )
    project.limen("run", "main.py", env={"WANTED": "yes", "NOISE": "n"}, check=True)
    env = project.last()["env"]
    assert env["WANTED"] == {"present": True, "value": "yes"}
    assert env["PROBE"] == {"present": False}
    assert env["VIA_GETENV"] == {"present": False}
    assert env["MINE"].get("set_by_run")
    assert "NOISE" not in env and "PATH" not in env
    assert project.last()["env_bulk_read"] is True


def test_secret_like_values_are_hashed(project: Project) -> None:
    project.write("main.py", "import os\nos.environ.get('API_TOKEN')\n")
    project.limen("run", "main.py", env={"API_TOKEN": "hunter2"}, check=True)
    entry = project.last()["env"]["API_TOKEN"]
    assert "value" not in entry and "sha256" not in entry
    assert entry["digest"].startswith("hmac:"), "a keyed digest: not reversible by guessing"
    assert hashlib.sha256(b"hunter2").hexdigest() not in json.dumps(project.last())


def test_records_files_read_written_and_listed(project: Project) -> None:
    project.write("data/train.txt", "abc\n")
    project.write("data/docs/a.md", "a")
    project.write(
        "main.py",
        """
        import os
        open("data/train.txt").read()
        os.listdir("data/docs")
        os.makedirs("out", exist_ok=True)
        with open("out/result.json", "w") as f:
            f.write("{}")
        open("out/result.json").read()     # reading its own output is not an input
    """,
    )
    project.limen("run", "main.py", check=True)
    rec = project.last()
    reads = rec["files"]["read"]
    train = str(project.root / "data/train.txt")
    assert reads[train]["sha256"] == hashlib.sha256(b"abc\n").hexdigest()
    assert str(project.root / "data/docs") + os.sep in reads
    out = str(project.root / "out/result.json")
    assert out in rec["files"]["written"]
    assert reads[out].get("self_written")
    assert not any(p.endswith(".py") for p in reads), "imported modules are not data inputs"


def test_records_subprocesses(project: Project) -> None:
    project.write("main.py", "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'])\n")
    project.limen("run", "main.py", check=True)
    subs = project.last()["subprocesses"]
    assert subs and subs[0]["argv"][1:] == ["-c", "pass"] and subs[0]["env"] == "inherited"


def test_modules_origin_git_state_and_copies(project: Project) -> None:
    project.write("main.py", "import pkg\nimport json\n")
    project.commit()
    project.write("src/pkg/__init__.py", "VALUE = 2\n")  # now modified vs HEAD
    project.limen("run", "main.py", env={"PYTHONPATH": "src"}, check=True)
    rec = project.last()
    mod = rec["modules"]["pkg"]
    assert mod["origin"] == "project" and mod["git"] == "modified"
    assert "json" not in rec["modules"], "stdlib is not recorded as project code"
    assert rec["main"]["git"] == "clean"
    obj = project.root / ".limen/objects" / mod["sha256"][:2] / mod["sha256"]
    assert obj.read_text() == "VALUE = 2\n", "executed source is preserved"
    assert (project.root / ".limen/.gitignore").read_text().strip().endswith("*"), "records are private by default"


def test_detects_shadowing_copy(project: Project) -> None:
    project.write("scripts/pkg/__init__.py", "VALUE = 'stale'\n")
    project.write("main.py", "import sys\nsys.path.insert(0, 'scripts')\nimport pkg\n")
    project.limen("run", "main.py", check=True)
    info = project.last()["imports"]["packages"]["pkg"]
    assert info["loaded_from"].endswith(os.path.join("scripts", "pkg"))
    assert info["canonical"].endswith(os.path.join("src", "pkg"))
    out = project.limen("check")
    assert out.returncode == 1 and "SHADOWED" in out.stdout


def test_exit_statuses(project: Project) -> None:
    project.write("ok.py", "print('hi')\n")
    project.write("exit3.py", "import sys\nsys.exit(3)\n")
    project.write("boom.py", "raise RuntimeError('boom')\n")
    project.write("msg.py", "import sys\nsys.exit('bad input')\n")
    assert project.limen("run", "ok.py").returncode == 0
    assert project.last()["status"] == "ok"
    assert project.limen("run", "exit3.py").returncode == 3
    assert project.last()["status"] == "failed" and project.last()["exit_code"] == 3
    out = project.limen("run", "boom.py")
    assert out.returncode == 1 and "RuntimeError: boom" in out.stderr
    assert project.last()["exception"] == "RuntimeError: boom"
    out = project.limen("run", "msg.py")
    assert out.returncode == 1 and "bad input" in out.stderr


def test_script_sees_normal_argv_and_main(project: Project) -> None:
    project.write(
        "main.py",
        """
        import sys, json
        json.dump({"argv": sys.argv, "name": __name__, "path0": sys.path[0]}, open("seen.json", "w"))
    """,
    )
    project.limen("run", "--name", "n", "main.py", "--lr", "0.1", "x", check=True)
    seen = json.loads((project.root / "seen.json").read_text())
    assert seen["argv"] == ["main.py", "--lr", "0.1", "x"] and seen["name"] == "__main__"
    assert os.path.realpath(seen["path0"]) == os.path.realpath(project.root)
    assert project.last()["argv"] == ["main.py", "--lr", "0.1", "x"]


def test_run_module(project: Project) -> None:
    project.write("src/pkg/tool.py", "import limen\nlimen.metric('m', 1.5)\n")
    project.limen("run", "-m", "pkg.tool", env={"PYTHONPATH": "src"}, check=True)
    rec = project.last()
    assert rec["metrics"] == {"m": 1.5}
    assert rec["main"]["path"].endswith(os.path.join("pkg", "tool.py"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_finalizes_record_as_killed(project: Project) -> None:
    project.write("slow.py", "import time\nopen('started', 'w').close()\ntime.sleep(60)\n")
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("LIMEN_")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "limen", "run", "slow.py"],
        cwd=project.root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(200):
        if (project.root / "started").exists():
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM
    rec = project.last()
    assert rec["status"] == "killed" and rec["signal"] == "SIGTERM"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigkill_leaves_incomplete_record(project: Project) -> None:
    project.write("slow.py", "import time\nopen('started', 'w').close()\ntime.sleep(60)\n")
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("LIMEN_")}
    proc = subprocess.Popen([sys.executable, "-m", "limen", "run", "slow.py"], cwd=project.root, env=env)
    for _ in range(200):
        if (project.root / "started").exists():
            break
        time.sleep(0.05)
    proc.kill()
    proc.wait(timeout=30)
    assert project.last()["status"] == "running"
    out = project.limen("check")
    assert out.returncode == 1 and "INCOMPLETE" in out.stdout


def test_context_manager_api(project: Project) -> None:
    project.write(
        "main.py",
        """
        import os, limen
        with limen.run("api", arm="b", treatment=["env:K"]):
            os.environ.get("K")
            limen.outcome("i1", "pass")
            limen.param("lr", 0.1)
        limen.metric("outside", 1)   # no-op outside a run
    """,
    )
    project.limen("run", "--quiet", "--role", "eval", "main.py", check=True)  # nested: declarations merge
    merged = project.last()
    assert merged["declared"]["role"] == "eval" and merged["declared"]["arm"] == "b"
    assert merged["declared"]["treatment"] == ["env:K"] and merged["metrics"] == {"outside": 1}
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("LIMEN_")}
    subprocess.run([sys.executable, "main.py"], cwd=project.root, env=env, check=True)
    rec = project.last()
    assert rec["declared"]["name"] == "api" and rec["declared"]["arm"] == "b"
    assert rec["outcomes"] == {"i1": {"status": "pass"}} and rec["params"] == {"lr": 0.1}
    assert rec["env"]["K"] == {"present": False}
    assert "outside" not in rec["metrics"]


def test_holdout_policy_recorded_from_config(project: Project) -> None:
    project.write("limen.toml", 'holdout = ["data/test"]\n')
    project.write("data/test/q.jsonl", '{"id": 1}\n')
    project.write("train.py", "open('data/test/q.jsonl').read()\n")
    project.limen("run", "--role", "train", "train.py", check=True)
    assert project.last()["policy"]["holdout"] == [str((project.root / "data/test").resolve())]
    out = project.limen("check")
    assert out.returncode == 1 and "HOLDOUT_READ" in out.stdout
    project.limen("run", "--role", "eval", "train.py", check=True)
    assert project.limen("check", "latest").returncode == 0
