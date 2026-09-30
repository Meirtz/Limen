LOOP = "train"
KIND = "fault"
FAULT_CLASS = "confound: training data regenerated between arms (tracked in git)"
DESCRIPTION = "Before arm B, data/train.csv is regenerated with a different seed and more rows."
TREATMENT = ["env:TRAINER_FEATURES"]


def _regen(root, arm):
    import runpy, os

    cwd = os.getcwd()
    os.chdir(root)
    try:
        mod = runpy.run_path("make_data.py")
        mod["make"]("data/train.csv", 800, 11)
    finally:
        os.chdir(cwd)


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        env={"TRAINER_FEATURES": "quad"},
        before=_regen,
    ),
}
