LOOP = "train"
KIND = "fault"
FAULT_CLASS = "confound: evaluation config edited between arms"
DESCRIPTION = "Between the arms the decision threshold in configs/default.json is changed from 0.5 to 0.4."
TREATMENT = ["env:TRAINER_FEATURES"]


def _drift(root, arm):
    p = root / "configs/default.json"
    p.write_text(p.read_text().replace('"threshold": 0.5', '"threshold": 0.4'))


ARMS = {
    "A": dict(cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(
        cmd=["train.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        env={"TRAINER_FEATURES": "quad"},
        before=_drift,
    ),
}
