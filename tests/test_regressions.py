"""Regression tests for the findings of the pre-freeze adversarial review (ids in each docstring)."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from conftest import GIT_ENV, Project, record
from limen import fields, redact
from limen.capture import _jsonable, _plain
from limen.checks import check_run
from limen.compare import _t975, compare
from limen.leak import read_ids
from limen.store import Store

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


def codes(findings: list[Any], severity: str | None = None) -> set[str]:
    return {f.code for f in findings if severity is None or f.severity == severity}


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LIMEN_")}
    env.update(GIT_ENV)
    env.update(extra)
    return env


def wait_for(path: Path, seconds: float = 20) -> None:
    for _ in range(int(seconds / 0.05)):
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


# ---- signals and process lifecycle --------------------------------------------------------------


@POSIX
def test_inherited_sighup_ignore_is_kept(project: Project) -> None:
    """CAP-01: under nohup (SIGHUP ignored), a hangup must not kill the experiment."""
    project.write("slow.py", "import time\nopen('started','w').close()\ntime.sleep(1)\nprint('done')\n")
    proc = subprocess.Popen(
        [sys.executable, "-m", "limen", "run", "slow.py"],
        cwd=project.root,
        env=clean_env(),
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for(project.root / "started")
    proc.send_signal(signal.SIGHUP)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0 and b"done" in out
    assert project.last()["status"] == "ok"


def test_sigusr1_is_left_to_the_script(project: Project) -> None:
    """C1: SIGUSR1/SIGUSR2 stay at their default so checkpoint-and-requeue handlers can install."""
    project.write("m.py", "import signal\nassert signal.getsignal(signal.SIGUSR1) is signal.SIG_DFL\n")
    project.limen("run", "m.py", check=True)


@POSIX
def test_forked_child_does_not_overwrite_parent_record(project: Project) -> None:
    """CAP-17: a forked child that exits through sys.exit must not save over the parent's record."""
    project.write(
        "m.py",
        """
        import os, sys, time, limen
        pid = os.fork()
        if pid == 0:
            time.sleep(0.3)
            sys.exit(3)
        limen.outcome("parent", "pass")
        os.waitpid(pid, 0)
    """,
    )
    project.limen("run", "m.py", check=True)
    time.sleep(0.5)
    rec = project.last()
    assert rec["status"] == "ok" and rec["outcomes"] == {"parent": {"status": "pass"}}
    assert any(s["argv"] == ["<fork>"] for s in rec["subprocesses"])


@POSIX
def test_forked_child_dies_on_sigterm(project: Project) -> None:
    """M1: a forked worker gets the default SIGTERM action, not Limen's exception."""
    project.write(
        "m.py",
        """
        import os, signal, time
        pid = os.fork()
        if pid == 0:
            while True:
                try:
                    time.sleep(0.05)
                except BaseException:
                    pass
        time.sleep(0.3)
        os.kill(pid, signal.SIGTERM)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGTERM, status
    """,
    )
    project.limen("run", "m.py", check=True)


def test_threads_and_atexit_are_recorded(project: Project) -> None:
    """CAP-16: outcomes reported by non-daemon threads and atexit handlers are kept."""
    project.write(
        "m.py",
        """
        import atexit, threading, time, limen
        def late():
            time.sleep(0.3)
            limen.outcome("thread", "pass")
        threading.Thread(target=late).start()
        atexit.register(lambda: limen.metric("atexit", 1))
    """,
    )
    project.limen("run", "m.py", check=True)
    rec = project.last()
    assert rec["outcomes"] == {"thread": {"status": "pass"}} and rec["metrics"] == {"atexit": 1}


def test_unserializable_values_do_not_break_the_run(project: Project) -> None:
    """CAP-15: odd param values are made JSON-safe; the script's exit code survives."""
    project.write("m.py", "import sys, limen\nlimen.param('labels', {0: 'cat', 'bg': (1, 2)})\nsys.exit(3)\n")
    assert project.limen("run", "m.py").returncode == 3
    rec = project.last()
    assert rec["status"] == "failed" and rec["params"]["labels"] == {"0": "cat", "bg": [1, 2]}


def test_scalar_like_metrics_become_numbers() -> None:
    """CAP-13/S5: numpy/torch-like scalars are stored as numbers, and numeric strings still compare."""

    class Scalar:
        def item(self) -> float:
            return 0.625

    assert _plain(Scalar()) == 0.625 and _plain(Fraction(1, 4)) == 0.25
    assert _jsonable({"a": (Scalar(), {1, 2})}) == {"a": [0.625, [1, 2]]}
    a = [record(id="a", metrics={"acc": "0.5"})]
    b = [record(id="b", metrics={"acc": 0.75})]
    assert compare(a, b, treatment=[]).effects[0]["delta"] == pytest.approx(0.25)


def test_nan_metric_is_reported() -> None:
    """S5: a diverged replicate (NaN) is surfaced, not silently dropped."""
    a = [record(id="a1", metrics={"m": 1.0}), record(id="a2", metrics={"m": float("nan")})]
    b = [record(id="b1", metrics={"m": 2.0}), record(id="b2", metrics={"m": 2.1})]
    assert "METRIC_NOT_FINITE" in codes(compare(a, b, treatment=[]).findings, "warn")


# ---- the -m runner --------------------------------------------------------------------------------


def test_run_package_records_its_main(project: Project) -> None:
    """CAP-06: `limen run -m pkg` records pkg/__main__.py, which is what executes."""
    project.write("src/pkg/__main__.py", "import limen\nlimen.metric('ran', 1)\n")
    project.limen("run", "-m", "pkg", env={"PYTHONPATH": "src"}, check=True)
    rec = project.last()
    assert rec["main"]["path"].endswith(os.path.join("pkg", "__main__.py")) and rec["metrics"] == {"ran": 1}


def test_run_module_from_cwd_with_console_script(project: Project) -> None:
    """CAP-07: the console script resolves `-m` from the cwd, as `python -m` does."""
    script = Path(sys.executable).parent / "limen"
    if not script.exists():
        pytest.skip("console script not installed")
    project.write("tool.py", "import limen\nlimen.metric('ok', 1)\n")
    out = subprocess.run(
        [str(script), "run", "-m", "tool"],
        cwd=project.root,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert project.last()["metrics"] == {"ok": 1}


def test_parent_package_import_is_recorded(project: Project) -> None:
    """CAP-07: for `-m pkg.sub`, reads in pkg/__init__.py happen while recording."""
    project.write("src/pkg/__init__.py", "import os\nWIKI = os.environ.get('WIKI')\n")
    project.write("src/pkg/sub.py", "import pkg\n")
    project.limen("run", "--treatment", "env:WIKI", "-m", "pkg.sub", env={"PYTHONPATH": "src", "WIKI": "x"}, check=True)
    assert project.last()["env"]["WIKI"] == {"present": True, "value": "x"}
    assert project.limen("check").returncode == 0


# ---- environment -----------------------------------------------------------------------------------


def test_environ_copies_and_early_references(project: Project) -> None:
    """CAP-02/CAP-10: copy/deepcopy work, and `from os import environ` bound early is still observed."""
    project.write(
        "m.py",
        """
        import copy
        from os import environ
        import limen
        with limen.run("early", treatment=["env:WIKI"]):
            copy.copy(environ); copy.deepcopy(environ); copy.deepcopy({"e": environ})
            environ.get("WIKI")
    """,
    )
    subprocess.run([sys.executable, "m.py"], cwd=project.root, env=clean_env(WIKI="rich"), check=True)
    assert project.last()["env"]["WIKI"] == {"present": True, "value": "rich"}


def test_read_then_export_keeps_the_input(project: Project) -> None:
    """CAP-08/S14: reading a variable and then exporting it keeps the external value as an input."""
    project.write("m.py", "import os\nx = os.environ.get('WIKI', 'd')\nos.environ['WIKI'] = os.path.abspath(x)\n")
    project.limen("run", "--treatment", "env:WIKI", "m.py", env={"WIKI": "kb"}, check=True)
    rec = project.last()
    assert rec["env"]["WIKI"]["value"] == "kb" and rec["env"]["WIKI"].get("set_after_read")
    assert fields.identity(rec)["env:WIKI"] == "kb"
    found = codes(check_run(rec))
    assert "TREATMENT_REWRITTEN" in found and "TREATMENT_OVERRIDDEN" not in found


def test_key_scan_does_not_swallow_the_next_read(project: Project) -> None:
    """CAP-09: scanning keys, then reading the first variable, is a real read."""
    project.write(
        "m.py",
        """
        import os
        first = next(iter(os.environ))
        any(k.startswith("SLURM_") for k in os.environ)
        os.environ.get(first)
        open("first.txt", "w").write(first)
    """,
    )
    project.limen("run", "m.py", check=True)
    first = (project.root / "first.txt").read_text()
    assert first in project.last()["env"]


def test_bulk_reads_and_declared_treatment_values(project: Project) -> None:
    """H4/S9: a treatment only seen through a bulk copy is unverified, and PLACEBO when it had the same value."""
    project.write("m.py", "import os\nsettings = dict(os.environ.items())\nx = settings.get('WIKI')\n")
    project.limen("run", "--name", "b", "--arm", "a", "--treatment", "env:WIKI", "m.py", check=True)
    project.limen("run", "--name", "b", "--arm", "b", "--treatment", "env:WIKI", "m.py", env={"WIKI": "r"}, check=True)
    rec = project.last()
    assert rec["env_bulk_read"] and "WIKI" not in rec["env"]
    assert codes(check_run(rec)) & {"TREATMENT_UNVERIFIED"} and "TREATMENT_NOT_READ" not in codes(check_run(rec))
    out = project.limen("compare", "b:a", "b:b")
    assert out.returncode == 0 and "TREATMENT_UNVERIFIED" in out.stdout
    project.limen("run", "--name", "b", "--arm", "c", "--treatment", "env:WIKI", "m.py", env={"WIKI": "r"}, check=True)
    out = project.limen("compare", "b:b", "b:c")
    assert out.returncode == 1 and "PLACEBO" in out.stdout, "same value in both arms"


def test_nested_run_does_not_record_limen_itself(project: Project) -> None:
    """CAP-11: a nested limen.run() must not add Limen's own git call or config reads to the run."""
    project.write("m.py", "import limen\nwith limen.run(treatment=['env:X']):\n    pass\n")
    project.limen("run", "m.py", check=True)
    rec = project.last()
    assert not rec["subprocesses"] and not any(k.startswith("LIMEN_") for k in rec["env"])
    assert "TREATMENT_NOT_READ" in codes(check_run(rec), "block")


# ---- files -----------------------------------------------------------------------------------------


def test_import_time_and_package_data_reads(project: Project) -> None:
    """CAP-04/M2: files read at import time and through pkgutil.get_data are inputs."""
    project.write("src/pkg/conf.json", '{"a": 1}')
    project.write(
        "src/pkg/__init__.py", "import json\nCONF = json.load(open(__file__.replace('__init__.py', 'conf.json')))\n"
    )
    project.write("src/pkg/res.txt", "r")
    project.write("m.py", "import pkg, pkgutil\npkgutil.get_data('pkg', 'res.txt')\n")
    project.limen("run", "m.py", env={"PYTHONPATH": "src"}, check=True)
    reads = project.last()["files"]["read"]
    assert any(p.endswith("conf.json") for p in reads) and any(p.endswith("res.txt") for p in reads)
    assert not any(p.endswith(".py") for p in reads)


def test_code_changed_during_run(project: Project) -> None:
    """CAP-03: the record keeps the bytes that were loaded and flags the change."""
    project.write("src/pkg/model.py", "LR = 0.1\n")
    project.write(
        "m.py",
        """
        import pathlib, pkg.model
        pathlib.Path("src/pkg/model.py").write_text("LR = 0.5\\n")
    """,
    )
    project.commit()
    project.limen("run", "m.py", env={"PYTHONPATH": "src"}, check=True)
    rec = project.last()
    mod = rec["modules"]["pkg.model"]
    assert mod["changed_during_run"] and mod["git"] == "clean"
    obj = project.root / ".limen/objects" / mod["sha256"][:2] / mod["sha256"]
    assert obj.read_text() == "LR = 0.1\n"
    assert "CODE_CHANGED_DURING_RUN" in codes(check_run(rec), "block")


def test_holdout_through_symlink(project: Project, tmp_path_factory: pytest.TempPathFactory) -> None:
    """CAP-05/S3: a held-out file reached through a symlinked data directory is detected."""
    shared = tmp_path_factory.mktemp("shared")
    (shared / "test").mkdir()
    (shared / "test" / "x.jsonl").write_text("{}\n")
    os.symlink(shared, project.root / "data")
    project.write("limen.toml", 'holdout = ["data/test"]\n')
    project.write("train.py", "open('data/test/x.jsonl').read()\n")
    project.limen("run", "--role", "train", "train.py", check=True)
    assert "HOLDOUT_READ" in project.limen("check").stdout


def test_config_comes_from_the_script_not_the_cwd(project: Project, tmp_path_factory: pytest.TempPathFactory) -> None:
    """CAP-18: `cd $SCRATCH && limen run ~/proj/train.py` still applies the project's holdout policy."""
    project.write("limen.toml", 'holdout = ["data/test.csv"]\n')
    project.write("data/test.csv", "id\n1\n")
    project.write("train.py", "import os\nopen(os.path.join(os.path.dirname(__file__), 'data/test.csv')).read()\n")
    scratch = tmp_path_factory.mktemp("scratch")
    out = subprocess.run(
        [sys.executable, "-m", "limen", "run", "--role", "train", str(project.root / "train.py")],
        cwd=scratch,
        env=clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "HOLDOUT_READ" in project.limen("check").stdout


def test_read_write_modes_sqlite_rename_and_output_listing(project: Project) -> None:
    """CAP-14/H3/H1/CAP-12: r+ is a read, sqlite files are inputs, renames and listings of outputs are outputs."""
    project.write("data/a.bin", "abc")
    project.write(
        "m.py",
        """
        import os, sqlite3, glob
        open("data/a.bin", "r+b").read()
        sqlite3.connect("db.sqlite").close()
        os.makedirs("out", exist_ok=True)
        open("out/tmp", "w").write("x")
        os.replace("out/tmp", "out/final")
        glob.glob("out/*")
    """,
    )
    (project.root / "db.sqlite").write_bytes(b"")
    project.limen("run", "m.py", check=True)
    files = project.last()["files"]
    root = str(project.root)
    assert os.path.join(root, "data/a.bin") in files["read"] and os.path.join(root, "data/a.bin") in files["written"]
    assert os.path.join(root, "db.sqlite") in files["read"]
    assert os.path.join(root, "out/final") in files["written"]
    listing = files["read"][os.path.join(root, "out") + os.sep]
    assert listing.get("self_written")
    assert "file:out/" not in fields.identity(project.last())


def test_native_write_then_read_is_not_an_input(project: Project) -> None:
    """H1: a file created during the run by native code and read back is not an input (but is surfaced)."""
    project.write(
        "m.py",
        """
        import os, subprocess
        os.makedirs("ckpt", exist_ok=True)
        subprocess.run(["sh", "-c", "echo weights > ckpt/model.bin"], check=True)
        open("ckpt/model.bin").read()
    """,
    )
    project.limen("run", "m.py", check=True)
    rec = project.last()
    path = os.path.join(str(project.root), "ckpt/model.bin")
    assert rec["files"]["read"][path].get("self_written") and rec["files"]["read"][path].get("by") == "mtime"
    assert "INPUT_CHANGED_DURING_RUN" in codes(check_run(rec))


def test_worker_processes_are_visible(project: Project) -> None:
    """H2: multiprocessing workers started without Popen are listed, and the blind spot is reported."""
    project.write(
        "m.py",
        """
        import multiprocessing as mp
        def f(x):
            return x * 2
        if __name__ == "__main__":
            with mp.get_context("spawn").Pool(1) as p:
                p.map(f, [1, 2])
    """,
    )
    project.limen("run", "m.py", check=True)
    rec = project.last()
    assert rec["subprocesses"], "the spawned worker is recorded"
    assert "CHILD_PROCESSES" in codes(check_run(rec), "warn")


def test_relative_module_file_is_ignored() -> None:
    """L2: modules with a relative __file__ (torch.ops) are not project code."""
    rec = record(modules={})
    assert check_run(rec) == []


# ---- privacy ---------------------------------------------------------------------------------------


def test_secrets_are_digested_everywhere(project: Project) -> None:
    """CAP-19/PRIV-1/PRIV-2: credential values, secret flags, child commands and exception text are digested."""
    project.write(
        "m.py",
        """
        import os, subprocess
        os.environ.get("DATABASE_URL"); os.environ.get("MYSQL_PWD"); os.environ.get("TOKENIZERS_PARALLELISM")
        subprocess.run(["true", "-H", "Authorization: Bearer abcdefgh12345678"])
        raise RuntimeError("login failed for https://admin:hunter2@db.example.com")
    """,
    )
    env = {"DATABASE_URL": "postgresql://admin:hunter2@db/x", "MYSQL_PWD": "hunter2", "TOKENIZERS_PARALLELISM": "false"}
    project.limen("run", "m.py", "--api-key", "sk-abcdefghijklmnop", "--lr", "0.1", env=env)
    rec = project.last()
    text = json.dumps(rec)
    assert "hunter2" not in text and "sk-abcdefghijklmnop" not in text and "abcdefgh12345678" not in text
    assert rec["env"]["TOKENIZERS_PARALLELISM"] == {"present": True, "value": "false"}
    assert rec["argv"][-2:] == ["--lr", "0.1"]


def test_store_is_private(project: Project) -> None:
    """PRIV-3/PRIV-4: owner-only permissions, and a per-store key that never appears in records."""
    project.write("m.py", "pass\n")
    project.limen("run", "m.py", check=True)
    store = project.root / ".limen"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE((store / "key").stat().st_mode) == 0o600
    rec_file = next((store / "runs").glob("*.json"))
    assert stat.S_IMODE(rec_file.stat().st_mode) == 0o600
    assert (store / "key").read_bytes().hex() not in rec_file.read_text()


def test_malformed_records_are_skipped(project: Project) -> None:
    """PRIV-5: one bad file in the store must not break every command."""
    project.write("m.py", "pass\n")
    project.limen("run", "m.py", check=True)
    rid = project.last()["id"]
    (project.root / ".limen/runs/bad.json").write_text('{"schema": "limen.run/1", "id": 5}')
    (project.root / ".limen/runs/deep.json").write_text("[" * 100000 + "]" * 100000)
    out = project.limen("ls")
    assert out.returncode == 0 and rid in out.stdout and "skipped 2" in out.stderr


def test_redaction_rules() -> None:
    assert redact.secret_name("HF_TOKEN") and redact.secret_name("--api-key") and redact.secret_name("DB_PASS")
    assert not redact.secret_name("TOKENIZERS_PARALLELISM") and not redact.secret_name("--max-tokens")
    assert redact.secret_value("postgresql://u:p@h/db") and not redact.secret_value("https://example.com/x.csv")


# ---- comparison semantics ----------------------------------------------------------------------------


def env(**kv: str | None) -> dict[str, dict[str, Any]]:
    return {k: ({"present": False} if v is None else {"present": True, "value": v}) for k, v in kv.items()}


def test_extra_flag_equal_to_treatment_value_is_a_confound() -> None:
    """S1: `--fp16` alongside `--use-rag` is not 'the treatment recorded twice'."""
    a = record(id="a", argv=["m.py"])
    b = record(id="b", argv=["m.py", "--use-rag", "--fp16"])
    comp = compare([a], [b], treatment=["arg:--use-rag"])
    assert {f.subject for f in comp.findings if f.code == "CONFOUND"} == {"arg:--fp16"}


def test_model_name_is_an_input_not_a_label() -> None:
    """S2: `--model-name` differing between arms is a confound even when outputs are named after it."""
    wa, wb = {"/p/results/gpt-4o/r.json": {"sha256": "1"}}, {"/p/results/gpt-4o-mini/r.json": {"sha256": "2"}}
    a = record(id="a", env=env(W=None), argv=["m.py", "--model-name", "gpt-4o"], files={"read": {}, "written": wa})
    b = record(id="b", env=env(W="1"), argv=["m.py", "--model-name", "gpt-4o-mini"], files={"read": {}, "written": wb})
    comp = compare([a], [b], treatment=["env:W"])
    assert "arg:--model-name" in {f.subject for f in comp.findings if f.code == "CONFOUND"}
    assert (
        fields.is_label_key("--run-name") and fields.is_label_key("RUN_ID") and not fields.is_label_key("--model-name")
    )


def test_git_child_does_not_hide_a_placebo() -> None:
    """S4: logging `git rev-parse HEAD` does not downgrade a placebo to a warning."""
    sub = [{"argv": ["git", "rev-parse", "HEAD"], "env": "inherited"}]
    comp = compare([record(id="a", subprocesses=sub)], [record(id="b", subprocesses=sub)], treatment=["env:WIKI"])
    assert "PLACEBO" in codes(comp.findings, "block")


def test_treatment_not_applied_to_every_replicate() -> None:
    """S6: a treatment-arm run that has the control's value mixes the arms."""
    a = [record(id=f"a{i}", env=env(W=None)) for i in range(2)]
    b = [record(id="b0", env=env(W="rich")), record(id="b1", env=env(W=None))]
    assert "TREATMENT_NOT_APPLIED" in codes(compare(a, b, treatment=["env:W"]).findings, "block")


def test_large_files_keep_their_lineage(project: Project) -> None:
    """S7: held-out lineage follows files identified by size and mtime (over the hash cap)."""
    project.write("limen.toml", 'holdout = ["data/test.txt"]\nmax_hash_bytes = 4\n')
    project.write("data/test.txt", "held out items")
    project.write("gen.py", "open('data/test.txt').read()\nopen('mix.txt','w').write('derived from test')\n")
    project.write("train.py", "open('mix.txt').read()\n")
    project.limen("run", "--role", "eval", "gen.py", check=True)
    project.limen("run", "--role", "train", "train.py", check=True)
    assert "HOLDOUT_DERIVED" in codes(check_run(project.last(), Store(project.root / ".limen")), "block")


def test_non_replicate_fields_must_match_in_value_and_mix() -> None:
    """S8: code, data or package versions that vary inside both arms are confounds."""
    a = [record(id=f"a{i}", env=env(W=None), distributions={"torch": v}) for i, v in enumerate(["2.3", "2.4"])]
    b = [record(id=f"b{i}", env=env(W="1"), distributions={"torch": v}) for i, v in enumerate(["2.5", "2.6"])]
    assert "CONFOUND" in codes(compare(a, b, treatment=["env:W"]).findings, "block")
    fa = [{"read": {"/p/d.csv": {"sha256": v}}, "written": {}} for v in ["v1", "v1", "v1", "v2"]]
    fb = [{"read": {"/p/d.csv": {"sha256": v}}, "written": {}} for v in ["v2", "v2", "v2", "v1"]]
    a = [record(id=f"a{i}", env=env(W=None), files=f) for i, f in enumerate(fa)]
    b = [record(id=f"b{i}", env=env(W="1"), files=f) for i, f in enumerate(fb)]
    assert "CONFOUND" in codes(compare(a, b, treatment=["env:W"]).findings, "block")


def test_argv_parsing_keeps_repeats_and_overrides() -> None:
    """S10: repeated flags keep every value; Hydra overrides are named fields."""
    assert fields.parse_argv(["--data", "wiki", "--data", "books"]) == {"arg:--data": '["wiki", "books"]'}
    assert fields.parse_argv(["train", "lr=0.1", "+trainer.fast=true"]) == {
        "arg:#0": "train",
        "arg:lr": "0.1",
        "arg:trainer.fast": "true",
    }


def test_copies_do_not_taint_retroactively(project: Project) -> None:
    """S11: an eval run copying a config does not taint training runs; copying the holdout still does."""
    project.write("limen.toml", 'holdout = ["data/test.jsonl"]\n')
    project.write("data/test.jsonl", '{"id": 1}\n')
    project.write("configs/base.yaml", "lr: 0.1\n")
    project.write("train.py", "open('configs/base.yaml').read()\n")
    project.write(
        "evaluate.py",
        """
        import shutil
        open('data/test.jsonl').read()
        shutil.copy('configs/base.yaml', 'results_config.yaml')
        shutil.copy('data/test.jsonl', 'results_test_copy.jsonl')
    """,
    )
    project.write("train2.py", "open('results_test_copy.jsonl').read()\n")
    project.limen("run", "--role", "train", "train.py", check=True)
    first = project.last()
    project.limen("run", "--role", "eval", "evaluate.py", check=True)
    store = Store(project.root / ".limen")
    assert "HOLDOUT_DERIVED" not in codes(check_run(first, store))
    project.limen("run", "--role", "train", "train.py", check=True)
    assert "HOLDOUT_DERIVED" not in codes(check_run(project.last(), store))
    project.limen("run", "--role", "train", "train2.py", check=True)
    assert "HOLDOUT_DERIVED" in codes(check_run(project.last(), store))


def test_statistics() -> None:
    """S12: the t quantile is conservative for fractional df; few items are flagged."""
    assert _t975(1.02) == 12.706 and _t975(2.5) == 4.303 and _t975(200) == 1.980
    a = record(id="a", outcomes={"q0": {"status": "fail"}})
    b = record(id="b", outcomes={"q0": {"status": "pass"}})
    assert "FEW_ITEMS" in codes(compare([a], [b], treatment=[]).findings, "warn")


def test_partial_replicate_is_items_differ() -> None:
    """S13: a replicate that scored only part of the items is caught even when unions match."""
    full = {f"q{i}": {"status": "pass"} for i in range(6)}
    half = {f"q{i}": {"status": "pass"} for i in range(3)}
    a = [record(id=f"a{i}", outcomes=full) for i in range(2)]
    b = [record(id="b0", outcomes=full), record(id="b1", outcomes=half)]
    assert "ITEMS_DIFFER" in codes(compare(a, b, treatment=[]).findings, "block")


def test_launcher_variables_are_waived() -> None:
    """H5: MASTER_PORT and Slurm job ids differ per job and are not confounds."""
    a = record(id="a", env=env(W=None, MASTER_PORT="29500", SLURM_JOB_ID="1"))
    b = record(id="b", env=env(W="1", MASTER_PORT="29511", SLURM_JOB_ID="2"))
    assert not codes(compare([a], [b], treatment=["env:W"]).findings, "block")


def test_leak_formats(tmp_path: Path) -> None:
    """S15: object containers, BOM CSVs and float ids."""
    (tmp_path / "h.json").write_text(json.dumps({"examples": [{"id": 7}, {"id": 8}]}))
    (tmp_path / "t.csv").write_text("﻿id,x\n7.0,1\n9,2\n", encoding="utf-8")
    held = read_ids(tmp_path / "h.json", "id")
    assert held == {"7", "8"} and held & read_ids(tmp_path / "t.csv", "id") == {"7"}
    (tmp_path / "bad.json").write_text(json.dumps({"a": [1], "b": [2]}))
    with pytest.raises(ValueError):
        read_ids(tmp_path / "bad.json", "id")
    with pytest.raises(ValueError, match="missing column"):
        read_ids(tmp_path / "t.csv", "pid")


# ---- second review round ------------------------------------------------------------------------------


def test_library_env_scan_does_not_disable_placebo_detection(
    project: Project, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """NEW-CAP-01: a library's prefix scan of os.environ (tqdm does this) is not a possible treatment read."""
    lib = tmp_path_factory.mktemp("lib")
    (lib / "libscan.py").write_text(
        "import os\nCONF = {k: v for k, v in os.environ.items() if k.startswith('LIBSCAN_')}\n"
    )
    project.write("m.py", "import libscan, os\nos.environ.get('WIKI_ROTO')\n")
    project.limen(
        "run", "--treatment", "env:WIKI_ROOT", "m.py", env={"PYTHONPATH": str(lib), "WIKI_ROOT": "x"}, check=True
    )
    rec = project.last()
    assert not rec["env_bulk_read"] and rec["env_bulk_libraries"] == ["libscan"]
    assert "TREATMENT_NOT_READ" in codes(check_run(rec), "block")


@POSIX
def test_signal_after_script_returned_exits_with_the_signal(project: Project) -> None:
    """NEW-CAP-02: SIGTERM while non-daemon threads finish is recorded as killed and exits 143."""
    project.write(
        "m.py",
        """
        import threading, time
        def late():
            open('started', 'w').close()
            time.sleep(5)
        threading.Thread(target=late).start()
    """,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "limen", "run", "m.py"],
        cwd=project.root,
        env=clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for(project.root / "started")
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 143 and b"Traceback" not in err
    assert project.last()["status"] == "killed"


@POSIX
def test_chained_sigterm_handler_decides(project: Project) -> None:
    """C1: a handler installed by the script that chains to Limen's (Lightning) keeps a graceful stop graceful."""
    project.write(
        "m.py",
        """
        import os, signal, time
        prev = signal.getsignal(signal.SIGTERM)
        def graceful(s, f):
            print("graceful")
            if callable(prev):
                prev(s, f)
        signal.signal(signal.SIGTERM, graceful)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.2)
    """,
    )
    out = project.limen("run", "m.py")
    assert out.returncode == 0 and "graceful" in out.stdout
    rec = project.last()
    assert rec["status"] == "ok" and rec["signals_received"] == ["SIGTERM"]


def test_results_database_appended_by_runs_is_not_an_input(project: Project) -> None:
    """NEW-CAP-03/N3: a shared sqlite log or results file each run appends to does not become a confound."""
    project.write(
        "m.py",
        """
        import json, os, sqlite3
        lr = os.environ.get("LR", "0.1")
        con = sqlite3.connect("results.db")
        con.execute("create table if not exists r (lr text)")
        con.execute("insert into r values (?)", (lr,))
        con.commit(); con.close()
        s = json.load(open("summary.json")) if os.path.exists("summary.json") else []
        s.append(lr)
        json.dump(s, open("summary.json", "w"))
    """,
    )
    for _ in range(2):
        project.limen(
            "run", "-q", "--name", "db", "--arm", "a", "--treatment", "env:LR", "m.py", env={"LR": "0.1"}, check=True
        )
        project.limen(
            "run", "-q", "--name", "db", "--arm", "b", "--treatment", "env:LR", "m.py", env={"LR": "0.2"}, check=True
        )
    out = project.limen("compare", "db:a", "db:b")
    assert out.returncode == 0, out.stdout


def test_environ_copies_are_not_the_process_environment(project: Project) -> None:
    """NEW-CAP-04: setting a key on a deepcopy of os.environ does not mark the real variable set by the run."""
    project.write("m.py", "import copy, os\nc = copy.deepcopy(os.environ)\nc['SEED'] = '999'\nos.environ.get('SEED')\n")
    project.limen("run", "--treatment", "env:SEED", "m.py", env={"SEED": "5"}, check=True)
    rec = project.last()
    assert rec["env"]["SEED"] == {"present": True, "value": "5"} and project.limen("check").returncode == 0


def test_env_scans_survive_declarations_and_threads(project: Project) -> None:
    """NEW-CAP-05/06: a scan then a read is kept across a nested limen.run and inside a worker thread."""
    project.write(
        "m.py",
        """
        import os, threading, limen
        first = next(iter(os.environ))
        any(k.startswith("SLURM_") for k in os.environ)
        os.environ.get(first)
        with limen.run(treatment=["env:MODE"]):
            os.environ.get("MODE")
        def worker():
            any(k.startswith("X_") for k in os.environ)
            os.environ.get(first)
        t = threading.Thread(target=worker); t.start(); t.join()
        open("first.txt", "w").write(first)
    """,
    )
    project.limen("run", "m.py", check=True)
    assert (project.root / "first.txt").read_text() in project.last()["env"]


def test_symlinked_script_imports_its_siblings(project: Project) -> None:
    """NEW-CAP-07: sys.path[0] is the directory of the resolved script, as with python."""
    project.write("code/helper.py", "X = 42\n")
    project.write("code/train.py", "import helper\nprint(helper.X)\n")
    (project.root / "runs").mkdir()
    os.symlink(project.root / "code/train.py", project.root / "runs/train.py")
    out = project.limen("run", "runs/train.py", check=True)
    assert "42" in out.stdout


def test_main_stays_main_for_threads_and_atexit(project: Project) -> None:
    """NEW-CAP-08: objects of classes defined in the script can still be pickled after it returns."""
    project.write(
        "m.py",
        """
        import atexit, pickle, threading, time
        class Model:
            pass
        def save():
            time.sleep(0.2)
            pickle.dumps(Model()); open("thread.ok", "w").close()
        threading.Thread(target=save).start()
        atexit.register(lambda: (pickle.dumps(Model()), open("atexit.ok", "w").close()))
    """,
    )
    project.limen("run", "m.py", check=True)
    assert (project.root / "thread.ok").exists() and (project.root / "atexit.ok").exists()


def test_module_edited_after_import_before_run(project: Project) -> None:
    """NEW-CAP-09: with limen.run() in a long-lived process, a module edited after import is flagged."""
    project.write("src/pkg/model.py", "LR = 0.1\n")
    project.write(
        "m.py",
        """
        import os, pathlib, time, limen
        import pkg.model
        time.sleep(1.1)
        pathlib.Path("src/pkg/model.py").write_text("LR = 0.55555\\n")
        with limen.run("nb"):
            pass
    """,
    )
    subprocess.run([sys.executable, "m.py"], cwd=project.root, env=clean_env(PYTHONPATH="src"), check=True)
    rec = project.last()
    mod = rec["modules"]["pkg.model"]
    found = codes(check_run(rec))
    if mod.get("changed_before_run"):  # a bytecode cache proves the loaded code differs from the file
        assert "CODE_CHANGED_DURING_RUN" in found
    else:  # no bytecode cache (PYTHONDONTWRITEBYTECODE): the edit after process start is still surfaced
        assert mod.get("edited_before_run") and "CODE_EDITED_BEFORE_RUN" in found


def test_unprintable_values_do_not_crash(project: Project) -> None:
    """NEW-CAP-10."""
    project.write(
        "m.py",
        """
        import limen
        class Weird:
            def __str__(self): raise RuntimeError("no str")
            __repr__ = __str__
        limen.param("w", Weird())
    """,
    )
    project.limen("run", "m.py", check=True)
    assert project.last()["params"]["w"] == "<unrepresentable Weird>"


def test_compiler_cache_modules_are_generated_code(project: Project, tmp_path_factory: pytest.TempPathFactory) -> None:
    """COMPAT-N1: modules loaded from compiler caches are not code identity."""
    cache = tmp_path_factory.mktemp("inductor")
    project.write(
        "m.py",
        """
        import importlib.util, os
        d = os.environ["TORCHINDUCTOR_CACHE_DIR"]
        p = os.path.join(d, "kernel_abc.py")
        open(p, "w").write("K = 1\\n")
        spec = importlib.util.spec_from_file_location("kernel_abc", p)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        import sys; sys.modules["kernel_abc"] = m
    """,
    )
    project.limen("run", "m.py", env={"TORCHINDUCTOR_CACHE_DIR": str(cache)}, check=True)
    rec = project.last()
    assert "kernel_abc" not in rec["modules"] and "kernel_abc" in rec["generated_code"]
    assert not codes(check_run(rec), "block")


def test_native_writes_into_existing_output_dirs(project: Project) -> None:
    """H1: per-arm output directories that already exist are still recognized as outputs."""
    project.write(
        "m.py",
        """
        import os, subprocess, sys
        out = sys.argv[sys.argv.index("--out") + 1]
        subprocess.run(["sh", "-c", f"echo {os.environ.get('SCALE')} > {out}/model.bin"], check=True)
    """,
    )
    for arm in ("A", "B"):
        (project.root / "out" / arm).mkdir(parents=True)
    project.limen(
        "run",
        "-q",
        "--name",
        "o",
        "--arm",
        "A",
        "--treatment",
        "env:SCALE",
        "m.py",
        "--out",
        "out/A",
        env={"SCALE": "1"},
        check=True,
    )
    project.limen(
        "run",
        "-q",
        "--name",
        "o",
        "--arm",
        "B",
        "--treatment",
        "env:SCALE",
        "m.py",
        "--out",
        "out/B",
        env={"SCALE": "2"},
        check=True,
    )
    out = project.limen("compare", "o:A", "o:B")
    assert out.returncode == 0 and "CONFOUND" not in out.stdout, out.stdout


def test_children_with_explicit_env_are_reported(project: Project) -> None:
    """H2: a child started with a copied environment is still a blind spot worth reporting."""
    project.write(
        "m.py", "import os, subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'], env=dict(os.environ))\n"
    )
    project.limen("run", "m.py", check=True)
    assert "CHILD_PROCESSES" in codes(check_run(project.last()), "warn")


def test_bulk_only_variables_that_differ_are_possible_confounds(project: Project) -> None:
    """H4: a variable a settings loader read in bulk and that differs between arms is surfaced."""
    project.write("m.py", "import os, sys\nsettings = dict(os.environ)\nlr = sys.argv[-1]\n")
    project.limen(
        "run",
        "-q",
        "--name",
        "bk",
        "--arm",
        "a",
        "--treatment",
        "arg:--lr",
        "m.py",
        "--lr",
        "0.1",
        env={"L2": "0.0"},
        check=True,
    )
    project.limen(
        "run",
        "-q",
        "--name",
        "bk",
        "--arm",
        "b",
        "--treatment",
        "arg:--lr",
        "m.py",
        "--lr",
        "0.3",
        env={"L2": "0.5"},
        check=True,
    )
    out = project.limen("compare", "bk:a", "bk:b", "-v")
    assert "POSSIBLE_CONFOUND" in out.stdout and "env:L2" in out.stdout


def test_rules_from_second_review() -> None:
    """S2 (checkpoint is an input), S4 (redirected git), N1 (mixed arms via declared values), N4 (disjoint knobs),
    N5 (compilers read env), N6 (untracked env)."""
    assert not fields.is_label_key("--checkpoint") and fields.is_label_key("--run-name") and fields.is_label_key("ARM")
    sub = [{"argv": ["/bin/sh", "-c", "git rev-parse HEAD 2>/dev/null"], "env": "inherited"}]
    assert "PLACEBO" in codes(
        compare(
            [record(id="a", subprocesses=sub)], [record(id="b", subprocesses=sub)], treatment=["env:WIKI"]
        ).findings,
        "block",
    )

    def denv(v: str | None) -> dict[str, Any]:
        return {"WIKI": {"present": False} if v is None else {"present": True, "value": v}}

    bulk = {"env_bulk_read": True, "env_bulk_by": ["pydantic_settings"]}
    a = [record(id=f"a{i}", declared_env=denv(None), **bulk) for i in range(2)]
    b = [record(id="b0", declared_env=denv("rich"), **bulk), record(id="b1", declared_env=denv(None), **bulk)]
    assert "TREATMENT_NOT_APPLIED" in codes(compare(a, b, treatment=["env:WIKI"]).findings, "block")

    ka = [record(id=f"a{i}", argv=["m.py", "--seed", str(i), "--lr", lr]) for i, lr in enumerate(["0.001", "0.002"])]
    kb = [
        record(id=f"b{i}", argv=["m.py", "--seed", str(i + 5), "--lr", lr], env=env(W="1"))
        for i, lr in enumerate(["0.01", "0.02"])
    ]
    comp = compare(ka, kb, treatment=["env:W"])
    assert {f.subject for f in comp.findings if f.code == "CONFOUND"} == {"arg:--lr"}

    gcc = [{"argv": ["gcc", "-E", "k.c"], "env": "inherited"}]
    comp = compare([record(id="a", subprocesses=gcc)], [record(id="b", subprocesses=gcc)], treatment=["env:CPATH"])
    assert "TREATMENT_UNVERIFIED" in codes(comp.findings, "warn") and "PLACEBO" not in codes(comp.findings)

    ua = record(id="a", env_tracked=False, declared_env=denv(None))
    ub = record(id="b", env_tracked=False, declared_env=denv("x"))
    assert "PLACEBO" not in codes(compare([ua], [ub], treatment=["env:WIKI"]).findings)


def test_lineage_ignores_empty_and_independently_produced_content(project: Project) -> None:
    """N2: an empty file, or bytes a clean run also produced, carry no held-out data."""
    project.write("limen.toml", 'holdout = ["data/test.jsonl"]\n')
    project.write("data/test.jsonl", '{"id": 1}\n')
    project.write("data/train.jsonl", '{"id": 2}\n')
    project.write("configs/empty.yaml", "")
    project.write("prep.py", "v = open('data/train.jsonl').read()\nopen('vocab.txt', 'w').write('vocab:' + v)\n")
    project.write(
        "evaluate.py",
        """
        open('data/test.jsonl').read()
        open('errors.log', 'w').close()
        v = open('data/train.jsonl').read()
        open('vocab2.txt', 'w').write('vocab:' + v)
    """,
    )
    project.write("train.py", "open('configs/empty.yaml').read()\nopen('vocab.txt').read()\n")
    project.limen("run", "--role", "generate", "prep.py", check=True)
    project.limen("run", "--role", "eval", "evaluate.py", check=True)
    project.limen("run", "--role", "train", "train.py", check=True)
    assert "HOLDOUT_DERIVED" not in codes(check_run(project.last(), Store(project.root / ".limen")))


def test_redaction_second_round() -> None:
    """NEW-PRIV-1/2/3: compound names, more credential shapes, no damage to ordinary paths."""
    k = b"k" * 32
    for n in ("PGPASSWORD", "NGROK_AUTHTOKEN", "--dbPassword", "--hfToken"):
        assert redact.secret_name(n), n
    for n in ("pad_token_id", "KEY_FIELD", "--sort-key"):
        assert not redact.secret_name(n), n
    assert "hunter2" not in redact.text(k, "redis://:hunter2@cache:6379/0")
    assert "hunter2" not in " ".join(redact.argv(k, ["docker", "run", "-e", "DB_PASSWORD=hunter2", "img"]))
    assert "hunter2" not in " ".join(redact.argv(k, ["curl", "-u", "admin:hunter2", "https://x"]))
    assert "hunter2" not in " ".join(redact.argv(k, ["mysql", "-uroot", "-phunter2"]))
    assert "abcdef0123456789" not in redact.text(k, "api-key: abcdef0123456789abcdef")
    assert redact.text(k, "out/hf_finetune_a") == "out/hf_finetune_a"


def test_key_creation_is_atomic(tmp_path: Path) -> None:
    """NEW-PRIV-4: concurrent first runs all see the same full key."""
    code = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
        f"from limen.store import Store\nprint(Store({str(tmp_path / '.limen')!r}).key().hex())"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE) for _ in range(8)]
    keys = {p.communicate(timeout=60)[0].strip() for p in procs}
    assert len(keys) == 1 and len(next(iter(keys))) == 64


def test_secret_params_are_digested(project: Project) -> None:
    """NEW-PRIV-5."""
    project.write("m.py", "import limen\nlimen.param('db_password', 'hunter2-param')\nlimen.param('lr', 0.1)\n")
    project.limen("run", "m.py", check=True)
    params = project.last()["params"]
    assert params["db_password"].startswith("hmac:") and params["lr"] == 0.1


def test_malformed_fields_are_skipped(project: Project) -> None:
    """NEW-PRIV-6: records with wrongly typed fields are skipped, not fatal."""
    project.write("m.py", "pass\n")
    project.limen("run", "m.py", check=True)
    good = project.last()
    for i, patch in enumerate(
        [
            {"started_at": 5},
            {"argv": "notalist"},
            {"env": {"X": "str"}},
            {"files": {"read": [1]}},
            {"declared": {"name": 5}},
        ]
    ):
        bad = {**good, "id": f"zzbad{i}", **patch}
        (project.root / f".limen/runs/zzbad{i}.json").write_text(json.dumps(bad))
    out = project.limen("ls")
    assert out.returncode == 0 and "skipped 5" in out.stderr
    assert project.limen("check", "latest").returncode == 0
