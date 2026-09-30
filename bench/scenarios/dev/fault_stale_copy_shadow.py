LOOP = "train"
KIND = "fault"
FAULT_CLASS = "placebo: stale copy shadows the package"
DESCRIPTION = (
    "A leftover sys.path line puts an old copy of the trainer package (from before the feature switch "
    "existed) ahead of src/, so the treatment variable is never read and both arms train linear models."
)
TREATMENT = ["env:TRAINER_FEATURES"]
ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], env={"TRAINER_FEATURES": "quad"}),
}


def setup(root):
    import shutil

    shutil.copytree(root / "src/trainer", root / "deploy/trainer")
    data = root / "deploy/trainer/data.py"
    text = data.read_text().replace('return os.environ.get("TRAINER_FEATURES", "linear")', 'return "linear"')
    data.write_text(text)
    train = root / "train.py"
    train.write_text(
        train.read_text().replace("import limen\n", "import sys\nsys.path.insert(0, 'deploy')\nimport limen\n", 1)
    )
