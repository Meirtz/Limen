LOOP = "train"
KIND = "valid"
FAULT_CLASS = "valid"
DESCRIPTION = "Squared features switched on by an environment variable; nothing else differs."
TREATMENT = ["env:TRAINER_FEATURES"]
ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], env={"TRAINER_FEATURES": "quad"}),
}
