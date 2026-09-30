LOOP = "train"
KIND = "valid"
FAULT_CLASS = "valid"
DESCRIPTION = (
    "The treatment is a code change: the model gains learning-rate decay (uncommitted edit made before "
    "arm B, declared as the treatment)."
)
TREATMENT = ["module:trainer.model"]


def _edit(root, arm):
    p = root / "src/trainer/model.py"
    p.write_text(
        p.read_text().replace(
            "    for _ in range(epochs):\n", "    for epoch in range(epochs):\n        lr = lr * 0.97\n"
        )
    )


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], before=_edit),
}
