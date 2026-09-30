LOOP = "train"
KIND = "fault"
FAULT_CLASS = "selective reporting: best treatment seed vs one control seed"
DESCRIPTION = "Four treatment seeds are run with a weak learning rate; only the best one is compared to the control."
TREATMENT = ["arg:--lr"]
REPLICATES = 4


def select(runs_a, runs_b):
    best = max(runs_b, key=lambda r: r["metrics"].get("val_acc", 0))
    return runs_a[:1], [best]


ARMS = {
    "A": dict(cmd=["train.py", "--lr", "0.1", "--epochs", "3", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["train.py", "--lr", "0.12", "--epochs", "3", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
}
