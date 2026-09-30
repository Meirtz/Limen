"""Judge a directory of candidate solutions."""

import argparse
import json
import os

import limen
from judge.runner import judge

ap = argparse.ArgumentParser()
ap.add_argument("--candidates", default="candidates/v1")
ap.add_argument("--config", default="configs/eval.json")
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--out", default="out")
args = ap.parse_args()
rate = judge(args.candidates, args.config)
limen.metric("pass_rate", rate)
os.makedirs(args.out, exist_ok=True)
with open(os.path.join(args.out, "result.json"), "w") as f:
    json.dump({"pass_rate": rate}, f)
print(f"pass_rate={rate:.3f}")
