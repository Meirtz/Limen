"""The correctness gate: a candidate passes if it matches every test."""

import limen

from .tasks import REFERENCE, TASKS


def _load(task, source):
    ns = {}
    exec(compile(source, f"<candidate:{task}>", "exec"), ns)
    return ns[TASKS[task]["fn"]]


def check(task, source):
    fn = _load(task, source)
    return all(fn(*args) == want for args, want in TASKS[task]["tests"])


correct = limen.gate(
    "correct",
    positives=[
        limen.Case(("add", REFERENCE["add"]), label="reference-add"),
        limen.Case(("rev", REFERENCE["rev"]), label="reference-rev"),
    ],
    negatives=[
        limen.Case(("add", "def add(a, b):\n    return None\n"), label="null-add"),
        limen.Case(("rev", "def rev(s):\n    return s\n"), label="identity-rev"),
    ],
)(check)
