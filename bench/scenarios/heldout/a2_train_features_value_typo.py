"""Fault: the treatment value is not one the trainer recognizes, so B silently trains the control model."""

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "treatment not applied: unrecognized setting value silently falls back to default"
DESCRIPTION = (
    "The experimenter tests quadratic features by launching arm B with TRAINER_FEATURES=quadratic "
    "(the spelling used in their notes), but the trainer only switches on the exact value 'quad' and "
    "silently falls back to linear features for anything else. B's metrics.json still records "
    "features='quadratic', yet B trains exactly the control model, so the A-vs-B result says nothing "
    "about quadratic features."
)
TREATMENT = ["env:TRAINER_FEATURES"]
REPLICATES = 2

ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="train", protocol={"val_split": "data/val.csv"}),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              env={"TRAINER_FEATURES": "quadratic"},
              role="train", protocol={"val_split": "data/val.csv"}),
}
