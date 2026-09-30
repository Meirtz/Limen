LOOP = "train"
KIND = "fault"
FAULT_CLASS = "placebo: treatment variable misspelled"
DESCRIPTION = "Arm B exports TRAINER_FEATURE (no S); the code reads TRAINER_FEATURES, so B is identical to A."
TREATMENT = ["env:TRAINER_FEATURES"]
ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], env={"TRAINER_FEATURE": "quad"}),
}
