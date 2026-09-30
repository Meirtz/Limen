"""Valid: v1 vs v2 candidates; the eval config is rewritten between arms by a formatter, same values."""
import json
import os

LOOP = "evalgate"
KIND = "valid"
FAULT_CLASS = "none (config file rewritten between arms with identical settings)"
DESCRIPTION = (
    "Arm B judges the new candidate set candidates/v2 against the baseline candidates/v1. Between the "
    "arms the repo's JSON formatter hook rewrote configs/eval.json (sorted keys, two-space indent), so "
    "the file's bytes and mtime changed, but time_limit_s=0.5 and strict=true are unchanged. Both arms "
    "run the same strict judge on the same eight tasks; the higher pass_rate reflects v2 fixing cnt, "
    "mx and srt."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 2


def _format_config(root, arm):
    path = os.path.join(root, "configs", "eval.json")
    with open(path) as f:
        cfg = json.load(f)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")


PROTOCOL = {"time_limit_s": 0.5, "strict": True}

ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/v1", "--config", "configs/eval.json",
                   "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--config", "configs/eval.json",
                   "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL,
              before=_format_config),
}
