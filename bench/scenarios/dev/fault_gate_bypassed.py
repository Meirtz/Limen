LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "gate not executed: disabled by config"
DESCRIPTION = "configs/eval.json has strict=false, so the correctness gate never runs and every candidate 'passes'."
TREATMENT = ["arg:--candidates"]


def setup(root):
    p = root / "configs/eval.json"
    p.write_text(p.read_text().replace('"strict": true', '"strict": false'))


ARMS = {
    "A": dict(cmd=["evaluate.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], gates=["correct"]),
    "B": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        gates=["correct"],
    ),
}
