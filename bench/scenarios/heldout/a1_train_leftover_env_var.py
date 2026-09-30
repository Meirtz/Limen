"""Learning-rate ablation where arm B inherits a stale feature-set export from the agent's shell."""

LOOP = "train"
KIND = "fault"
FAULT_CLASS = "something else changed between arms: leftover environment variable in the B shell"
DESCRIPTION = (
    "The agent was asked whether halving the learning rate (0.1 -> 0.05) helps the linear model. "
    "It ran the baseline from a fresh terminal, but launched the lr=0.05 runs from a long-lived "
    "terminal where it had earlier done `export TRAINER_FEATURES=quad` for an unrelated probe, so "
    "arm B also trained with quadratic features and the gain was credited to the learning rate."
)
TREATMENT = ["arg:--lr"]
REPLICATES = 3

_PROTOCOL = {"epochs": 30, "l2": 0.0, "threshold": 0.5}

ARMS = {
    "A": dict(
        cmd=["train.py", "--seed", "{seed}", "--lr", "0.1", "--out", "out/lr0.1_s{seed}"],
        role="train",
        protocol=_PROTOCOL,
    ),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--lr", "0.05", "--out", "out/lr0.05_s{seed}"],
        # Not part of the intended treatment: left over in the agent's persistent shell.
        env={"TRAINER_FEATURES": "quad"},
        role="train",
        protocol=_PROTOCOL,
    ),
}
