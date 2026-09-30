"""Valid: both arms share an on-disk verdict cache, but it is keyed on everything that determines a verdict."""
import subprocess
from pathlib import Path

LOOP = "evalgate"
KIND = "valid"
FAULT_CLASS = "none (shared verdict cache, correctly keyed)"
DESCRIPTION = (
    "To speed up re-runs the judge stores verdicts in .cache/judge/verdicts.json, shared across runs and "
    "arms. The key hashes the candidate source, the task, strict/time_limit_s and the judge's own code and "
    "test table, and timeouts are never cached. A's runs fill the cache; B's five candidates that are "
    "byte-identical to v1 are answered from A's entries, which are exactly what re-running them would give."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 2
CLAIM_METRIC = "pass_rate"

RUNNER = '''import hashlib
import json
import os
import time

import limen

from .gate import correct

CACHE_PATH = os.path.join(".cache", "judge", "verdicts.json")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _judge_fingerprint():
    """Hash of the judge's own code and test table: changing either invalidates every cached verdict."""
    h = hashlib.sha256()
    for name in sorted(os.listdir(_HERE)):
        if name.endswith(".py"):
            with open(os.path.join(_HERE, name), "rb") as f:
                h.update(name.encode() + b"\\0" + f.read())
    return h.hexdigest()


def _cache_key(fingerprint, cfg, task, source):
    spec = json.dumps({"judge": fingerprint, "strict": cfg["strict"],
                       "time_limit_s": cfg["time_limit_s"], "task": task}, sort_keys=True)
    return hashlib.sha256((spec + "\\0" + source).encode()).hexdigest()


def _cache_load():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _cache_save(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_PATH)


def _evaluate(task, source, cfg):
    """Run one candidate through the gate; returns (status, detail)."""
    start = time.perf_counter()
    try:
        ok = correct(task, source) if cfg["strict"] else True
    except SyntaxError:
        return "error", "syntax"
    except Exception as e:
        return "error", type(e).__name__
    if time.perf_counter() - start > cfg["time_limit_s"]:
        return "timeout", None
    return ("pass" if ok else "fail"), None


def judge(candidate_dir, config_path):
    with open(config_path) as f:
        cfg = json.load(f)
    limen.param("time_limit_s", cfg["time_limit_s"])
    limen.param("strict", cfg["strict"])
    cache = _cache_load()
    fingerprint = _judge_fingerprint()
    passed = 0
    names = sorted(n for n in os.listdir(candidate_dir) if n.endswith(".py"))
    for name in names:
        task = name[:-3]
        with open(os.path.join(candidate_dir, name)) as f:
            source = f.read()
        key = _cache_key(fingerprint, cfg, task, source)
        if key in cache:
            status, detail = cache[key]
        else:
            status, detail = _evaluate(task, source, cfg)
            if status != "timeout":  # machine-dependent; always re-measure
                cache[key] = [status, detail]
        if detail is None:
            limen.outcome(task, status)
        else:
            limen.outcome(task, status, detail)
        passed += status == "pass"
    _cache_save(cache)
    return passed / len(names) if names else 0.0
'''


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def setup(root):
    root = Path(root)
    (root / "src" / "judge" / "runner.py").write_text(RUNNER)
    (root / ".gitignore").write_text("__pycache__/\n/out/\n/.cache/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "judge: content-addressed verdict cache")


ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"]),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"]),
}
