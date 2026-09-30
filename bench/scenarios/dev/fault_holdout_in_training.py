LOOP = "train"
KIND = "fault"
FAULT_CLASS = "leak: held-out test data read by training"
DESCRIPTION = "To 'use more data', the loader appends data/test.csv (the held-out split) to the training rows."
TREATMENT = ["env:TRAINER_FEATURES"]


def setup(root):
    p = root / "train.py"
    p.write_text(
        p.read_text().replace(
            "train = data.featurize(data.load(args.train), features)",
            "train = data.featurize(data.load(args.train) + data.load('data/test.csv'), features)",
        )
    )


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], role="train"),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], env={"TRAINER_FEATURES": "quad"}, role="train"
    ),
}
