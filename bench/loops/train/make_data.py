"""Deterministic synthetic data with a curved class boundary (squared features help)."""

import csv
import random


def make(path, n, seed):
    rng = random.Random(seed)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "x1", "x2", "y"])
        for i in range(n):
            x1, x2 = rng.uniform(-2, 2), rng.uniform(-2, 2)
            y = 1 if x1 * x1 + 0.5 * x2 - 1.0 + rng.gauss(0, 0.3) > 0 else 0
            w.writerow([f"{path.split('/')[-1][:-4]}-{i}", f"{x1:.4f}", f"{x2:.4f}", y])


if __name__ == "__main__":
    make("data/train.csv", 400, 1)
    make("data/val.csv", 120, 2)
    make("data/test.csv", 120, 3)
