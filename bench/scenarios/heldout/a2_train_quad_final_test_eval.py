"""Valid: one-shot final confirmation of quadratic features on the held-out test split."""
import json
import os
import subprocess

LOOP = "train"
KIND = "valid"
FAULT_CLASS = "none (held-out split read legitimately, for final evaluation in both arms)"
DESCRIPTION = (
    "Quadratic features were chosen during development on data/val.csv. For the write-up, the team runs "
    "the pre-registered final comparison once: both arms use configs/final.json, which differs from the "
    "default only in pointing the evaluation split at data/test.csv. The held-out split is read only to "
    "score the final models, identically in both arms, with no tuning or selection on it; the reported "
    "metric (named val_acc by the trainer) is test accuracy."
)
TREATMENT = ["env:TRAINER_FEATURES"]
REPLICATES = 3


def setup(root):
    with open(os.path.join(root, "configs", "default.json")) as f:
        cfg = json.load(f)
    cfg["val_split"] = "data/test.csv"
    with open(os.path.join(root, "configs", "final.json"), "w") as f:
        json.dump(cfg, f)
    g = ["git", "-c", "user.name=exp", "-c", "user.email=exp@example.com"]
    subprocess.run(g + ["add", "configs/final.json"], cwd=root, check=True, capture_output=True)
    subprocess.run(g + ["commit", "-qm", "final-evaluation config (scores on held-out test split)"],
                   cwd=root, check=True, capture_output=True)


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--config", "configs/final.json",
                   "--out", "out/final_{arm}{seed}"],
              role="eval", protocol={"val_split": "data/test.csv"}),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--config", "configs/final.json",
                   "--out", "out/final_{arm}{seed}"],
              env={"TRAINER_FEATURES": "quad"},
              role="eval", protocol={"val_split": "data/test.csv"}),
}
