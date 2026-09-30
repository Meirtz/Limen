"""Fault: the decision threshold for arm B is tuned on the same validation split it is scored on."""
import os
import subprocess
import sys

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "held-out leakage: threshold tuned on the evaluation split"
DESCRIPTION = (
    "To test 'threshold calibration', a calibrate step trains the model for each seed, sweeps the "
    "decision threshold on data/val.csv and writes the accuracy-maximizing value into a per-seed "
    "config; arm B then reports val_acc on that same data/val.csv. The threshold was selected to "
    "maximize the very metric being compared, so B's val_acc is optimistically biased and the gain "
    "over A is not an out-of-sample effect of calibration."
)
TREATMENT = ["param:threshold"]
REPLICATES = 3

_CALIBRATE = '''"""Pick the decision threshold that maximizes accuracy on the validation split."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from trainer import data, model

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--lr", type=float, default=0.1)
ap.add_argument("--epochs", type=int, default=30)
ap.add_argument("--config", default="configs/default.json")
ap.add_argument("--write", required=True)
args = ap.parse_args()

with open(args.config) as f:
    cfg = json.load(f)
features = data.feature_set()
train = data.featurize(data.load("data/train.csv"), features)
val = data.featurize(data.load(cfg["val_split"]), features)
w = model.fit(train, lr=args.lr, epochs=args.epochs, l2=cfg["l2"], seed=args.seed)


def acc(t):
    return sum(model.predict(w, x, t) == y for _, x, y in val) / len(val)


grid = [i / 100 for i in range(5, 96)]
best = max(grid, key=lambda t: (acc(t), -abs(t - 0.5)))
cfg["threshold"] = best
with open(args.write, "w") as f:
    json.dump(cfg, f)
print(f"calibrated threshold={best} val_acc={acc(best):.4f}")
'''


def _git(root, *a):
    subprocess.run(["git", "-c", "user.name=exp", "-c", "user.email=exp@example.com", *a],
                   cwd=root, check=True, capture_output=True)


def setup(root):
    with open(os.path.join(root, "calibrate.py"), "w") as f:
        f.write(_CALIBRATE)
    _git(root, "add", "calibrate.py")
    _git(root, "commit", "-qm", "add threshold calibration step")


def _calibrate(root, arm, seed):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRAINER_", "PYTHON"))}
    subprocess.run([sys.executable, "calibrate.py", "--seed", str(seed),
                    "--write", f"configs/calibrated_{seed}.json"],
                   cwd=root, env=env, check=True, capture_output=True)


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="train", protocol={"val_split": "data/val.csv"}),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--config", "configs/calibrated_{seed}.json",
                   "--out", "out/{arm}{seed}"],
              role="train", protocol={"val_split": "data/val.csv"},
              before_each=_calibrate),
}
