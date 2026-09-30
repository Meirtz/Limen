"""New prompt's candidate set is missing the tasks whose generations failed to parse."""

import json
import subprocess

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "arms scored on different item sets: failed generations silently dropped from B"
DESCRIPTION = (
    "The agent regenerated solutions with a new 'terse' prompt into candidates/v3. For cnt and srt the "
    "model's reply had no parsable code block, so the generation script logged a warning and wrote no "
    "file. The judge divides by the number of .py files it finds, so B is scored on 6 tasks and A on 8; "
    "on the 6 shared tasks the two candidate sets behave the same, and all of B's gain comes from the "
    "two missing tasks."
)
TREATMENT = ["arg:--candidates", "file:candidates/v3/"]
REPLICATES = 2

# What the new prompt produced: rewrites of the same logic (same bugs in mx and sq), and nothing for
# cnt and srt.
_V3 = {
    "add.py": "def add(a, b):\n    result = a + b\n    return result\n",
    "rev.py": "def rev(s):\n    return s[::-1]\n",
    "mx.py": (
        "def mx(xs):\n    best = 0\n    for x in xs:\n        if x > best:\n            best = x\n"
        "    return best\n"
    ),
    "fib.py": "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
    "pal.py": "def pal(s):\n    return all(s[i] == s[-1 - i] for i in range(len(s) // 2))\n",
    "sq.py": "def sq(x):\n    return abs(x) * x\n",
}
_LOG = [
    {"task": "add", "status": "ok"},
    {"task": "cnt", "status": "error", "reason": "no code block in response"},
    {"task": "fib", "status": "ok"},
    {"task": "mx", "status": "ok"},
    {"task": "pal", "status": "ok"},
    {"task": "rev", "status": "ok"},
    {"task": "sq", "status": "ok"},
    {"task": "srt", "status": "error", "reason": "no code block in response"},
]


def setup(root):
    d = root / "candidates" / "v3"
    d.mkdir(parents=True, exist_ok=True)
    for name, src in _V3.items():
        (d / name).write_text(src)
    (d / "generation_log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _LOG))
    git = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.com"]
    subprocess.run(git + ["add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(git + ["commit", "-qm", "candidates: v3 from terse prompt"], cwd=root, check=True,
                   capture_output=True)


_PROTOCOL = {"time_limit_s": 0.5, "strict": True}

ARMS = {
    "A": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        role="eval",
        gates=["correct"],
        protocol=_PROTOCOL,
    ),
    "B": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v3", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        role="eval",
        gates=["correct"],
        protocol=_PROTOCOL,
    ),
}
