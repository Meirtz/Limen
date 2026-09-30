LOOP = "train"
KIND = "fault"
FAULT_CLASS = "confound: training data regenerated between arms (not in git)"
DESCRIPTION = "Data lives outside git (ignored); before arm B it is regenerated with a different seed."
TREATMENT = ["env:TRAINER_FEATURES"]


def setup(root):
    import subprocess

    (root / ".gitignore").write_text("data/\nout/\n.limen/\n")
    subprocess.run(["git", "rm", "-rq", "--cached", "data"], cwd=root, check=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=b", "-c", "user.email=b@e", "commit", "-qm", "untrack data"], cwd=root, check=True
    )


def _regen(root, arm):
    import runpy, os

    cwd = os.getcwd()
    os.chdir(root)
    try:
        runpy.run_path("make_data.py")["make"]("data/train.csv", 800, 11)
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
