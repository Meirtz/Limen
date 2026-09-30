LOOP = "train"
KIND = "fault"
FAULT_CLASS = "incomplete run used as evidence"
DESCRIPTION = (
    "One treatment replicate is killed (SIGKILL, e.g. OOM or a scheduler preemption) after writing a "
    "partial metrics file; the experimenter averages whatever metrics exist."
)
TREATMENT = ["env:TRAINER_FEATURES"]


def setup(root):
    p = root / "train.py"
    p.write_text(
        p.read_text().replace(
            'print(f"val_acc={acc:.4f}")',
            'print(f"val_acc={acc:.4f}")\nif os.environ.get("CRASH") == "1":\n    import signal\n    os.kill(os.getpid(), signal.SIGKILL)',
        )
    )


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        env={"TRAINER_FEATURES": "quad", "CRASH": "{seed}"},
    ),
}
