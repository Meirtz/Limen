"""Fault-injection benchmark for Limen.

Each scenario copies one experiment loop into a fresh git repository, optionally injects a
fault, runs arm A (control) and arm B (treatment) under ``limen run``, and records the claim a
naive experimenter would make (B beats A on the claim metric). Detectors then judge the same
runs *before* that claim is accepted:

- ``none``      never objects (what happens today by default);
- ``mlflow``    what an MLflow-style tracker records: git commit and dirty flag, command-line
                parameters, logged parameters, declared protocol, package versions, Python, and
                run status; it objects when any of those differ outside the declared treatment,
                when the treatment is not among the differences, or when a run did not finish;
- ``sacred``    ``mlflow`` plus what a Sacred-style observer adds: hashes of the imported local
                source files and host information;
- ``limen``     :func:`limen.compare.compare` with run checks; objects on any blocking finding.

The baselines are given the same declared treatment and the same difference rule as Limen, so
the comparison isolates what each one can *see*, not how cleverly it is configured.

A scenario is a Python file with::

    LOOP = "train" | "evalgate"
    KIND = "fault" | "valid"
    FAULT_CLASS = "..."          # free text for faults, "valid" otherwise
    DESCRIPTION = "..."
    TREATMENT = ["env:TRAINER_FEATURES"]
    ARMS = {"A": dict(cmd=[...], env={...}), "B": dict(...)}  # optional keys: role, gates, protocol, before
    REPLICATES = 2               # optional
    CLAIM_METRIC = "val_acc"     # optional; default per loop
    def setup(root): ...          # optional, runs once after the copy is committed
    def select(runs_a, runs_b): ...  # optional: the runs the experimenter chooses to compare

``cmd`` items may contain ``{seed}`` and ``{arm}``; ``before(root, arm)`` runs before each
arm's first run (to simulate edits made mid-experiment).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))

from limen import fields  # noqa: E402
from limen.compare import compare, restates_treatment  # noqa: E402
from limen.store import Store  # noqa: E402

DEFAULT_METRIC = {"train": "val_acc", "evalgate": "pass_rate"}
GIT_ENV = {
    "GIT_AUTHOR_NAME": "b",
    "GIT_AUTHOR_EMAIL": "b@example.com",
    "GIT_COMMITTER_NAME": "b",
    "GIT_COMMITTER_EMAIL": "b@example.com",
}


def load_scenario(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"scenario_{path.parent.name}_{path.stem}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("LOOP", "KIND", "FAULT_CLASS", "TREATMENT", "ARMS"):
        if not hasattr(mod, attr):
            raise ValueError(f"{path}: missing {attr}")
    return mod


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def _base_env(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LIMEN_", "TRAINER_", "JUDGE_", "PYTHON"))}
    env.update(GIT_ENV)
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(REPO / "src")])
    return env


def run_scenario(path: Path, workdir: Path) -> dict[str, Any]:
    sc = load_scenario(path)
    root = workdir / path.stem
    shutil.copytree(HERE / "loops" / sc.LOOP, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "loop")
    if hasattr(sc, "setup"):
        sc.setup(root)

    replicates = getattr(sc, "REPLICATES", 2)
    ids: dict[str, list[str]] = {"A": [], "B": []}
    errors: list[str] = []
    for arm in ("A", "B"):
        spec = sc.ARMS[arm]
        if spec.get("before"):
            spec["before"](root, arm)
        for seed in range(replicates):
            if spec.get("before_each"):
                spec["before_each"](root, arm, seed)
            cmd = [c.format(seed=seed, arm=arm) for c in spec["cmd"]]
            args = [sys.executable, "-m", "limen", "run", "-q", "--name", path.stem, "--arm", arm]
            for t in sc.TREATMENT:
                args += ["--treatment", t]
            if spec.get("role"):
                args += ["--role", spec["role"]]
            for g in spec.get("gates", []):
                args += ["--gate", g]
            for k, v in spec.get("protocol", {}).items():
                args += ["--protocol", f"{k}={json.dumps(v)}"]
            env = _base_env(root)
            env.update({k: v.format(seed=seed, arm=arm) for k, v in spec.get("env", {}).items()})
            before = set(Store(root / ".limen").runs.glob("*.json")) if (root / ".limen/runs").is_dir() else set()
            timeout = spec.get("timeout", 120)
            try:
                proc = subprocess.run([*args, *cmd], cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
                if proc.returncode != 0:
                    errors.append(f"{arm}{seed}: exit {proc.returncode}: {proc.stderr[-300:]}")
            except subprocess.TimeoutExpired:
                errors.append(f"{arm}{seed}: harness timeout")
            after = set((root / ".limen/runs").glob("*.json")) if (root / ".limen/runs").is_dir() else set()
            new = sorted(after - before)
            if new:
                ids[arm].append(new[-1].stem)

    store = Store(root / ".limen")
    runs = {r["id"]: r for r in store.all()}
    runs_a = [runs[i] for i in ids["A"] if i in runs]
    runs_b = [runs[i] for i in ids["B"] if i in runs]
    if hasattr(sc, "select"):
        runs_a, runs_b = sc.select(runs_a, runs_b)

    metric = getattr(sc, "CLAIM_METRIC", DEFAULT_METRIC[sc.LOOP])
    ma = [r.get("metrics", {}).get(metric) for r in runs_a]
    mb = [r.get("metrics", {}).get(metric) for r in runs_b]
    va, vb = [x for x in ma if isinstance(x, (int, float))], [x for x in mb if isinstance(x, (int, float))]
    claim = bool(va and vb and sum(vb) / len(vb) > sum(va) / len(va))

    comp = compare(runs_a, runs_b, treatment=list(sc.TREATMENT), waive=list(getattr(sc, "WAIVE", [])), store=store)
    limen_block = sorted({f.code for f in comp.findings if f.severity == "block"})
    limen_warn = sorted({f.code for f in comp.findings if f.severity == "warn"})
    return {
        "scenario": path.stem,
        "set": path.parent.parent.name if path.parent.name != "scenarios" else path.parent.name,
        "loop": sc.LOOP,
        "kind": sc.KIND,
        "fault_class": sc.FAULT_CLASS,
        "description": getattr(sc, "DESCRIPTION", ""),
        "runs": [len(runs_a), len(runs_b)],
        "errors": errors,
        "metric": metric,
        "a": va,
        "b": vb,
        "claim_b_better": claim,
        "detectors": {
            "none": False,
            "mlflow": baseline(runs_a, runs_b, sc.TREATMENT, getattr(sc, "WAIVE", []), sources=False),
            "sacred": baseline(runs_a, runs_b, sc.TREATMENT, getattr(sc, "WAIVE", []), sources=True),
            "limen": bool(limen_block),
        },
        "limen_block": limen_block,
        "limen_warn": limen_warn,
    }


def _baseline_fields(r: dict[str, Any], sources: bool) -> dict[str, str]:
    """The fields an MLflow-style (and optionally Sacred-style) tracker would have logged."""
    out: dict[str, str] = {}
    for top, st in (r.get("git_at_start") or {}).items():  # MLflow tags the commit; Sacred also records dirty
        out[f"git:{top}"] = f"{st.get('head')}{'+dirty' if sources and st.get('dirty') else ''}"
    written, read = fields._output_values(r)
    for k, v in fields.parse_argv(list(r.get("argv") or [])[1:]).items():
        if not fields._is_output(r, k[len("arg:") :], v, written, read):
            out[k] = v
    for k, v in (r.get("params") or {}).items():
        out[f"param:{k}"] = json.dumps(v, sort_keys=True)
    for k, v in ((r.get("declared") or {}).get("protocol") or {}).items():
        out[f"protocol:{k}"] = json.dumps(v, sort_keys=True)
    for d, v in (r.get("distributions") or {}).items():
        out[f"dist:{d}"] = str(v)
    out["python"] = str((r.get("python") or {}).get("version"))
    if sources:
        for name, m in (r.get("modules") or {}).items():
            out[f"module:{name}"] = str(m.get("sha256"))
        if r.get("main"):
            out["module:__main__"] = str(r["main"].get("sha256"))
        plat = r.get("platform") or {}
        out["platform"] = f"{plat.get('system')}-{plat.get('machine')}"
        out["gpu"] = ", ".join(plat.get("gpus") or []) or "none"
    return out


def baseline(
    runs_a: list[dict[str, Any]], runs_b: list[dict[str, Any]], treatment: list[str], waive: list[str], *, sources: bool
) -> bool:
    if not runs_a or not runs_b:
        return True
    if any(r.get("status") != "ok" for r in runs_a + runs_b):
        return True
    fa = [_baseline_fields(r, sources) for r in runs_a]
    fb = [_baseline_fields(r, sources) for r in runs_b]
    keys = set().union(*fa, *fb)

    def vals(fs: list[dict[str, str]], k: str) -> set[str]:
        return {f.get(k, "<absent>") for f in fs}

    for spec in treatment:
        matched = [k for k in keys if fields.matches(spec, k)]
        if matched and all(vals(fa, k) == vals(fb, k) for k in matched):
            return True  # the logged treatment is identical: placebo visible to the tracker
    for k in keys:
        if any(fields.matches(s, k) for s in treatment) or any(fields.matches(w, k) for w in waive):
            continue
        sa, sb = vals(fa, k), vals(fb, k)
        if sa != sb and not (len(sa) > 1 and len(sb) > 1) and not restates_treatment(k, fa + fb, treatment):
            return True
    return False


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    detectors = ["none", "mlflow", "sacred", "limen"]
    out: dict[str, Any] = {}
    for subset in [*sorted({r["set"] for r in rows}), "all"]:
        rs = [r for r in rows if subset == "all" or r["set"] == subset]
        faults = [r for r in rs if r["kind"] == "fault"]
        valid = [r for r in rs if r["kind"] == "valid"]
        out[subset] = {
            "faults": len(faults),
            "valid": len(valid),
            "detected": {d: sum(r["detectors"][d] for r in faults) for d in detectors},
            "false_alarms": {d: sum(r["detectors"][d] for r in valid) for d in detectors},
            "limen_only": sorted(
                r["scenario"]
                for r in faults
                if r["detectors"]["limen"] and not r["detectors"]["sacred"] and not r["detectors"]["mlflow"]
            ),
            "missed_by_limen": sorted(r["scenario"] for r in faults if not r["detectors"]["limen"]),
            "limen_false_alarms": sorted(r["scenario"] for r in valid if r["detectors"]["limen"]),
        }
    return out


def table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| set | scenario | kind | class | claim B>A | mlflow | sacred | limen | limen blocking findings |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: (r["set"], r["kind"], r["scenario"])):
        d = r["detectors"]
        mark = {True: "flag", False: "-"}
        lines.append(
            f"| {r['set']} | {r['scenario']} | {r['kind']} | {r['fault_class']} | "
            f"{'yes' if r['claim_b_better'] else 'no'} | {mark[d['mlflow']]} | {mark[d['sacred']]} | "
            f"{mark[d['limen']]} | {', '.join(r['limen_block']) or '-'} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("sets", nargs="*", default=["dev", "heldout"], help="scenario sets under bench/scenarios/")
    ap.add_argument("--only", help="run scenarios whose name contains this")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--keep", action="store_true", help="keep the scenario work trees")
    a = ap.parse_args()
    paths = []
    for s in a.sets:
        paths += sorted((HERE / "scenarios" / s).glob("*.py"))
    paths = [p for p in paths if not p.name.startswith("_") and (not a.only or a.only in p.stem)]
    work = Path(tempfile.mkdtemp(prefix="limen-bench-"))
    rows = []
    t0 = time.time()
    for p in paths:
        t = time.time()
        row = run_scenario(p, work / p.parent.name)
        row["set"] = p.parent.name
        row["seconds"] = round(time.time() - t, 1)
        rows.append(row)
        d = row["detectors"]
        print(
            f"{row['set']:8} {row['kind']:5} {row['scenario']:40} claim={'Y' if row['claim_b_better'] else 'n'} "
            f"mlflow={int(d['mlflow'])} sacred={int(d['sacred'])} limen={int(d['limen'])} "
            f"{','.join(row['limen_block'])}{' ERR' if row['errors'] else ''}",
            flush=True,
        )
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows)
    (out / "results.json").write_text(json.dumps({"rows": rows, "summary": summary}, indent=1, sort_keys=True) + "\n")
    (out / "results.md").write_text(table(rows) + "\n")
    print(json.dumps(summary, indent=1))
    print(f"{len(rows)} scenarios in {time.time() - t0:.0f}s; work trees in {work if a.keep else '(removed)'}")
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
