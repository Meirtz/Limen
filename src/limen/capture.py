"""Record what a Python process actually executed.

A :class:`Recorder` observes, without changing behaviour:

- which environment variables the process read (the ``os.environ`` object is instrumented in place),
- which files it read, listed, created and wrote (PEP 578 audit events),
- which child processes and workers it started,
- which code it executed: every project module and the main script, hashed when it was loaded,
  with its location and git state, and flagged if the file changed while the run was going,
- the outcomes, metrics, parameters and gate results the experiment reports.

Blind spots, by construction: reads made by child processes and workers, by native code that
opens files itself (Arrow/Parquet datasets, safetensors, HDF5), and by other processes. The record
lists the child processes and native readers it saw, so these gaps are visible.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.machinery
import importlib.util
import numbers
import os
import platform
import shutil
import site
import subprocess
import sys
import sysconfig
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

from . import redact
from ._version import __version__
from .config import Config
from .store import SCHEMA, Store, new_run_id

NOT_EVALUATED = ("error", "timeout", "infra", "skip")
STATUSES = ("pass", "fail", *NOT_EVALUATED)

_MAX_ENV_VALUE = 256
_LIMEN_DIR = str(Path(__file__).resolve().parent)
_STDLIB_NAMES = frozenset(getattr(sys, "stdlib_module_names", ()))
_CODE_SUFFIXES = (*importlib.machinery.all_suffixes(), ".pyc")
_IMPORT_MODULES = frozenset(
    {
        "importlib._bootstrap",
        "importlib._bootstrap_external",
        "_frozen_importlib",
        "_frozen_importlib_external",
        "zipimport",
    }
)
# modules that read files through native code, invisible to the audit hooks
NATIVE_READERS = (
    "pyarrow.parquet",
    "pyarrow.dataset",
    "pyarrow.csv",
    "pyarrow.feather",
    "datasets",
    "h5py",
    "safetensors",
    "lmdb",
    "zarr",
    "tensorstore",
    "tables",
)
_MTIME_SLACK_NS = 10_000_000

_active: Recorder | None = None
_local = threading.local()
_install_lock = threading.Lock()
_installed: set[str] = set()


def active() -> Recorder | None:
    """The recorder observing this process, if any."""
    rec = _active
    if rec is not None and rec.pid != os.getpid():
        return None  # a forked child does not report into its parent's record
    return rec


def _busy() -> bool:
    return bool(getattr(_local, "busy", False))


class _internal:
    """Suspend observation while Limen itself touches files or starts processes."""

    def __enter__(self) -> None:
        self.prev = _busy()
        _local.busy = True

    def __exit__(self, *exc: object) -> None:
        _local.busy = self.prev


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


# ---- identities --------------------------------------------------------------------------------


def file_identity(path: str, max_bytes: int, *, git_blob: bool = False) -> dict[str, Any] | None:
    """Content identity of a regular file: sha256 when small enough, else size+mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    ident: dict[str, Any] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if st.st_size > max_bytes:
        ident["stat"] = f"{st.st_size}:{st.st_mtime_ns}"
        return ident
    try:
        with _internal(), open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    ident["sha256"] = hashlib.sha256(data).hexdigest()
    if git_blob:
        ident["git_blob"] = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return ident


def dir_identity(path: str, max_entries: int = 10000) -> dict[str, Any] | None:
    """Identity of a directory listing: entry names and kinds (contents are identified per file)."""
    try:
        with _internal(), os.scandir(path) as it:
            entries = []
            for n, e in enumerate(it):
                if n >= max_entries:
                    return {"entries": n, "stat": f"dir:{n}+", "dir": True}
                try:
                    kind = "d" if e.is_dir() else "f"
                except OSError:
                    kind = "?"
                entries.append(f"{e.name}\t{kind}")
    except OSError:
        return None
    h = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8", "surrogateescape")).hexdigest()
    return {"entries": len(entries), "sha256": h, "dir": True}


def identity_token(ident: dict[str, Any] | None) -> str:
    if not ident:
        return "<missing>"
    if ident.get("sha256"):
        return str(ident["sha256"])
    if ident.get("stat"):
        return f"stat:{ident['stat']}"
    return "<missing>"


class _Git:
    """The git state of the work trees that contain executed project code."""

    def __init__(self, heads: dict[str, str | None] | None = None) -> None:
        self.toplevel_of: dict[str, str | None] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.heads = dict(heads or {})

    def _run(self, *args: str) -> str | None:
        try:
            with _internal():
                out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    def toplevel(self, directory: str) -> str | None:
        if directory not in self.toplevel_of:
            out = self._run("-C", directory, "rev-parse", "--show-toplevel")
            self.toplevel_of[directory] = os.path.realpath(out.strip()) if out else None
        return self.toplevel_of[directory]

    def head(self, directory: str) -> str | None:
        return (self._run("-C", directory, "rev-parse", "HEAD") or "").strip() or None

    def status_of(self, path: str, blob: str | None) -> str:
        """``clean``/``modified`` against the HEAD the run started from, ``untracked``, or ``no-git``."""
        top = self.toplevel(os.path.dirname(os.path.realpath(path)))
        if top is None:
            return "no-git"
        if top not in self.trees:
            head = self.heads.get(top) or self.head(top)
            listing = self._run("-C", top, "ls-tree", "-r", "-z", head or "HEAD") or ""
            tree: dict[str, str] = {}
            for entry in listing.split("\0"):
                if "\t" in entry:
                    meta, rel = entry.split("\t", 1)
                    parts = meta.split()
                    if len(parts) == 3 and parts[1] == "blob":
                        tree[rel] = parts[2]
            self.trees[top] = tree
            dirty = bool((self._run("-C", top, "status", "--porcelain", "--untracked-files=no") or "").strip())
            self.state[top] = {"head": head, "dirty_at_exit": dirty}
        rel = os.path.relpath(os.path.realpath(path), top).replace(os.sep, "/")
        tracked = self.trees[top].get(rel)
        if tracked is None:
            return "untracked"
        return "clean" if blob == tracked else "modified"


# ---- environment -------------------------------------------------------------------------------


def _note(key: object, value: str | None) -> None:
    rec = active()
    if rec is not None and isinstance(key, str) and not _busy():
        rec._on_env_read(key, value)


def _end_scan(bulk: bool) -> None:
    """Close an ``os.environ`` key scan. If every key was read back in order it was a bulk copy
    (``dict(os.environ)``, ``.items()``); otherwise the values it read were ordinary reads."""
    provisional = getattr(_local, "provisional", None)
    scan_by = getattr(_local, "scan_by", None)
    _local.pending = _local.provisional = _local.scan_by = None
    rec = active()
    if rec is None or _busy():
        return
    if bulk:
        rec._on_env_bulk(scan_by)
    else:
        for key, value in provisional or []:
            rec._on_env_read(key, value)


def _caller_module() -> str:
    frame: FrameType | None = sys._getframe(2)
    while frame is not None:
        name = str(frame.f_globals.get("__name__", ""))
        if name not in ("os", "_collections_abc", "collections.abc", __name__):
            return name
        frame = frame.f_back
    return "?"


class _TrackedEnviron(os._Environ):  # type: ignore[type-arg]
    """``os._Environ`` that tells the active recorder which variables were read or set.

    Installed by swapping the class of the existing ``os.environ`` object, so references taken
    before recording started (``from os import environ``) are observed too.
    """

    def __getitem__(self, key: str) -> str:
        pending = getattr(_local, "pending", None)
        if pending is not None:
            if pending and pending[0] == key:  # maybe a bulk copy replaying every key in order
                pending.popleft()
                value: str = super().__getitem__(key)
                _local.provisional.append((key, value))
                if not pending:
                    _end_scan(bulk=True)
                return value
            _end_scan(bulk=False)
        try:
            value = super().__getitem__(key)
        except KeyError:
            _note(key, None)
            raise
        _note(key, value)
        return value

    def __setitem__(self, key: str, value: str) -> None:
        rec = active()
        if rec is not None and not _busy():
            rec._on_env_set(key)
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        rec = active()
        if rec is not None and not _busy():
            rec._on_env_set(key)
        super().__delitem__(key)

    def __iter__(self) -> Iterator[str]:
        if getattr(_local, "pending", None) is not None:
            _end_scan(bulk=False)
        keys = list(super().__iter__())
        if active() is not None and not _busy():
            _local.pending = deque(keys)
            _local.provisional = []
            _local.scan_by = _caller_module()
        return iter(keys)


def _install_env_tracking() -> bool:
    if type(os.environ) is _TrackedEnviron:
        return True
    if type(os.environ) is os._Environ:
        os.environ.__class__ = _TrackedEnviron
        return True
    return False  # os.environ was replaced by something else: environment reads are not observed


def _env_entry(name: str, value: str | None, key: bytes) -> dict[str, Any]:
    if value is None:
        return {"present": False}
    if redact.is_secret(name, value) or len(value) > _MAX_ENV_VALUE:
        return {"present": True, "digest": redact.digest(key, value)}
    return {"present": True, "value": value}


# ---- values reported by user code -------------------------------------------------------------


def _plain(value: Any) -> Any:
    """numpy/torch scalars as Python numbers; everything else unchanged."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        return float(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            v = item()
        except (TypeError, ValueError, RuntimeError):
            return value
        if isinstance(v, (bool, int, float)):
            return v
    return value


def _jsonable(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy (string keys, lists for tuples/sets, ``str`` for anything else)."""
    value = _plain(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 32:
        return repr(value)[:200]
    if isinstance(value, dict):
        return {k if isinstance(k, str) else str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in value]
    return str(value)


# ---- audit events ------------------------------------------------------------------------------


def _is_import_frame(frame: FrameType) -> bool:
    code = frame.f_code.co_filename
    return (
        code.startswith(("<frozen importlib", "<frozen zipimport"))
        or frame.f_globals.get("__name__") in _IMPORT_MODULES
    )


def _from_import_system() -> bool:
    """Was the event issued by the import machinery (loading code, filling finder caches)?

    Only the frames that issued the event count: code running at import time, ``pkgutil.get_data``
    and ``importlib.resources`` reads are ordinary reads. ``importlib.metadata`` scans are ignored.
    """
    frame: FrameType | None = sys._getframe(3)
    names = []
    while frame is not None and _is_import_frame(frame):
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    if any(n not in ("get_data", "_get_data") for n in names):
        return True
    for _ in range(6):
        if frame is None:
            break
        if str(frame.f_globals.get("__name__", "")).startswith("importlib.metadata"):
            return True
        frame = frame.f_back
    return False


def _path(arg: Any) -> str | None:
    if arg is None or isinstance(arg, int):
        return None
    try:
        return os.path.abspath(os.fsdecode(arg))
    except (TypeError, ValueError):
        return None


def _ev_open(rec: Recorder, args: tuple[Any, ...]) -> None:
    raw, mode, flags = (*tuple(args), None, None, None)[:3]
    path = _path(raw)
    if path is None:
        return
    if _from_import_system():
        if path.endswith(_CODE_SUFFIXES):
            rec._on_code(path)
        return
    if mode is None:
        fl = flags or 0
        writes = bool(fl & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
        reads = (fl & os.O_ACCMODE) != os.O_WRONLY and not fl & os.O_TRUNC
    else:
        m = str(mode)
        writes = any(c in m for c in "wax+")
        reads = "r" in m or ("a" in m and "+" in m)
    rec._on_open(path, writes=writes, reads=reads)


def _ev_listdir(rec: Recorder, args: tuple[Any, ...]) -> None:
    path = _path(args[0] if args else ".")
    if path is not None and not _from_import_system():
        rec._on_listdir(path)


def _ev_glob(rec: Recorder, args: tuple[Any, ...]) -> None:
    pattern = args[0] if args else None
    if not pattern or _from_import_system():
        return
    try:
        pattern = os.fsdecode(pattern)
    except (TypeError, ValueError):
        return
    base = pattern.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
    rec._on_listdir(os.path.abspath(os.path.dirname(base) or "."))


def _ev_created(index: int, only_new: bool = False) -> Any:
    """``os.mkdir`` (fires before the call: skip existing directories) and ``os.rename``/``os.replace``."""

    def handler(rec: Recorder, args: tuple[Any, ...]) -> None:
        path = _path(args[index] if len(args) > index else None)
        if path is None or (only_new and os.path.exists(path)) or _from_import_system():
            return
        rec._on_created(path)

    return handler


def _ev_sqlite(rec: Recorder, args: tuple[Any, ...]) -> None:
    db = args[0] if args else None
    try:
        name = os.fsdecode(db) if db is not None and not isinstance(db, int) else ""
    except (TypeError, ValueError):
        return
    if name.startswith("file:"):
        name = name[5:].split("?", 1)[0]
    if name and name != ":memory:":
        rec._on_open(os.path.abspath(name), writes=False, reads=True)


def _ev_popen(rec: Recorder, args: tuple[Any, ...]) -> None:
    executable, argv, cwd, env = (*tuple(args), None, None, None, None)[:4]
    rec._on_spawn(argv if argv is not None else executable, cwd, env)


def _ev_system(rec: Recorder, args: tuple[Any, ...]) -> None:
    rec._on_spawn(args[0] if args else None, None, None)


def _ev_exec(rec: Recorder, args: tuple[Any, ...]) -> None:
    path, argv, env = (*tuple(args), None, None, None)[:3]
    rec._on_spawn(argv if argv is not None else path, None, env)


def _ev_spawn(rec: Recorder, args: tuple[Any, ...]) -> None:
    _mode, path, argv, env = (*tuple(args), None, None, None, None)[:4]
    rec._on_spawn(argv if argv is not None else path, None, env)


def _ev_fork(rec: Recorder, args: tuple[Any, ...]) -> None:
    rec._on_spawn(["<fork>"], None, None)


_EVENTS = {
    "open": _ev_open,
    "os.listdir": _ev_listdir,
    "os.scandir": _ev_listdir,
    "glob.glob": _ev_glob,
    "os.mkdir": _ev_created(0, only_new=True),
    "os.rename": _ev_created(1),
    "sqlite3.connect": _ev_sqlite,
    "subprocess.Popen": _ev_popen,
    "os.system": _ev_system,
    "os.exec": _ev_exec,
    "os.posix_spawn": _ev_exec,
    "os.spawn": _ev_spawn,
    "os.fork": _ev_fork,
}


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in _EVENTS:
        return
    rec = _active
    if rec is None or _busy() or rec.pid != os.getpid():
        return
    try:
        _local.busy = True
        _EVENTS[event](rec, args)
    except Exception:  # never let observation break the experiment
        pass
    finally:
        _local.busy = False


def _install_hooks() -> None:
    with _install_lock:
        if "audit" not in _installed:
            sys.addaudithook(_audit_hook)
            _installed.add("audit")
        if "fork_exec" not in _installed:
            _installed.add("fork_exec")
            try:
                import _posixsubprocess
            except ImportError:  # pragma: no cover - not POSIX
                return
            original = _posixsubprocess.fork_exec

            def fork_exec(*args: Any, **kwargs: Any) -> Any:
                """Workers started without subprocess.Popen (multiprocessing spawn, loky) raise no audit event."""
                rec = _active
                if rec is not None and not _busy() and rec.pid == os.getpid():
                    try:
                        if sys._getframe(1).f_globals.get("__name__") != "subprocess":
                            _local.busy = True
                            rec._on_spawn(
                                args[0] if args else None,
                                args[4] if len(args) > 4 else None,
                                args[5] if len(args) > 5 else None,
                            )
                    except Exception:
                        pass
                    finally:
                        _local.busy = False
                return original(*args, **kwargs)

            _posixsubprocess.fork_exec = fork_exec


# ---- where things live -------------------------------------------------------------------------


def _both(dirs: set[str]) -> list[str]:
    return sorted({os.path.realpath(d) for d in dirs if d} | {d for d in dirs if d})


def _site_dirs() -> list[str]:
    dirs = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}
    try:
        dirs.update(site.getsitepackages())
    except AttributeError:  # pragma: no cover
        pass
    if hasattr(site, "getusersitepackages"):
        dirs.add(site.getusersitepackages())
    return _both(dirs)


def _stdlib_dirs() -> list[str]:
    return _both({sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]})


def _interpreter_dirs(root: Path) -> list[str]:
    """Directories whose files belong to the interpreter or installed packages, not the project."""
    dirs = set(_site_dirs()) | set(_stdlib_dirs())
    for prefix in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}:
        rp = Path(prefix).resolve()
        if not (root == rp or rp in root.parents):  # never exclude a project that lives in the prefix
            dirs.add(str(rp))
    return _both(dirs)


def _under(path: str, dirs: list[str]) -> bool:
    return any(path == d or path.startswith(d.rstrip(os.sep) + os.sep) for d in dirs)


def _gpus() -> list[str]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        with _internal():
            out = subprocess.run(
                [exe, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()] if out.returncode == 0 else []


# ---- the recorder ------------------------------------------------------------------------------


class Recorder:
    def __init__(
        self,
        config: Config,
        *,
        name: str | None = None,
        arm: str | None = None,
        role: str | None = None,
        treatment: list[str] | tuple[str, ...] = (),
        gates: list[str] | tuple[str, ...] = (),
        protocol: dict[str, Any] | None = None,
        argv: list[str] | None = None,
        main: str | None = None,
    ) -> None:
        self.config = config
        self.store = Store(config.store)
        self.pid = os.getpid()
        self.lock = threading.RLock()
        self.id = new_run_id()
        self.started = time.time()
        self.started_ns = time.time_ns()
        self.argv = list(argv if argv is not None else sys.argv)
        self.main = os.path.abspath(main) if main else None
        self.declared: dict[str, Any] = {
            "name": name,
            "arm": arm,
            "role": role,
            "treatment": list(treatment),
            "gates": list(gates),
            "protocol": _jsonable(dict(protocol or {})),
        }
        self._key = b""
        self.env_tracked = True
        self.env_reads: dict[str, dict[str, Any]] = {}
        self.env_set: set[str] = set()
        self.env_bulk_read = False
        self.env_bulk_by: set[str] = set()
        self.declared_env: dict[str, dict[str, Any]] = {}
        self.files_read: dict[str, dict[str, Any]] = {}
        self.files_written: set[str] = set()
        self.code_seen: dict[str, dict[str, Any]] = {}
        self.main_ident: dict[str, Any] | None = None
        self.heads: dict[str, str | None] = {}
        self.git_at_start: dict[str, dict[str, Any]] = {}
        self.subprocesses: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.params: dict[str, Any] = {}
        self.metrics: dict[str, Any] = {}
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.duplicate_outcomes = 0
        self.gates: dict[str, dict[str, Any]] = {}
        self._excluded = [
            *_interpreter_dirs(config.root),
            str(config.store),
            os.path.realpath(config.store),
            _LIMEN_DIR,
            "/dev",
            "/proc",
            "/sys",
        ]
        self.finished = False

    # ---- lifecycle ------------------------------------------------------------------------------

    def start(self) -> Recorder:
        global _active
        if _active is not None and _active is not self and _active.pid == os.getpid() and not _active.finished:
            raise RuntimeError("a Limen run is already being recorded in this process")
        with _internal():
            self._key = self.store.key()
            self.env_tracked = _install_env_tracking()
            _install_hooks()
            if self.main and os.path.isfile(self.main):
                self.main_ident = file_identity(self.main, self.config.max_hash_bytes, git_blob=True)
                if self.main_ident and self.main_ident.get("sha256"):
                    self.store.put_object(self.main, self.main_ident["sha256"])
            git = _Git()
            for d in {str(self.config.root), *(str(r) for r in self.config.source_roots)}:
                top = git.toplevel(d)
                if top and top not in self.heads:
                    self.heads[top] = git.head(top)
                    status = git._run("-C", top, "status", "--porcelain", "--untracked-files=no")
                    self.git_at_start[top] = {"head": self.heads[top], "dirty": bool((status or "").strip())}
            self._snapshot_declared_env()
            _active = self
            self.store.save(self._record(status="running"))
        return self

    def finish(
        self,
        status: str,
        exit_code: int | None = None,
        exception: str | None = None,
        signal_name: str | None = None,
    ) -> dict[str, Any]:
        global _active
        if self.finished:
            raise RuntimeError("run already finished")
        self.finished = True
        if os.getpid() != self.pid:  # a forked child must never overwrite its parent's record
            return {}
        if getattr(_local, "pending", None) is not None:
            _end_scan(bulk=False)
        if _active is self:
            _active = None
        with _internal():
            record = self._record(status=status, exit_code=exit_code, exception=exception, signal_name=signal_name)
            self._snapshot(record)
            self.store.save(record)
        return record

    # ---- observation callbacks (called with observation suspended) ----------------------------

    def _on_env_read(self, name: str, value: str | None) -> None:
        with self.lock:
            if name in self.env_reads:
                return
            entry = _env_entry(name, value, self._key)
            if name in self.env_set:  # set by the run before it was ever read: not an input
                entry["set_by_run"] = True
            self.env_reads[name] = entry

    def _on_env_set(self, name: str) -> None:
        with self.lock:
            self.env_set.add(name)

    def _on_env_bulk(self, by: str | None) -> None:
        with self.lock:
            self.env_bulk_read = True
            if by:
                self.env_bulk_by.add(by)

    def _snapshot_declared_env(self) -> None:
        """The values the declared env treatments had when the run started, read or not."""
        raw = dict(os.environ.items()) if not self.env_tracked else dict(os._Environ.items(os.environ))
        for spec in self.declared["treatment"]:
            if not spec.startswith("env:"):
                continue
            name = spec[len("env:") :]
            names = [k for k in raw if k.startswith(name[:-1])] if name.endswith("*") else [name]
            for n in names or [name]:
                if not n.endswith("*"):
                    self.declared_env.setdefault(n, _env_entry(n, raw.get(n), self._key))

    def _excluded_path(self, path: str) -> bool:
        return path.endswith(".pyc") or "__pycache__" in path or _under(path, self._excluded)

    def _on_open(self, path: str, *, writes: bool, reads: bool = True) -> None:
        if self._excluded_path(path):
            return
        with self.lock:
            if not reads or path in self.files_read:
                if writes:
                    self.files_written.add(path)
                return
        ident = file_identity(path, self.config.max_hash_bytes)  # content before any write
        with self.lock:
            if ident is not None and path not in self.files_read:
                if path in self.files_written:
                    ident["self_written"] = True
                elif ident.get("mtime_ns", 0) >= self.started_ns - _MTIME_SLACK_NS:
                    ident["self_written"] = True  # created or changed during the run, e.g. by torch.save
                    ident["by"] = "mtime"
                self.files_read[path] = ident
            if writes:
                self.files_written.add(path)

    def _on_code(self, path: str) -> None:
        """The import system loaded code from ``path``: remember the bytes that executed."""
        if path.endswith(".pyc"):
            try:
                path = importlib.util.source_from_cache(path)
            except (ValueError, NotImplementedError):
                return
        if self._excluded_path(path) or path in self.code_seen:
            return
        ident = file_identity(path, self.config.max_hash_bytes, git_blob=True)
        if ident is None:
            return
        with self.lock:
            self.code_seen.setdefault(path, ident)
        if ident.get("sha256"):
            self.store.put_object(path, ident["sha256"])

    def _on_listdir(self, path: str) -> None:
        if self._excluded_path(path):
            return
        key = path.rstrip(os.sep) + os.sep
        with self.lock:
            if key in self.files_read:
                return
        ident = dir_identity(path)
        if ident is None:
            return
        with self.lock:
            self.files_read.setdefault(key, ident)

    def _on_created(self, path: str) -> None:
        if not self._excluded_path(path):
            with self.lock:
                self.files_written.add(path)

    def _on_spawn(self, argv: Any, cwd: Any, env: Any) -> None:
        if isinstance(argv, (str, bytes, os.PathLike)):
            cmd = [os.fsdecode(argv)]
        else:
            try:
                cmd = [os.fsdecode(a) for a in argv]
            except TypeError:
                cmd = [repr(argv)]
        cmd = redact.argv(self._key, [c[:500] for c in cmd[:50]])
        entry = {
            "argv": cmd,
            "cwd": os.fsdecode(cwd) if cwd else None,
            "env": "inherited" if env is None else "explicit",
        }
        k = (tuple(cmd), entry["cwd"], entry["env"])
        with self.lock:
            if k in self.subprocesses:
                self.subprocesses[k]["count"] += 1
            elif len(self.subprocesses) < 500:
                self.subprocesses[k] = {**entry, "count": 1}

    # ---- values reported by the experiment ------------------------------------------------------

    def outcome(self, item: Any, status: str, value: Any = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"outcome status must be one of {STATUSES}, got {status!r}")
        with self.lock:
            key = str(_plain(item))
            if key in self.outcomes:
                self.duplicate_outcomes += 1
            self.outcomes[key] = {"status": status} if value is None else {"status": status, "value": _jsonable(value)}

    def metric(self, name: str, value: Any) -> None:
        with self.lock:
            self.metrics[str(name)] = _jsonable(value)

    def param(self, name: str, value: Any) -> None:
        with self.lock:
            self.params[str(name)] = _jsonable(value)

    def declare(self, **fields: Any) -> None:
        with self.lock:
            for key, value in fields.items():
                if key in ("treatment", "gates"):
                    for v in [value] if isinstance(value, str) else value:
                        if v not in self.declared[key]:
                            self.declared[key].append(v)
                    if key == "treatment":
                        with _internal():
                            self._snapshot_declared_env()
                elif key == "protocol":
                    self.declared["protocol"].update(_jsonable(dict(value)))
                elif key in ("name", "arm", "role"):
                    self.declared[key] = value
                else:
                    raise TypeError(f"unknown declaration {key!r}")

    def gate_event(self, name: str, kind: str, detail: dict[str, Any] | None = None) -> None:
        with self.lock:
            g = self.gates.setdefault(name, {"calls": 0, "passed": 0, "failed": 0, "errors": 0, "controls": None})
            if kind == "controls":
                g["controls"] = detail
            else:
                g["calls"] += 1
                g[{"pass": "passed", "fail": "failed", "error": "errors"}[kind]] += 1

    # ---- record assembly --------------------------------------------------------------------------

    def _record(
        self,
        status: str,
        exit_code: int | None = None,
        exception: str | None = None,
        signal_name: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            return {
                "schema": SCHEMA,
                "id": self.id,
                "limen_version": __version__,
                "declared": {
                    **self.declared,
                    "treatment": list(self.declared["treatment"]),
                    "gates": list(self.declared["gates"]),
                    "protocol": dict(self.declared["protocol"]),
                },
                "status": status,
                "exit_code": exit_code,
                "exception": redact.text(self._key, exception) if exception else None,
                "signal": signal_name,
                "pid": self.pid,
                "started_at": _dt.datetime.fromtimestamp(self.started, _dt.timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
                "ended_at": _now() if status != "running" else None,
                "wall_seconds": round(time.time() - self.started, 3) if status != "running" else None,
                "argv": redact.argv(self._key, self.argv),
                "cwd": os.getcwd(),
                "root": str(self.config.root),
                "python": {
                    "version": platform.python_version(),
                    "implementation": platform.python_implementation(),
                    "executable": sys.executable,
                },
                "declared_env": {k: dict(v) for k, v in self.declared_env.items()},
                "params": dict(self.params),
                "metrics": dict(self.metrics),
                "outcomes": dict(self.outcomes),
                "duplicate_outcomes": self.duplicate_outcomes,
                "gates": {k: dict(v) for k, v in self.gates.items()},
                "policy": {
                    "holdout": [str(h) for h in self.config.holdout],
                    "holdout_readers": list(self.config.holdout_readers),
                },
            }

    def _code_entry(self, path: str, git: _Git, origin: str) -> dict[str, Any]:
        """Identity of code as it was loaded, and whether the file changed while the run was going."""
        loaded = self.code_seen.get(path) if path != self.main else self.main_ident
        now = file_identity(path, self.config.max_hash_bytes, git_blob=True) or {}
        ident = loaded or now
        entry: dict[str, Any] = {"path": path, "origin": origin, "sha256": ident.get("sha256")}
        if loaded is not None:
            changed = now.get("sha256") != loaded.get("sha256") or now.get("stat") != loaded.get("stat")
        else:
            changed = now.get("mtime_ns", 0) >= self.started_ns - _MTIME_SLACK_NS
        if changed:
            entry["changed_during_run"] = True
        if origin == "project":
            entry["git"] = git.status_of(path, ident.get("git_blob"))
        if not loaded and ident.get("sha256"):
            self.store.put_object(path, ident["sha256"])
        return entry

    def _snapshot(self, record: dict[str, Any]) -> None:
        cfg = self.config
        git = _Git(self.heads)
        site_dirs, stdlib_dirs = _site_dirs(), _stdlib_dirs()

        modules: dict[str, dict[str, Any]] = {}
        module_files: set[str] = set()
        top_dists: dict[str, str] = {}
        imported_tops: set[str] = set()
        for name, mod in list(sys.modules.items()):
            path = getattr(mod, "__file__", None)
            if not isinstance(path, str) or not os.path.isabs(path) or name == "__main__":
                continue
            if name.startswith("limen") and path.startswith(_LIMEN_DIR):
                continue
            if path.endswith(".pyc"):
                try:
                    path = importlib.util.source_from_cache(path)
                except (ValueError, NotImplementedError):
                    pass
            if not os.path.isfile(path):
                continue
            module_files.add(path)
            real = os.path.realpath(path)
            top = name.split(".", 1)[0]
            if _under(real, site_dirs) or _under(path, site_dirs) or "site-packages" in real or "dist-packages" in real:
                imported_tops.add(top)
                continue
            if _under(real, stdlib_dirs) or _under(path, stdlib_dirs):
                continue
            entry = self._code_entry(path, git, "project" if cfg.in_source_roots(real) else "external")
            if top in _STDLIB_NAMES:
                entry["shadows_stdlib"] = True
            modules[name] = entry

        try:
            from importlib.metadata import packages_distributions, version

            mapping = packages_distributions()
            for top in sorted(imported_tops):
                for dist in mapping.get(top, []):
                    if dist not in top_dists:
                        try:
                            top_dists[dist] = version(dist)
                        except Exception:
                            top_dists[dist] = "unknown"
        except Exception:  # pragma: no cover - broken metadata
            pass

        main = None
        if self.main and os.path.isfile(self.main):
            main = self._code_entry(self.main, git, "project" if cfg.in_source_roots(self.main) else "external")
            module_files.add(self.main)

        # Which copy of each project package won import resolution, and which others were on sys.path.
        packages: dict[str, dict[str, Any]] = {}
        loaded_tops = {n.split(".", 1)[0] for n in modules}
        for top in sorted(set(cfg.packages) & (loaded_tops | imported_tops)):
            top_mod = sys.modules.get(top)
            loaded = getattr(top_mod, "__file__", None)
            loaded_dir = os.path.dirname(os.path.realpath(loaded)) if loaded else None
            if loaded and not os.path.basename(loaded).startswith("__init__."):
                loaded_dir = os.path.realpath(loaded)
            candidates: list[str] = []
            for path_entry in sys.path:
                base = os.path.realpath(path_entry or os.getcwd())
                for cand in (os.path.join(base, top), os.path.join(base, top + ".py")):
                    importable = os.path.isfile(os.path.join(cand, "__init__.py")) or os.path.isfile(cand)
                    if importable and cand not in candidates:
                        candidates.append(cand)
            packages[top] = {"loaded_from": loaded_dir, "canonical": str(cfg.packages[top]), "candidates": candidates}

        native = sorted(m for m in NATIVE_READERS if m in sys.modules)

        with self.lock:
            written: dict[str, dict[str, Any]] = {}
            for p in sorted(self.files_written):
                if p not in module_files:
                    written[p] = file_identity(p, cfg.max_hash_bytes) or (
                        {"dir": True} if os.path.isdir(p) else {"missing": True}
                    )
            files_read: dict[str, dict[str, Any]] = {}
            for p, v in self.files_read.items():
                if p in module_files:
                    continue
                if v.get("dir") and any(w.startswith(p) for w in self.files_written):
                    v = {**v, "self_written": True}  # a listing of the run's own output tree
                if not v.get("dir"):
                    real = os.path.realpath(p)
                    if real != p:
                        v = {**v, "realpath": real}
                files_read[p] = v
            env = {k: dict(v) for k, v in self.env_reads.items()}
            for k in self.env_set:
                if k in env and not env[k].get("set_by_run"):
                    env[k]["set_after_read"] = True

        raw_env = os.environ if not self.env_tracked else os._Environ.copy(os.environ)
        record.update(
            {
                "main": main,
                "modules": modules,
                "distributions": top_dists,
                "imports": {"sys_path": list(sys.path), "packages": packages},
                "env": env,
                "env_tracked": self.env_tracked,
                "env_bulk_read": self.env_bulk_read,
                "env_bulk_by": sorted(self.env_bulk_by),
                "files": {"read": files_read, "written": written},
                "subprocesses": list(self.subprocesses.values()),
                "native_readers": native,
                "platform": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                    "node": platform.node(),
                    "cpu_count": os.cpu_count(),
                    "gpus": _gpus(),
                    "cuda_visible_devices": raw_env.get("CUDA_VISIBLE_DEVICES"),
                },
                "git": git.state,
                "git_at_start": {k: dict(v) for k, v in self.git_at_start.items()},
            }
        )
