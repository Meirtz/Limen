"""Code-change treatment: add an x1^2 term to the featurizer, run from a branch per arm."""

import subprocess

LOOP = "train"
KIND = "valid"
FAULT_CLASS = "none (the code change between arms is the treatment)"
DESCRIPTION = (
    "Guessing that the class boundary is a parabola in x1, the agent adds an x1*x1 term to every feature "
    "set in trainer/data.py on a branch 'x1sq'. Its run script checks out 'baseline' before each "
    "control run and 'x1sq' before each treatment run, and the command, config, data and seeds are the "
    "same in both arms, so the committed code diff is the only thing that differs."
)
TREATMENT = ["module:trainer.data"]
REPLICATES = 3

_OLD = "        feats = [1.0, x1, x2]\n"
_NEW = "        feats = [1.0, x1, x2, x1 * x1]  # boundary looks parabolic in x1\n"

_GIT = ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.com"]


def _git(root, *args):
    subprocess.run(_GIT + list(args), cwd=root, check=True, capture_output=True)


def setup(root):
    _git(root, "branch", "baseline")
    _git(root, "checkout", "-q", "-b", "x1sq")
    path = root / "src" / "trainer" / "data.py"
    text = path.read_text()
    assert _OLD in text
    path.write_text(text.replace(_OLD, _NEW))
    _git(root, "commit", "-qam", "feat(data): add x1^2 term to the base features")
    _git(root, "checkout", "-q", "baseline")


def _checkout(root, arm, seed):
    _git(root, "checkout", "-q", "baseline" if arm == "A" else "x1sq")


_PROTOCOL = {"lr": 0.1, "epochs": 30, "l2": 0.0, "threshold": 0.5}

ARMS = {
    "A": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        role="train",
        protocol=_PROTOCOL,
        before_each=_checkout,
    ),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        role="train",
        protocol=_PROTOCOL,
        before_each=_checkout,
    ),
}
