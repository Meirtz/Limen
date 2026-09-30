"""A verdict cache keyed only by task name makes arm B replay arm A's verdicts."""

import subprocess

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "treatment never measured: stale cached verdicts reused across candidate sets"
DESCRIPTION = (
    "Earlier in the project the agent added a verdict cache to the judge so reruns would be fast. It "
    "is keyed by task name only, not by the candidate's source, and lives in .judge_cache/ "
    "(gitignored). The v1 baseline fills the cache; the v2 runs then read v1's verdicts for every "
    "task, so B reports the baseline's pass rate and the agent concludes v2 is no better, even "
    "though v2 fixes three of the four failing tasks."
)
TREATMENT = ["arg:--candidates", "file:candidates/v2/"]
REPLICATES = 2

_RUNNER_PY = '''import json
import os
import time

import limen

from .gate import correct

# Verdicts are cached across runs so re-evaluations are quick.
_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".judge_cache", "verdicts.json")


def _load_cache():
    try:
        with open(_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
    with open(_CACHE, "w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)


def judge(candidate_dir, config_path):
    with open(config_path) as f:
        cfg = json.load(f)
    limen.param("time_limit_s", cfg["time_limit_s"])
    limen.param("strict", cfg["strict"])
    cache = _load_cache()
    passed = 0
    names = sorted(n for n in os.listdir(candidate_dir) if n.endswith(".py"))
    for name in names:
        task = name[:-3]
        if task in cache:
            verdict = cache[task]
            passed += verdict == "pass"
            limen.outcome(task, verdict)
            continue
        with open(os.path.join(candidate_dir, name)) as f:
            source = f.read()
        start = time.perf_counter()
        try:
            ok = correct(task, source) if cfg["strict"] else True
        except SyntaxError:
            limen.outcome(task, "error", "syntax")
            continue
        except Exception as e:
            limen.outcome(task, "error", type(e).__name__)
            continue
        if time.perf_counter() - start > cfg["time_limit_s"]:
            limen.outcome(task, "timeout")
            continue
        passed += ok
        cache[task] = "pass" if ok else "fail"
        limen.outcome(task, cache[task])
    _save_cache(cache)
    return passed / len(names) if names else 0.0
'''


def setup(root):
    # Project state before the experiment: the cache was added and committed a while ago.
    (root / "src" / "judge" / "runner.py").write_text(_RUNNER_PY)
    with open(root / ".gitignore", "a") as f:
        f.write(".judge_cache/\nout/\n__pycache__/\n")
    git = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.com"]
    subprocess.run(git + ["add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(git + ["commit", "-qm", "perf(judge): cache verdicts between runs"], cwd=root,
                   check=True, capture_output=True)


_PROTOCOL = {"time_limit_s": 0.5, "strict": True}

ARMS = {
    "A": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        role="eval",
        gates=["correct"],
        protocol=_PROTOCOL,
    ),
    "B": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        role="eval",
        gates=["correct"],
        protocol=_PROTOCOL,
    ),
}
