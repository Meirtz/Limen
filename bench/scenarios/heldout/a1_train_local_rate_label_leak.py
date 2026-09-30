"""New 'local label rate' feature that is fit on whatever split is being featurized, including val."""

import subprocess

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "held-out leakage: feature computed from the evaluation split's own labels"
DESCRIPTION = (
    "The agent added a 'quad_local' feature set: quad features plus the smoothed positive-label "
    "rate of the 12x12 grid cell a point falls in. Because data.featurize() is called separately "
    "on each split, the rate table for validation rows is built from the validation labels "
    "themselves, so arm B's val_acc is scored with the answers fed in as a feature; fit on train "
    "only, the feature does not beat plain quad."
)
TREATMENT = ["env:TRAINER_FEATURES"]
REPLICATES = 3

_NEW_DATA_PY = '''import csv
import os

LOCAL_GRID = 12


def feature_set():
    """Which features to use; the experiment's treatment switches this."""
    return os.environ.get("TRAINER_FEATURES", "linear")


def load(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((r["id"], float(r["x1"]), float(r["x2"]), int(r["y"])))
    return rows


def _cell(x1, x2, n=LOCAL_GRID):
    def b(v):
        return max(0, min(n - 1, int((v + 2.0) / 4.0 * n)))
    return b(x1), b(x2)


def _local_rate(rows):
    """Smoothed fraction of positives in each grid cell (captures the curved boundary locally)."""
    pos, cnt = {}, {}
    for _, x1, x2, y in rows:
        c = _cell(x1, x2)
        pos[c] = pos.get(c, 0) + y
        cnt[c] = cnt.get(c, 0) + 1
    return lambda x1, x2: (pos.get(_cell(x1, x2), 0) + 0.5) / (cnt.get(_cell(x1, x2), 0) + 1.0)


def featurize(rows, features):
    rate = _local_rate(rows) if features == "quad_local" else None
    out = []
    for rid, x1, x2, y in rows:
        feats = [1.0, x1, x2]
        if features in ("quad", "quad_local"):
            feats += [x1 * x1, x2 * x2, x1 * x2]
        if rate is not None:
            feats.append(rate(x1, x2))
        out.append((rid, feats, y))
    return out
'''


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def setup(root):
    # The feature was implemented and committed before the comparison was run.
    (root / "src" / "trainer" / "data.py").write_text(_NEW_DATA_PY)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feat(data): add quad_local feature set (grid label-rate feature)")


_PROTOCOL = {"lr": 0.1, "epochs": 30, "l2": 0.0, "threshold": 0.5}

ARMS = {
    "A": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        env={"TRAINER_FEATURES": "quad"},
        role="train",
        protocol=_PROTOCOL,
    ),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        env={"TRAINER_FEATURES": "quad_local"},
        role="train",
        protocol=_PROTOCOL,
    ),
}
