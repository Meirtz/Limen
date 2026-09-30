"""Project configuration: where the project's code lives and which files are held out.

Configuration comes from ``[tool.limen]`` in ``pyproject.toml`` or from ``limen.toml`` at the
project root, then from ``LIMEN_*`` environment variables. Everything has a default, so a
project with no configuration still works.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_toml: Any
try:  # tomllib on Python 3.11+; tomli, if installed, on 3.10; otherwise no config files
    _toml = importlib.import_module("tomllib")
except ModuleNotFoundError:  # pragma: no cover
    try:
        _toml = importlib.import_module("tomli")
    except ModuleNotFoundError:
        _toml = None

DEFAULT_MAX_HASH_BYTES = 64 * 1024 * 1024


@dataclass
class Config:
    root: Path
    source_roots: list[Path]
    packages: dict[str, Path]
    store: Path
    holdout: list[Path] = field(default_factory=list)
    holdout_readers: list[str] = field(default_factory=lambda: ["eval"])
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES
    waive: list[str] = field(default_factory=list)

    def relpath(self, path: str | os.PathLike[str]) -> str:
        """``path`` relative to the project root when it lies inside it, else absolute."""
        p = Path(os.path.abspath(path))
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def in_source_roots(self, path: str) -> bool:
        p = Path(os.path.abspath(path))
        return any(p == r or r in p.parents for r in self.source_roots)


def find_root(start: Path | None = None) -> Path:
    """The git toplevel containing ``start`` (default: cwd), else ``start`` itself."""
    start = Path(os.path.abspath(start or os.getcwd()))
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def _read_toml(root: Path) -> dict[str, Any]:
    if _toml is None:
        return {}
    limen_toml = root / "limen.toml"
    if limen_toml.is_file():
        with limen_toml.open("rb") as f:
            return dict(_toml.load(f))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        with pyproject.open("rb") as f:
            data = _toml.load(f)
        return dict(data.get("tool", {}).get("limen", {}))
    return {}


def _discover_packages(source_roots: list[Path]) -> dict[str, Path]:
    """Top-level packages found directly in each source root or its ``src/`` directory.

    A ``src/`` copy wins over a same-named directory elsewhere, because that is where a
    src-layout project's real code lives.
    """
    found: dict[str, Path] = {}
    for root in source_roots:
        for base in (root / "src", root):
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / "__init__.py").is_file():
                    found.setdefault(child.name, child.resolve())
    return found


def load(start: Path | None = None) -> Config:
    root = Path(os.environ["LIMEN_ROOT"]).resolve() if os.environ.get("LIMEN_ROOT") else find_root(start)
    cfg = _read_toml(root)

    source_roots = [(root / p).resolve() for p in cfg.get("source_roots", ["."])]
    packages = _discover_packages(source_roots)
    for name, rel in dict(cfg.get("packages", {})).items():
        packages[name] = (root / rel).resolve()

    store_env = os.environ.get("LIMEN_HOME")
    store = Path(store_env).resolve() if store_env else (root / cfg.get("store", ".limen")).resolve()

    raw_holdout = [root / p for p in cfg.get("holdout", [])]
    if os.environ.get("LIMEN_HOLDOUT"):
        raw_holdout += [Path(p) for p in os.environ["LIMEN_HOLDOUT"].split(os.pathsep) if p]
    holdout: list[Path] = []
    for h in raw_holdout:  # both the path as written and the file it resolves to (symlinked data dirs)
        for form in (Path(os.path.abspath(h)), h.resolve()):
            if form not in holdout:
                holdout.append(form)

    return Config(
        root=root,
        source_roots=source_roots,
        packages=packages,
        store=store,
        holdout=holdout,
        holdout_readers=list(cfg.get("holdout_readers", ["eval"])),
        max_hash_bytes=int(cfg.get("max_hash_bytes", DEFAULT_MAX_HASH_BYTES)),
        waive=[str(w) for w in cfg.get("waive", [])],
    )
