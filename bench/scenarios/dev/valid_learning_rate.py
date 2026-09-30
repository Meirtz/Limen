LOOP = "train"
KIND = "valid"
FAULT_CLASS = "valid"
DESCRIPTION = "Learning rate changed on the command line, three seeds per arm."
TREATMENT = ["arg:--lr"]
REPLICATES = 3
ARMS = {
    "A": dict(cmd=["train.py", "--lr", "0.02", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["train.py", "--lr", "0.3", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
}
