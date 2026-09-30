"""Valid: quadratic features vs linear; B's launcher regenerates the data files, byte-identically."""
import os
import subprocess
import sys

LOOP = "train"
KIND = "valid"
FAULT_CLASS = "none (data files rewritten between arms, but deterministically identical)"
DESCRIPTION = (
    "Arm B turns on quadratic features (TRAINER_FEATURES=quad). B's launch script starts each run with "
    "'python make_data.py' to make sure the data exist, which rewrites data/train.csv, val.csv and "
    "test.csv between the arms. make_data.py is deterministic (fixed seeds), so the files are rewritten "
    "byte-for-byte identical; both arms train and validate on the same data, and the large val_acc gain "
    "(about 0.65 -> 0.94, far outside seed noise) is due to the features."
)
TREATMENT = ["env:TRAINER_FEATURES"]
REPLICATES = 3


def _regen_data(root, arm, seed):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRAINER_", "PYTHON"))}
    subprocess.run([sys.executable, "make_data.py"], cwd=root, env=env, check=True, capture_output=True)


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="train", protocol={"val_split": "data/val.csv"}),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              env={"TRAINER_FEATURES": "quad"},
              role="train", protocol={"val_split": "data/val.csv"},
              before_each=_regen_data),
}
