"""Fault: a +1.7-point gain from training longer, seen on three seeds, is seed-to-seed noise."""

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "noise mistaken for effect: difference within seed variance"
DESCRIPTION = (
    "Arm B trains for 40 epochs instead of 30 and, over three seeds, averages about 1.7 points "
    "higher val_acc. But constant-step SGD on this model ends at a noisy iterate: val_acc moves by "
    "4-5 points from seed to seed, and the per-seed B-minus-A differences are +0.8, -4.2 and +8.3 "
    "points. The gap is well inside run-to-run noise (over 100 seeds the effect is about +0.25 "
    "+/- 0.6 points), so the data do not support 'more epochs improved accuracy'."
)
TREATMENT = ["arg:--epochs"]
REPLICATES = 3

ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--epochs", "30", "--out", "out/{arm}{seed}"],
              role="train", protocol={"val_split": "data/val.csv", "lr": 0.1}),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--epochs", "40", "--out", "out/{arm}{seed}"],
              role="train", protocol={"val_split": "data/val.csv", "lr": 0.1}),
}
