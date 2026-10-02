"""Paired statistics over per-item outcomes: bootstrap intervals, a log-trend slope test and Holm.

Arms are always compared on the same items, so resampling is over items and keeps the pairing.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float
    p_le_zero: float  # one-sided bootstrap p-value for "the effect is <= 0"
    n: int

    def __str__(self) -> str:
        return f"{self.estimate:+.3f} [{self.low:+.3f}, {self.high:+.3f}] (n={self.n}, p={self.p_le_zero:.3f})"


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _quantile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    pos = q * (len(sorted_xs) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def paired_diff(
    a: Sequence[float], b: Sequence[float], *, level: float = 0.95, resamples: int = 10_000, seed: int = 0
) -> Interval:
    """Bootstrap interval for mean(b - a) over paired items."""
    if len(a) != len(b):
        raise ValueError("arms must be scored on the same items")
    d = [y - x for x, y in zip(a, b, strict=True)]
    n = len(d)
    if n == 0:
        return Interval(0.0, 0.0, 0.0, 1.0, 0)
    rng = random.Random(seed)
    boots = sorted(mean([d[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    alpha = (1 - level) / 2
    p = sum(1 for x in boots if x <= 0) / resamples
    return Interval(mean(d), _quantile(boots, alpha), _quantile(boots, 1 - alpha), p, n)


def slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx else 0.0


def log_trend(
    positions: Sequence[int],
    outcomes: Sequence[Sequence[float]],
    *,
    level: float = 0.95,
    resamples: int = 10_000,
    seed: int = 0,
) -> Interval:
    """Slope of accuracy against log2(1 + position); ``outcomes[k][i]`` is item i at checkpoint k."""
    xs = [math.log2(1 + p) for p in positions]
    n = len(outcomes[0]) if outcomes else 0
    if n == 0:
        return Interval(0.0, 0.0, 0.0, 1.0, 0)

    def fit(items: list[int]) -> float:
        return slope(xs, [mean([row[i] for i in items]) for row in outcomes])

    rng = random.Random(seed)
    boots = sorted(fit([rng.randrange(n) for _ in range(n)]) for _ in range(resamples))
    alpha = (1 - level) / 2
    p = sum(1 for x in boots if x <= 0) / resamples
    return Interval(fit(list(range(n))), _quantile(boots, alpha), _quantile(boots, 1 - alpha), p, n)


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values."""
    order = sorted(pvalues, key=pvalues.__getitem__)
    m = len(order)
    out: dict[str, float] = {}
    running = 0.0
    for k, name in enumerate(order):
        running = max(running, min(1.0, (m - k) * pvalues[name]))
        out[name] = running
    return out


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)
