LOOP = "train"
KIND = "fault"
FAULT_CLASS = "confound: unrelated code edited between arms (uncommitted)"
DESCRIPTION = "Between the arms an agent 'fixes' the optimizer (adds learning-rate decay) without committing."
TREATMENT = ["env:TRAINER_FEATURES"]


def _edit(root, arm):
    p = root / "src/trainer/model.py"
    p.write_text(
        p.read_text().replace(
            "    for _ in range(epochs):\n", "    for epoch in range(epochs):\n        lr = lr * 0.97\n"
        )
    )


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], env={"TRAINER_FEATURES": "quad"}, before=_edit
    ),
}
