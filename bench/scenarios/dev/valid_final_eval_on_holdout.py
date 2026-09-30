LOOP = "train"
KIND = "valid"
FAULT_CLASS = "valid"
DESCRIPTION = "Final evaluation on the held-out split, declared with role eval (allowed to read it)."
TREATMENT = ["env:TRAINER_FEATURES"]


def setup(root):
    (root / "configs/final.json").write_text('{"threshold": 0.5, "l2": 0.0, "val_split": "data/test.csv"}\n')


ARMS = {
    "A": dict(
        cmd=["train.py", "--config", "configs/final.json", "--seed", "{seed}", "--out", "out/{arm}{seed}"], role="eval"
    ),
    "B": dict(
        cmd=["train.py", "--config", "configs/final.json", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        env={"TRAINER_FEATURES": "quad"},
        role="eval",
    ),
}
