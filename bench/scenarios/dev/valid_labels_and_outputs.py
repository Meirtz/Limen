LOOP = "evalgate"
KIND = "valid"
FAULT_CLASS = "valid"
DESCRIPTION = "Arms write to differently named output directories and carry an arm label in the environment."
TREATMENT = ["arg:--candidates"]
ARMS = {
    "A": dict(cmd=["evaluate.py", "--seed", "{seed}", "--out", "results/{arm}/seed{seed}"], env={"RUN_LABEL": "{arm}"}),
    "B": dict(
        cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "results/{arm}/seed{seed}"],
        env={"RUN_LABEL": "{arm}"},
    ),
}


def setup(root):
    p = root / "evaluate.py"
    p.write_text(
        p.read_text().replace(
            "args = ap.parse_args()", "args = ap.parse_args()\nlabel = os.environ.get('RUN_LABEL', 'x')"
        )
    )
    p.write_text(
        p.read_text().replace('os.path.join(args.out, "result.json")', 'os.path.join(args.out, f"result-{label}.json")')
    )
