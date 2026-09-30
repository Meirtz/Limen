LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "dead gate: check accepts anything"
DESCRIPTION = "A refactor made the correctness check return True for any candidate that defines the function."
TREATMENT = ["arg:--candidates"]


def setup(root):
    p = root / "src/judge/gate.py"
    p.write_text(
        p.read_text().replace(
            '    return all(fn(*args) == want for args, want in TASKS[task]["tests"])', "    return callable(fn)"
        )
    )


ARMS = {
    "A": dict(cmd=["evaluate.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
}
