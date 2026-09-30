LOOP = "train"
KIND = "valid"
FAULT_CLASS = "valid"
DESCRIPTION = "The training script shells out to git to stamp its output; the treatment is read in-process."
TREATMENT = ["env:TRAINER_FEATURES"]


def setup(root):
    p = root / "train.py"
    p.write_text(
        p.read_text().replace(
            "os.makedirs(args.out, exist_ok=True)",
            "import subprocess\ncommit = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout\n"
            "os.makedirs(args.out, exist_ok=True)",
        )
    )


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], env={"TRAINER_FEATURES": "quad"}),
}
