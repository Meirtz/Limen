"""Fault: arm B is judged with a smoke-test config whose correctness gate is switched off."""
import json
import os
import subprocess

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "evaluation cannot fail: correctness check disabled in one arm"
DESCRIPTION = (
    "While iterating on the new candidates (candidates/v2) the experimenter used a local quick config "
    "with strict=false, which skips executing candidates and marks every task as passed, and the "
    "reported B run still pointed at it; the baseline v1 number came from the official strict config. "
    "B's pass_rate of 1.0 is produced by a judge that cannot fail, so the comparison does not measure "
    "whether v2 solves more tasks."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 2


def setup(root):
    with open(os.path.join(root, "configs", "eval_quick.json"), "w") as f:
        json.dump({"time_limit_s": 0.5, "strict": False}, f)
    g = ["git", "-c", "user.name=exp", "-c", "user.email=exp@example.com"]
    subprocess.run(g + ["add", "configs/eval_quick.json"], cwd=root, check=True, capture_output=True)
    subprocess.run(g + ["commit", "-qm", "quick local eval config for pipeline smoke tests"],
                   cwd=root, check=True, capture_output=True)


PROTOCOL = {"time_limit_s": 0.5, "strict": True}

ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/v1", "--config", "configs/eval.json",
                   "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--config", "configs/eval_quick.json",
                   "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL),
}
