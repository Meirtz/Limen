from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


class Project:
    """A throwaway git project with a src-layout package, driven through the real CLI."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"))
        return p

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, env={**os.environ, **GIT_ENV})

    def commit(self) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", "snapshot")

    def limen(
        self, *args: str, env: dict[str, str] | None = None, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("LIMEN_")}
        full_env.update(GIT_ENV)
        full_env.update(env or {})
        out = subprocess.run(
            [sys.executable, "-m", "limen", *args],
            cwd=self.root,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if check and out.returncode != 0:
            raise AssertionError(f"limen {' '.join(args)} -> {out.returncode}\n{out.stdout}\n{out.stderr}")
        return out

    def runs(self) -> list[dict[str, Any]]:
        d = self.root / ".limen" / "runs"
        recs = [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
        return sorted(recs, key=lambda r: r["started_at"])

    def last(self) -> dict[str, Any]:
        return self.runs()[-1]


@pytest.fixture
def project(tmp_path: Path) -> Project:
    p = Project(tmp_path)
    p.git("init", "-q")
    p.write("src/pkg/__init__.py", "VALUE = 1\n")
    p.write("pyproject.toml", '[project]\nname = "pkg"\nversion = "0"\n')
    p.commit()
    return p


def record(**overrides: Any) -> dict[str, Any]:
    """A minimal finished run record for unit tests of checks and comparisons."""
    base: dict[str, Any] = {
        "schema": "limen.run/1",
        "id": overrides.pop("id", "r0"),
        "declared": {"name": "x", "arm": None, "role": None, "treatment": [], "gates": [], "protocol": {}},
        "status": "ok",
        "exit_code": 0,
        "started_at": "2026-01-01T00:00:00.000+00:00",
        "argv": ["main.py"],
        "cwd": "/p",
        "root": "/p",
        "python": {"version": "3.12.0", "implementation": "CPython"},
        "platform": {"system": "Linux", "machine": "x86_64", "gpus": []},
        "main": {"path": "/p/main.py", "sha256": "m", "origin": "project", "git": "clean"},
        "modules": {},
        "distributions": {},
        "imports": {"packages": {}, "sys_path": []},
        "env": {},
        "env_bulk_read": False,
        "files": {"read": {}, "written": {}},
        "subprocesses": [],
        "params": {},
        "metrics": {},
        "outcomes": {},
        "gates": {},
        "policy": {"holdout": [], "holdout_readers": ["eval"]},
    }
    declared = overrides.pop("declared", {})
    base["declared"].update(declared)
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _aosr_offline(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """AOSR tests never call a live model and never touch the user's caches."""
    home = tmp_path_factory.mktemp("aosr_home")
    monkeypatch.setenv("AOSR_HOME", str(home))
    monkeypatch.setenv("AOSR_OFFLINE", "1")
    for var in ("AOSR_ALLOW_LIVE", "AOSR_UNLOCK_EVAL", "AOSR_DATA", "AOSR_CACHE", "AOSR_SANDBOX_CACHE"):
        monkeypatch.delenv(var, raising=False)
    real_popen = subprocess.Popen

    def guarded(argv: Any, *args: Any, **kwargs: Any) -> Any:
        first = argv[0] if isinstance(argv, list | tuple) and argv else str(argv).split(" ")[0]
        if Path(str(first)).name == "claude" and not str(first).startswith(str(home.parent)):
            raise RuntimeError("live claude call in tests")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)
