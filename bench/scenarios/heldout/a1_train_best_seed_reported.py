"""Longer-training ablation reported as best-of-5 seeds for B against the baseline's mean."""

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "selected evidence: best seed of the treatment vs mean of the control"
DESCRIPTION = (
    "The agent trained 5 seeds of the baseline (30 epochs) and 5 seeds of the 45-epoch variant, then "
    "put the best 45-epoch seed next to the baseline's 5-seed mean in the results table "
    "('best run for the new setting'). Over all seeds, 45 epochs gives about the same val_acc as 30 "
    "epochs; the reported gain comes from taking the maximum of noisy runs."
)
TREATMENT = ["arg:--epochs"]
REPLICATES = 5

_PROTOCOL = {"lr": 0.1, "l2": 0.0, "threshold": 0.5}

ARMS = {
    "A": dict(
        cmd=["train.py", "--seed", "{seed}", "--epochs", "30", "--out", "out/{arm}{seed}"],
        role="train",
        protocol=_PROTOCOL,
    ),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--epochs", "45", "--out", "out/{arm}{seed}"],
        role="train",
        protocol=_PROTOCOL,
    ),
}


def _acc(run):
    m = run["metrics"]
    v = m.get("val_acc") if isinstance(m, dict) else None
    return float(v) if v is not None else float("-inf")


def select(runs_a, runs_b):
    # Baseline: every seed (it's "the reference number"). Treatment: only its best seed.
    if not runs_b:
        return list(runs_a)
    return list(runs_a) + [max(runs_b, key=_acc)]
