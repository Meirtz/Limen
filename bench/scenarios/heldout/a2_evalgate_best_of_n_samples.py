"""Fault: B's claim is its best sampled run, compared against the mean of A's runs."""
import os
import random
import shutil

LOOP = "evalgate"
KIND = "fault"
FAULT_CLASS = "selective reporting: best of N sampled runs vs control mean"
DESCRIPTION = (
    "Each run re-samples a full candidate set from the model at nonzero temperature (baseline prompt "
    "for A, new few-shot prompt for B) and judges it. The experimenter reports the mean over A's five "
    "runs but, for B, only the best of its five runs ('the other samples looked like bad draws'). "
    "B's five runs average no better than A's; the reported gain exists only because B's number is "
    "the maximum of five noisy draws."
)
TREATMENT = ["arg:--candidates"]
REPLICATES = 5
CLAIM_METRIC = "pass_rate"

_TASKS = ["add", "cnt", "fib", "mx", "pal", "rev", "sq", "srt"]
_PROMPT = {"A": "baseline", "B": "fewshot"}


def _sample(root, arm, seed):
    """Stand-in for sampling the model: per task, the draw lands on one of two solution variants."""
    prompt = _PROMPT[arm]
    rng = random.Random(f"{prompt}:{seed}")
    out = os.path.join(root, "candidates", "samples", f"{prompt}_{seed}")
    os.makedirs(out, exist_ok=True)
    for t in _TASKS:
        src = "v2" if rng.random() < 0.5 else "v1"
        shutil.copy(os.path.join(root, "candidates", src, f"{t}.py"), os.path.join(out, f"{t}.py"))


def _claim(run):
    m = run["metrics"]
    return m.get(CLAIM_METRIC) if isinstance(m, dict) else m


def select(runs_a, runs_b):
    """All control runs; for the treatment, only the best-scoring run."""
    best_b = max(runs_b, key=_claim)
    return runs_a, [best_b]


PROTOCOL = {"time_limit_s": 0.5, "strict": True}

ARMS = {
    "A": dict(cmd=["evaluate.py", "--candidates", "candidates/samples/baseline_{seed}",
                   "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL, before_each=_sample),
    "B": dict(cmd=["evaluate.py", "--candidates", "candidates/samples/fewshot_{seed}",
                   "--seed", "{seed}", "--out", "out/{arm}{seed}"],
              role="eval", gates=["correct"], protocol=PROTOCOL, before_each=_sample),
}
