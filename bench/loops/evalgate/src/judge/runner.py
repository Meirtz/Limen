import json
import os
import time

import limen

from .gate import correct


def judge(candidate_dir, config_path):
    with open(config_path) as f:
        cfg = json.load(f)
    limen.param("time_limit_s", cfg["time_limit_s"])
    limen.param("strict", cfg["strict"])
    passed = 0
    names = sorted(n for n in os.listdir(candidate_dir) if n.endswith(".py"))
    for name in names:
        task = name[:-3]
        with open(os.path.join(candidate_dir, name)) as f:
            source = f.read()
        start = time.perf_counter()
        try:
            ok = correct(task, source) if cfg["strict"] else True
        except SyntaxError:
            limen.outcome(task, "error", "syntax")
            continue
        except Exception as e:
            limen.outcome(task, "error", type(e).__name__)
            continue
        if time.perf_counter() - start > cfg["time_limit_s"]:
            limen.outcome(task, "timeout")
            continue
        passed += ok
        limen.outcome(task, "pass" if ok else "fail")
    return passed / len(names) if names else 0.0
