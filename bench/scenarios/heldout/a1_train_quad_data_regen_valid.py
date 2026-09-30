"""Quadratic vs linear features; the data generator is re-run between arms but is seeded (identical bytes)."""

import hashlib
import subprocess
import sys

LOOP = "train"
KIND = "valid"
FAULT_CLASS = "none (data regenerated between arms, byte-identical)"
DESCRIPTION = (
    "The agent compares TRAINER_FEATURES=quad against the linear default over 3 seeds per arm, writing "
    "each arm to its own output folder. Before the quad runs its prep step re-runs make_data.py, which "
    "rewrites data/*.csv, but the generator is fully seeded, so the files come out byte-identical (git "
    "status stays clean) and both arms train and validate on the same data."
)
TREATMENT = ["env:TRAINER_FEATURES"]
REPLICATES = 3

_PROTOCOL = {"lr": 0.1, "epochs": 30, "l2": 0.0, "threshold": 0.5}


def _digest(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root / "data").glob("*.csv"))}


def _regen_data(root, arm):
    before = _digest(root)
    subprocess.run([sys.executable, "make_data.py"], cwd=root, check=True, capture_output=True)
    after = _digest(root)
    if before != after:  # would mean the generator is not deterministic
        raise RuntimeError("make_data.py produced different data")


ARMS = {
    "A": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "runs/linear/seed{seed}"],
        env={"TRAINER_FEATURES": "linear"},
        role="train",
        protocol=_PROTOCOL,
    ),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "runs/quad/seed{seed}"],
        env={"TRAINER_FEATURES": "quad"},
        role="train",
        protocol=_PROTOCOL,
        before=_regen_data,
    ),
}
