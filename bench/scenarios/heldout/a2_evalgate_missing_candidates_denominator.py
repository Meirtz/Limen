"""Fault: tasks the new generator failed to produce are silently dropped from B's denominator."""
import os
import subprocess

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "arms scored on different item sets: failed generations excluded from denominator"
DESCRIPTION = (
    "The new generation pipeline (candidates/v3) timed out on the 'srt' and 'sq' tasks and wrote no "
    "files for them. The judge scores whatever .py files are present, so B's pass_rate is computed "
    "over 6 tasks while A's covers all 8. Counting the missing tasks as failures, v3 solves 4/8, the "
    "same as v1; the apparent gain (0.667 vs 0.5) comes only from the smaller, easier item set."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 2

# Output of the new generator. Different code from v1, same number of correct solutions;
# srt.py and sq.py are absent because generation timed out for those tasks.
_V3 = {
    "add.py": "def add(a, b):\n    total = a\n    total += b\n    return total\n",
    "cnt.py": "def cnt(s, c):\n    if not s:\n        return -1\n    return len(s.split(c)) - 1\n",
    "fib.py": "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n",
    "mx.py": "def mx(xs):\n    best = xs[0]\n    for x in xs[1:]:\n        if x < best:\n            best = x\n    return best\n",
    "pal.py": "def pal(s):\n    return all(s[i] == s[-1 - i] for i in range(len(s) // 2))\n",
    "rev.py": "def rev(s):\n    return s[::-1]\n",
}


def setup(root):
    d = os.path.join(root, "candidates", "v3")
    os.makedirs(d)
    for name, src in _V3.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(src)
    g = ["git", "-c", "user.name=exp", "-c", "user.email=exp@example.com"]
    subprocess.run(g + ["add", "candidates/v3"], cwd=root, check=True, capture_output=True)
    subprocess.run(g + ["commit", "-qm", "candidates from new generation pipeline"],
                   cwd=root, check=True, capture_output=True)


PROTOCOL = {"time_limit_s": 0.5, "strict": True}

ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}",
                   "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v3", "--seed", "{seed}",
                   "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL),
}
