"""The LLM device: one call interface, a persistent cache, replay, fakes and a cost ledger.

Every model call goes through ``Model.complete``. Live calls use the ``claude`` CLI in headless mode
with tools disabled, from an empty working directory, so the worker sees only the prompt. Calls are
cached by content, so a run can be replayed exactly and offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from aosr import config


class LiveCallsDisabled(RuntimeError):
    """Raised when a live model call is attempted while live calls are not enabled."""


class CacheMiss(KeyError):
    """Raised by ReplayModel when a call is not in the cache."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    status: str  # ok | timeout | infra_error
    key: str
    model_id: str
    latency_s: float = 0.0
    cli_usd: float = 0.0
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def usd(self) -> float:
        """Offline cost: recorded tokens times the pinned list price, no cache discount."""
        price_in, price_out = config.price(self.model_id)
        tokens_in = self.usage.input_tokens + self.usage.cache_read + self.usage.cache_write
        return (tokens_in * price_in + self.usage.output_tokens * price_out) / 1e6

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Completion:
        return cls(**{**d, "usage": Usage(**d["usage"])})


class Model(Protocol):
    name: str

    def complete(
        self, system: str, user: str, *, sample: int = 0, role: str = "worker", meta: dict[str, str] | None = None
    ) -> Completion: ...


def cache_key(model: str, thinking: bool, system: str, user: str, sample: int) -> str:
    blob = json.dumps([model, thinking, system, user, sample], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class CallCache:
    """A thread-safe SQLite store of completions keyed by ``cache_key``."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, completion TEXT, created REAL)")
        self._lock = threading.Lock()

    def get(self, key: str) -> Completion | None:
        with self._lock:
            row = self._db.execute("SELECT completion FROM calls WHERE key = ?", (key,)).fetchone()
        return Completion.from_json(json.loads(row[0])) if row else None

    def put(self, completion: Completion) -> Completion:
        """Store a completion unless one is already stored for its key; return the stored one."""
        blob = json.dumps(completion.to_json(), ensure_ascii=False)
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO calls VALUES (?, ?, ?)", (completion.key, blob, time.time()))
        return self.get(completion.key) or completion

    def keys(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT key FROM calls ORDER BY key")]

    def export_jsonl(self, keys: list[str], path: Path) -> None:
        with path.open("w") as f:
            for key in keys:
                c = self.get(key)
                if c is not None:
                    f.write(json.dumps(c.to_json(), ensure_ascii=False) + "\n")

    def import_jsonl(self, path: Path) -> int:
        n = 0
        for line in path.read_text().splitlines():
            if line.strip():
                self.put(Completion.from_json(json.loads(line)))
                n += 1
        return n


class Ledger:
    """Append-only JSON lines, one per model call (cached or live)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()

    def write(self, completion: Completion, role: str, meta: dict[str, str] | None) -> None:
        u = completion.usage
        rec = {
            "t": round(time.time(), 3),
            **(meta or {}),
            "role": role,
            "key": completion.key,
            "model_id": completion.model_id,
            "cached": completion.cached,
            "status": completion.status,
            "in": u.input_tokens + u.cache_read + u.cache_write,
            "out": u.output_tokens,
            "usd": round(completion.usd(), 6),
            "latency_s": round(completion.latency_s, 2),
        }
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")


_LIVE = threading.BoundedSemaphore(config.PARALLEL)  # concurrent live CLI processes


def live_allowed() -> bool:
    return os.environ.get("AOSR_OFFLINE") != "1" and os.environ.get("AOSR_ALLOW_LIVE") == "1"


@dataclass
class ClaudeCLI:
    """A live call to the ``claude`` CLI: headless, no tools, no session, isolated working directory."""

    model: str
    thinking: bool
    bin_path: str
    cwd: Path
    timeout_s: float = 120.0
    attempts: int = 3
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"{self.model}:{'think' if self.thinking else 'nothink'}"
        if not live_allowed():
            raise LiveCallsDisabled("set AOSR_ALLOW_LIVE=1 (and unset AOSR_OFFLINE) to make live model calls")

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "MAX_THINKING_TOKENS" and not k.startswith("CLAUDECODE")}
        if not self.thinking:
            env["MAX_THINKING_TOKENS"] = "0"
        return env

    def complete(
        self, system: str, user: str, *, sample: int = 0, role: str = "worker", meta: dict[str, str] | None = None
    ) -> Completion:
        if not live_allowed():
            raise LiveCallsDisabled("live model calls are disabled")
        key = cache_key(self.model, self.thinking, system, user, sample)
        argv = [
            self.bin_path,
            "-p",
            "--model",
            self.model,
            "--tools",
            "",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--system-prompt",
            system,
            "--output-format",
            "json",
        ]
        self.cwd.mkdir(parents=True, exist_ok=True)
        status = "infra_error"
        t0 = time.monotonic()
        for _ in range(self.attempts):
            try:
                with _LIVE:
                    proc = subprocess.run(
                        argv,
                        input=user,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout_s,
                        cwd=self.cwd,
                        env=self._env(),
                    )
            except subprocess.TimeoutExpired:
                status = "timeout"
                continue
            except OSError:
                status = "infra_error"
                break
            try:
                d = json.loads(proc.stdout)
            except json.JSONDecodeError:
                status = "infra_error"
                continue
            if d.get("is_error") or not isinstance(d.get("result"), str):
                status = "infra_error"
                continue
            u = d.get("usage") or {}
            usage = Usage(
                input_tokens=int(u.get("input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
                cache_read=int(u.get("cache_read_input_tokens", 0)),
                cache_write=int(u.get("cache_creation_input_tokens", 0)),
            )
            model_id = next(iter(d.get("modelUsage") or {}), self.model)
            return Completion(
                text=d["result"],
                usage=usage,
                status="ok",
                key=key,
                model_id=model_id,
                latency_s=time.monotonic() - t0,
                cli_usd=float(d.get("total_cost_usd") or 0.0),
            )
        return Completion("", Usage(), status, key, self.model, latency_s=time.monotonic() - t0)


class CachedModel:
    """Serve from the cache; on a miss call the inner model and store successful completions."""

    def __init__(
        self, inner: Model, cache: CallCache, *, model: str, thinking: bool, ledger: Ledger | None = None
    ) -> None:
        self.inner, self.cache, self.ledger = inner, cache, ledger
        self.model, self.thinking = model, thinking
        self.name = inner.name
        self.spec: str | None = None
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def complete(
        self, system: str, user: str, *, sample: int = 0, role: str = "worker", meta: dict[str, str] | None = None
    ) -> Completion:
        key = cache_key(self.model, self.thinking, system, user, sample)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # identical concurrent requests make one live call
            hit = self.cache.get(key)
            if hit is not None:
                c = Completion(**{**hit.__dict__, "cached": True})
            else:
                c = self.inner.complete(system, user, sample=sample, role=role, meta=meta)
                if c.ok:  # another process may have stored this call first; everyone uses the stored answer
                    stored = self.cache.put(c)
                    if stored.text != c.text:
                        c = Completion(**{**stored.__dict__, "cached": True})
        if self.ledger:
            self.ledger.write(c, role, meta)
        return c


class ReplayModel:
    """Serve only from the cache; a miss is an error. Used to replay a run offline."""

    def __init__(self, cache: CallCache, *, model: str, thinking: bool, ledger: Ledger | None = None) -> None:
        self.cache, self.model, self.thinking, self.ledger = cache, model, thinking, ledger
        self.name = f"replay:{model}:{'think' if thinking else 'nothink'}"
        self.spec: str | None = None
        self.calls = 0

    def complete(
        self, system: str, user: str, *, sample: int = 0, role: str = "worker", meta: dict[str, str] | None = None
    ) -> Completion:
        key = cache_key(self.model, self.thinking, system, user, sample)
        hit = self.cache.get(key)
        if hit is None:
            raise CacheMiss(key)
        self.calls += 1
        c = Completion(**{**hit.__dict__, "cached": True})
        if self.ledger:
            self.ledger.write(c, role, meta)
        return c


class FakeModel:
    """A deterministic in-process model for tests: ``fn(system, user, sample, role) -> text``."""

    def __init__(self, fn: Callable[[str, str, int, str], str], name: str = "fake") -> None:
        self.fn, self.name = fn, name
        self.calls = 0
        self._lock = threading.Lock()

    def complete(
        self, system: str, user: str, *, sample: int = 0, role: str = "worker", meta: dict[str, str] | None = None
    ) -> Completion:
        with self._lock:
            self.calls += 1
        text = self.fn(system, user, sample, role)
        usage = Usage(input_tokens=(len(system) + len(user)) // 4, output_tokens=len(text) // 4)
        return Completion(text, usage, "ok", cache_key(self.name, False, system, user, sample), self.name)


def make_model(spec: str, *, cache: CallCache, ledger: Ledger | None = None) -> Model:
    """Build a model from a spec such as ``haiku-nothink``, ``haiku-think`` or ``replay:haiku-nothink``."""
    replay = spec.startswith("replay:")
    base = spec.removeprefix("replay:")
    family, _, mode = base.partition("-")
    model = config.MODELS.get(family)
    if model is None or mode not in ("think", "nothink"):
        raise ValueError(f"unknown model spec {spec!r}")
    thinking = mode == "think"
    built: ReplayModel | CachedModel
    if replay:
        built = ReplayModel(cache, model=model, thinking=thinking, ledger=ledger)
    else:
        cli = ClaudeCLI(
            model,
            thinking,
            bin_path=config.claude_bin(),
            cwd=config.empty_cwd(),
            timeout_s=config.TIMEOUT_THINK_S if thinking else config.TIMEOUT_NOTHINK_S,
        )
        built = CachedModel(cli, cache, model=model, thinking=thinking, ledger=ledger)
    built.spec = spec  # lets worker processes rebuild the same model
    return built
