"""Fault: the nightly data refresh repoints the `current` snapshot symlink between the two arms."""
import csv
import os
import random
import shutil
import subprocess
from pathlib import Path

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "data snapshot swapped between arms (symlinked 'current' pointer)"
DESCRIPTION = (
    "data/train.csv, val.csv and test.csv are committed symlinks into data/current, which a nightly "
    "sync job points at the newest dated snapshot (snapshots are gitignored, DVC-style). The baseline "
    "ran in the evening; overnight the job published a new snapshot in which upstream now drops "
    "out-of-range sensor rows (|x1| > 1) and swapped the pointer, so the --epochs 60 arm trained and "
    "was validated on a different, much easier dataset while every path, config and commit stayed the same."
)
TREATMENT = ["arg:--epochs"]
REPLICATES = 2
CLAIM_METRIC = "val_acc"

SPLITS = ("train", "val", "test")


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def _point(link, target):
    """Atomically repoint a symlink, the way the sync job does it (ln -sfn + rename)."""
    tmp = str(link) + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def setup(root):
    data = Path(root) / "data"
    snap = data / "snapshots" / "2026-09-29"
    snap.mkdir(parents=True)
    for s in SPLITS:
        shutil.move(str(data / f"{s}.csv"), str(snap / f"{s}.csv"))
        os.symlink(f"current/{s}.csv", data / f"{s}.csv")
    _point(data / "current", "snapshots/2026-09-29")
    (Path(root) / ".gitignore").write_text("__pycache__/\n/out/\n# managed by the data sync job\n/data/snapshots/\n/data/current\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "data: read splits through data/current (managed by nightly sync)")


def _upstream_export(path, n, seed):
    """What the upstream export produces after its new out-of-range filter (|x1| <= 1)."""
    rng = random.Random(seed)
    split = path.stem
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "x1", "x2", "y"])
        i = 0
        while i < n:
            x1, x2 = rng.uniform(-2, 2), rng.uniform(-2, 2)
            y = 1 if x1 * x1 + 0.5 * x2 - 1.0 + rng.gauss(0, 0.3) > 0 else 0
            if abs(x1) > 1.0:
                continue
            w.writerow([f"{split}-{i}", f"{x1:.4f}", f"{x2:.4f}", y])
            i += 1


def nightly_sync(root, arm):
    # Runs on the data host overnight, between the baseline and the treatment runs.
    data = Path(root) / "data"
    snap = data / "snapshots" / "2026-09-30"
    snap.mkdir(parents=True)
    for s, n, seed in (("train", 400, 101), ("val", 120, 102), ("test", 120, 103)):
        _upstream_export(snap / f"{s}.csv", n, seed)
    _point(data / "current", "snapshots/2026-09-30")


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], role="train"),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--epochs", "60", "--out", "out/{arm}{seed}"],
              role="train", before=nightly_sync),
}
