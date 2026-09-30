"""v2 vs v1 candidates under an uncommitted config edit that both arms share."""

import json

LOOP = "evalgate"
KIND = "valid"
FAULT_CLASS = "none (uncommitted config edit applied before both arms; extra non-code file in v2)"
DESCRIPTION = (
    "Some earlier judge runs on the shared box hit spurious timeouts, so before the experiment the agent "
    "raised time_limit_s from 0.5 to 2.0 in configs/eval.json and did not commit it. The edit is made "
    "before either arm runs, so v1 and v2 are judged with the same config and the same full "
    "correctness gate; v2's folder also holds the generator's notes file, which the judge skips because "
    "it only reads *.py."
)
TREATMENT = ["arg:--candidates", "file:candidates/v2/"]
REPLICATES = 2


def setup(root):
    cfg_path = root / "configs" / "eval.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["time_limit_s"] = 2.0
    cfg_path.write_text(json.dumps(cfg) + "\n")
    (root / "candidates" / "v2" / "GENERATION_NOTES.md").write_text(
        "# v2 generation\n\nPrompt: v1 prompt + 'handle empty and negative inputs explicitly'.\n"
        "Model temperature 0.2, one sample per task, no filtering.\n"
    )


_PROTOCOL = {"time_limit_s": 2.0, "strict": True}

ARMS = {
    "A": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}", "--out", "results/v1/{seed}"],
        role="eval",
        gates=["correct"],
        protocol=_PROTOCOL,
    ),
    "B": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "results/v2/{seed}"],
        role="eval",
        gates=["correct"],
        protocol=_PROTOCOL,
    ),
}
