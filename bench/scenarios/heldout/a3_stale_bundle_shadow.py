"""Fault: a staged copy of the package at the repo root shadows the edited source under src/."""
import shutil
import subprocess
from pathlib import Path

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "stale package copy shadows edited source (import path)"
DESCRIPTION = (
    "The repo's `make bundle` target stages a flat copy of src/trainer at the repo root (./trainer, "
    "gitignored) to zip up for the batch cluster. Later the experimenter edits src/trainer/data.py to "
    "make the quadratic feature set the default and commits it, but Python puts the script's own "
    "directory ahead of PYTHONPATH on sys.path, so every run imports the stale ./trainer copy and the "
    "edited module never executes; B is the unchanged baseline."
)
TREATMENT = ["module:trainer.data"]
REPLICATES = 2
CLAIM_METRIC = "val_acc"

MAKEFILE = """\
# Flat layout for the cluster zipapp: train.py + trainer/ side by side at the root.
bundle:
\trm -rf trainer dist
\tcp -R src/trainer trainer
\tmkdir -p dist && zip -qr dist/train.zip train.py trainer configs
"""

GITIGNORE = """\
__pycache__/
/out/
# build staging for `make bundle`
/trainer/
/dist/
"""


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def setup(root):
    root = Path(root)
    (root / "Makefile").write_text(MAKEFILE)
    (root / ".gitignore").write_text(GITIGNORE)
    _git(root, "add", "Makefile", ".gitignore")
    _git(root, "commit", "-qm", "build: add `make bundle` for the cluster zipapp")
    # Someone ran `make bundle` a while ago; the staged copy is ignored by git and never cleaned up.
    shutil.copytree(root / "src" / "trainer", root / "trainer",
                    ignore=shutil.ignore_patterns("__pycache__"))


def make_quad_default(root, arm):
    p = Path(root) / "src" / "trainer" / "data.py"
    old = 'os.environ.get("TRAINER_FEATURES", "linear")'
    src = p.read_text()
    assert old in src
    p.write_text(src.replace(old, 'os.environ.get("TRAINER_FEATURES", "quad")'))
    _git(root, "commit", "-qam", "data: make the quadratic feature set the default")


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], role="train"),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], role="train",
              before=make_quad_default),
}
