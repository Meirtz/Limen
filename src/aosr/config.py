"""Paths, pinned prices and run defaults. Everything can be overridden through ``AOSR_*`` variables."""

from __future__ import annotations

import os
from pathlib import Path

MODELS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}

# list prices in USD per million tokens (input, output); pinned so costs are reproducible offline
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (5.0, 25.0),
}

TIMEOUT_NOTHINK_S = 120.0
TIMEOUT_THINK_S = 600.0
PARALLEL = int(os.environ.get("AOSR_PARALLEL", "10"))  # concurrent live model calls

ALLOWED_IMPORTS = (
    "bisect",
    "collections",
    "copy",
    "dataclasses",
    "enum",
    "functools",
    "heapq",
    "itertools",
    "math",
    "operator",
    "re",
    "statistics",
    "string",
    "typing",
)


def _path(var: str, default: str) -> Path:
    return Path(os.environ.get(var, default)).expanduser()


def home() -> Path:
    return _path("AOSR_HOME", "~/.cache/aosr")


def data_dir() -> Path:
    return _path("AOSR_DATA", str(home() / "data"))


def appworld_root() -> Path:
    return _path("APPWORLD_ROOT", str(home() / "awroot"))


def llm_cache_path() -> Path:
    return _path("AOSR_CACHE", str(home() / "llm.sqlite"))


def sandbox_cache_path() -> Path:
    return _path("AOSR_SANDBOX_CACHE", str(home() / "sandbox.sqlite"))


def empty_cwd() -> Path:
    return _path("AOSR_EMPTY_CWD", str(home() / "empty"))


def claude_bin() -> str:
    return str(_path("AOSR_CLAUDE_BIN", "~/.local/bin/claude"))


def price(model_id: str) -> tuple[float, float]:
    for name, p in PRICES.items():
        if model_id.startswith(name):
            return p
    return (0.0, 0.0)
