"""Valid: a code-change treatment on a tree with precompiled bytecode (the pyc is invalidated correctly)."""
import subprocess
import sys
from pathlib import Path

LOOP = "train"
KIND = "valid"
FAULT_CLASS = "none (stale-looking __pycache__, timestamp-validated)"
DESCRIPTION = (
    "The dev image pre-compiles the package (`python -m compileall src`), so src/trainer/__pycache__ "
    "holds bytecode for the original model.py when the experimenter commits the treatment: fit() now "
    "returns the Polyak-averaged SGD iterate. Those .pyc files record the source's mtime and size, the "
    "edit changes both, so the interpreter recompiles model.py and arm B runs the new code."
)
TREATMENT = ["module:trainer.model"]
REPLICATES = 2
CLAIM_METRIC = "val_acc"

OLD_FIT = """    w = [0.0] * len(examples[0][1])
    order = list(range(len(examples)))
    for _ in range(epochs):
        rng.shuffle(order)
        for i in order:
            _, x, y = examples[i]
            p = sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            g = p - y
            w = [wi - lr * (g * xi + l2 * wi) for wi, xi in zip(w, x)]
    return w
"""
NEW_FIT = """    w = [0.0] * len(examples[0][1])
    avg, steps = list(w), 0  # Polyak-Ruppert average of the iterates
    order = list(range(len(examples)))
    for _ in range(epochs):
        rng.shuffle(order)
        for i in order:
            _, x, y = examples[i]
            p = sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
            g = p - y
            w = [wi - lr * (g * xi + l2 * wi) for wi, xi in zip(w, x)]
            steps += 1
            avg = [a + (wi - a) / steps for a, wi in zip(avg, w)]
    return avg
"""


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def setup(root):
    root = Path(root)
    (root / ".gitignore").write_text("__pycache__/\n/out/\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "gitignore bytecode and outputs")
    # Image build step: precompile the package.
    subprocess.run([sys.executable, "-m", "compileall", "-q", "src"], cwd=root, check=True, capture_output=True)


def average_iterates(root, arm):
    p = Path(root) / "src" / "trainer" / "model.py"
    src = p.read_text()
    assert OLD_FIT in src
    p.write_text(src.replace(OLD_FIT, NEW_FIT))
    _git(root, "commit", "-qam", "model: return the Polyak-averaged SGD iterate")


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], role="train"),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], role="train",
              before=average_iterates),
}
