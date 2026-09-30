"""Summarize bench/results/results.json as the tables used in the README."""

from __future__ import annotations

import json
import sys
from pathlib import Path

DETECTORS = [("none", "no checks"), ("mlflow", "MLflow-style"), ("sacred", "Sacred-style"), ("limen", "Limen")]


def main(path: str) -> None:
    data = json.loads(Path(path).read_text())
    rows = data["rows"]
    print("| scenarios | detector | invalid comparisons caught | valid comparisons flagged |")
    print("|---|---|---|---|")
    for subset in ("dev", "heldout"):
        rs = [r for r in rows if r["set"] == subset]
        faults = [r for r in rs if r["kind"] == "fault"]
        valid = [r for r in rs if r["kind"] == "valid"]
        name = {"dev": "development (in-sample)", "heldout": "held-out"}[subset]
        for key, label in DETECTORS:
            caught = sum(r["detectors"][key] for r in faults)
            flagged = sum(r["detectors"][key] for r in valid)
            print(f"| {name} | {label} | {caught} / {len(faults)} | {flagged} / {len(valid)} |")
    print()
    for subset in ("heldout",):
        rs = [r for r in rows if r["set"] == subset]
        missed = [r for r in rs if r["kind"] == "fault" and not r["detectors"]["limen"]]
        only = [
            r
            for r in rs
            if r["kind"] == "fault"
            and r["detectors"]["limen"]
            and not r["detectors"]["mlflow"]
            and not r["detectors"]["sacred"]
        ]
        wrong = [r for r in rs if r["kind"] == "valid" and r["detectors"]["limen"]]
        print(f"held-out faults caught only by Limen: {len(only)}")
        print("held-out faults Limen missed:")
        for r in missed:
            warns = ", ".join(r["limen_warn"]) or "no warning"
            print(f"- {r['scenario']}: {r['fault_class']} ({warns})")
        print("held-out valid comparisons Limen flagged:")
        for r in wrong:
            print(f"- {r['scenario']}: {', '.join(r['limen_block'])}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "results" / "results.json"))
