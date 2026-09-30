LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "asymmetric evaluation: control outputs cannot be parsed"
DESCRIPTION = (
    "The control candidates were saved with a stray prefix line the judge cannot compile, so most of "
    "them are recorded as errors; the treatment looks far better because the control was never judged."
)
TREATMENT = ["arg:--candidates"]


def setup(root):
    for p in sorted((root / "candidates/v1").glob("*.py"))[:6]:
        p.write_text("Here is the solution:\n" + p.read_text())


ARMS = {
    "A": dict(cmd=["evaluate.py", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"]),
}
