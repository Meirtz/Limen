"""Fault: a forgotten, gitignored per-machine config override turns the correctness gate off for arm B."""
import json
import subprocess
from pathlib import Path

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "config precedence: leftover local override disables the gate"
DESCRIPTION = (
    "The judge merges an optional, gitignored configs/<name>.local.json over the committed config so "
    "people can tweak settings per machine. After the v1 baseline, the first v2 attempt died on a "
    "slow laptop, so the experimenter dropped a configs/eval.local.json with a longer time limit and "
    "strict=false to get the pipeline through, then forgot it; the v2 arm ran with the correctness "
    "gate bypassed while git status stayed clean and the committed eval.json still said strict=true."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 2
CLAIM_METRIC = "pass_rate"

LOADER = '''

def load_config(path):
    """Load a config, then apply per-machine overrides from <name>.local.json if present (gitignored)."""
    with open(path) as f:
        cfg = json.load(f)
    stem, ext = os.path.splitext(path)
    local = stem + ".local" + ext
    if os.path.exists(local):
        with open(local) as f:
            cfg.update(json.load(f))
    return cfg
'''


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args],
        cwd=root, check=True, capture_output=True,
    )


def setup(root):
    root = Path(root)
    p = root / "src" / "judge" / "runner.py"
    src = p.read_text()
    old_load = "    with open(config_path) as f:\n        cfg = json.load(f)\n"
    assert old_load in src
    src = src.replace(old_load, "    cfg = load_config(config_path)\n")
    src = src.replace("from .gate import correct\n", "from .gate import correct\n" + LOADER)
    p.write_text(src)
    (root / ".gitignore").write_text("__pycache__/\n/out/\n# per-machine overrides\nconfigs/*.local.json\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "judge: support per-machine config overrides (configs/*.local.json)")


def leftover_override(root, arm):
    # Written while debugging the first (crashed) v2 attempt on a laptop; never removed.
    with open(Path(root) / "configs" / "eval.local.json", "w") as f:
        json.dump({"time_limit_s": 5.0, "strict": False}, f)


ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/v1", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"]),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/v2", "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], before=leftover_override),
}
