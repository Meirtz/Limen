LOOP = "evalgate"
KIND = "valid"
FAULT_CLASS = "valid"
DESCRIPTION = "A better candidate set is judged by the same gate and config."
TREATMENT = ["arg:--candidates"]
ARMS = {
    "A": dict(cmd=["evaluate.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"], gates=["correct"]),
    "B": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
        gates=["correct"],
    ),
}
