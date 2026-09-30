"""Fault: a content-hash verdict cache whose key omits the test suite serves stale verdicts to both arms."""
import hashlib
import json
import subprocess
from pathlib import Path

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "stale cache: verdicts from an older test suite reused by both arms"
DESCRIPTION = (
    "The judge caches verdicts in .cache/judge/verdicts.json (restored from the shared CI cache) keyed by "
    "sha256(task, candidate source), but the key does not include the tests. The cache was filled when the "
    "suite still had one placeholder case per task; the edge-case tests added since then never run for any "
    "source already in the cache, so A (v1) and the unchanged v2 files are graded by the old suite and "
    "both arms come out at 1.0."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 2
CLAIM_METRIC = "pass_rate"

CACHE_HELPERS = '''
CACHE = os.path.join(".cache", "judge", "verdicts.json")


def _cache_key(task, source):
    return hashlib.sha256(f"{task}\\0{source}".encode()).hexdigest()


def _cache_load():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _cache_save(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
'''

OLD_CHECK = """        start = time.perf_counter()
        try:
            ok = correct(task, source) if cfg["strict"] else True
"""
NEW_CHECK = """        start = time.perf_counter()
        key = _cache_key(task, source)
        try:
            if key in cache:
                ok = cache[key]
            else:
                ok = correct(task, source) if cfg["strict"] else True
                if cfg["strict"]:
                    cache[key] = ok
"""

# The bootstrap suite: one plumbing check per task, before the edge cases were written.
PLACEHOLDER_TESTS = {
    "add": [((0, 0), 0)],
    "rev": [(("",), "")],
    "mx": [(([7],), 7)],
    "fib": [((1,), 1)],
    "cnt": [(("aaa", "a"), 3)],
    "srt": [(([],), [])],
    "pal": [(("",), True)],
    "sq": [((0,), 0)],
}


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def setup(root):
    root = Path(root)
    runner = root / "src" / "judge" / "runner.py"
    src = runner.read_text()
    assert OLD_CHECK in src
    src = src.replace("import json\n", "import hashlib\nimport json\n", 1)
    src = src.replace("from .gate import correct\n", "from .gate import correct\n" + CACHE_HELPERS)
    src = src.replace(OLD_CHECK, NEW_CHECK)
    src = src.replace("    passed = 0\n", "    passed = 0\n    cache = _cache_load()\n")
    src = src.replace("    return passed / len(names)", "    _cache_save(cache)\n    return passed / len(names)")
    runner.write_text(src)
    (root / ".gitignore").write_text("__pycache__/\n/out/\n# restored/saved by CI cache step\n/.cache/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "judge: cache verdicts by candidate content hash")

    # What the shared cache holds: verdicts computed for candidates/v1 back when the suite was the
    # placeholder one (the judge's check, run against those tests).
    cache = {}
    for f in sorted((root / "candidates" / "v1").glob("*.py")):
        task, source = f.stem, f.read_text()
        ns = {}
        exec(compile(source, f"<candidate:{task}>", "exec"), ns)
        fn = ns[task]
        ok = all(fn(*args) == want for args, want in PLACEHOLDER_TESTS[task])
        cache[hashlib.sha256(f"{task}\0{source}".encode()).hexdigest()] = ok
    (root / ".cache" / "judge").mkdir(parents=True)
    (root / ".cache" / "judge" / "verdicts.json").write_text(json.dumps(cache, indent=1, sort_keys=True))


ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"]),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"]),
}
