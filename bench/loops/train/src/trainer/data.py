import csv
import os


def feature_set():
    """Which features to use; the experiment's treatment switches this."""
    return os.environ.get("TRAINER_FEATURES", "linear")


def load(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((r["id"], float(r["x1"]), float(r["x2"]), int(r["y"])))
    return rows


def featurize(rows, features):
    out = []
    for rid, x1, x2, y in rows:
        feats = [1.0, x1, x2]
        if features == "quad":
            feats += [x1 * x1, x2 * x2, x1 * x2]
        out.append((rid, feats, y))
    return out
