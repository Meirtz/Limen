"""Run a Python script or module under a recorder, the way ``python`` would run it."""

from __future__ import annotations

import atexit
import builtins
import contextlib
import importlib.util
import io
import os
import runpy
import signal
import sys
import threading
import traceback
import types
import zipfile
from collections.abc import Callable, Iterator
from typing import Any

from .capture import Recorder, _internal, active, file_identity
from .config import Config

# Only termination signals, and only while they still have their default action: an inherited
# SIG_IGN (nohup) or a handler installed by a launcher is left alone, and SIGUSR1/SIGUSR2 stay free
# for checkpoint-and-requeue handlers (Slurm --signal, Lightning).
_SIGNALS = ("SIGTERM", "SIGHUP")
_previous: dict[int, Any] = {}
_owner_pid: int | None = None
_signalled: list[int] = []


class _Signalled(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _handler(signum: int, _frame: Any) -> None:
    if os.getpid() != _owner_pid:  # a forked child: behave as if Limen were not there
        signal.signal(signum, _previous.get(signum, signal.SIG_DFL))
        os.kill(os.getpid(), signum)
        return
    _signalled.append(signum)
    raise _Signalled(signum)


def _restore_in_child() -> None:
    global _owner_pid
    if _owner_pid is not None and _owner_pid != os.getpid():
        for sig, old in _previous.items():
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(sig, old)
        _previous.clear()
        _owner_pid = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_restore_in_child)


def _install_signal_handlers() -> None:
    global _owner_pid
    if threading.current_thread() is not threading.main_thread():
        return
    for name in _SIGNALS:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) is not signal.SIG_DFL:
                continue
            _previous[sig] = signal.signal(sig, _handler)
        except (OSError, ValueError):
            pass
    _owner_pid = os.getpid()


def _exit_code(e: SystemExit) -> int:
    code = e.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _run_file(target: str) -> None:
    """Run a script as ``python target`` does: ``sys.argv[0]`` as given, ``__file__`` absolute."""
    path = os.path.abspath(target)
    if os.path.isdir(path) or zipfile.is_zipfile(path):
        runpy.run_path(target, run_name="__main__")
        return
    with io.open_code(path) as f:
        source = f.read()
    code = compile(source, path, "exec", dont_inherit=True)
    module = types.ModuleType("__main__")
    module.__dict__.update(
        __file__=path, __cached__=None, __package__=None, __spec__=None, __loader__=None, __builtins__=builtins
    )
    saved = sys.modules.get("__main__")
    sys.modules["__main__"] = module
    try:
        exec(code, module.__dict__)
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved


def _finalize(rec: Recorder, state: dict[str, Any], on_finish: Callable[[dict[str, Any]], None] | None) -> None:
    if rec.finished or os.getpid() != rec.pid:
        return
    if _signalled and state["status"] == "ok":  # killed after the script returned (waiting for threads)
        sig = signal.Signals(_signalled[-1])
        state.update(status="killed", code=128 + sig.value, sig=sig.name)
    try:
        record = rec.finish(state["status"], state["code"], state["exc"], state["sig"])
    except Exception as e:  # never replace the script's own outcome with ours
        print(f"limen: could not save the run record: {type(e).__name__}: {e}", file=sys.stderr)
        return
    if on_finish is not None:
        try:
            on_finish(record)
        except Exception as e:  # pragma: no cover
            print(f"limen: {type(e).__name__}: {e}", file=sys.stderr)


def run(
    config: Config,
    command: list[str],
    *,
    defer: bool = False,
    on_finish: Callable[[dict[str, Any]], None] | None = None,
    **declared: Any,
) -> tuple[int, Recorder]:
    """Execute ``command`` (``script.py args...``, ``dir/`` or ``-m module args...``) and record it.

    With ``defer=True`` the record is finalized at interpreter exit, after non-daemon threads and
    the script's own ``atexit`` handlers, so their outcomes and writes are included.
    """
    if not command:
        raise ValueError("nothing to run")
    if command[0] == "-m":
        if len(command) < 2:
            raise ValueError("-m needs a module name")
        target, args, is_module = command[1], command[2:], True
        saved0 = sys.path[0] if sys.path else None
        if sys.path:
            sys.path[0] = os.getcwd()
        try:
            with _internal():
                found = importlib.util.find_spec(target.partition(".")[0]) is not None
        finally:
            if saved0 is not None:
                sys.path[0] = saved0
        if not found:
            raise ValueError(f"no module named {target!r}")
        main, argv0, path0 = None, target, os.getcwd()
    else:
        target, args, is_module = command[0], command[1:], False
        if not os.path.exists(target):
            raise ValueError(f"no such file: {target}")
        path = os.path.abspath(target)
        main = os.path.join(path, "__main__.py") if os.path.isdir(path) else path
        argv0, path0 = target, path if os.path.isdir(path) else os.path.dirname(path)

    rec = Recorder(config, argv=[argv0, *args], main=main, **declared)
    state: dict[str, Any] = {"status": "ok", "code": 0, "exc": None, "sig": None}
    saved_argv, saved_path0 = sys.argv[:], sys.path[0] if sys.path else None
    sys.argv = [argv0, *args]
    if sys.path:
        sys.path[0] = path0
    _install_signal_handlers()
    if defer:
        atexit.register(_finalize, rec, state, on_finish)  # registered first, so it runs last
    rec.start()
    try:
        if is_module:
            spec = importlib.util.find_spec(target)  # imports parent packages: now recorded
            if spec is not None and spec.submodule_search_locations is not None:
                spec = importlib.util.find_spec(target + ".__main__") or spec
            if spec is not None and spec.origin and os.path.isfile(spec.origin):
                rec.main = os.path.abspath(spec.origin)
                with _internal():
                    rec.main_ident = file_identity(rec.main, config.max_hash_bytes, git_blob=True)
                rec.argv[0] = sys.argv[0] = spec.origin
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            _run_file(target)
    except SystemExit as e:
        state["code"] = _exit_code(e)
        state["status"] = "ok" if state["code"] == 0 else "failed"
    except KeyboardInterrupt:
        state.update(status="interrupted", code=130)
    except _Signalled as e:
        state.update(status="killed", code=128 + e.signum, sig=signal.Signals(e.signum).name)
    except BaseException as e:  # report like the interpreter, then exit 1
        state.update(status="failed", code=1, exc=f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if os.getpid() == rec.pid:
            sys.argv = saved_argv
            if saved_path0 is not None and sys.path:
                sys.path[0] = saved_path0
    if os.getpid() != rec.pid:  # a forked child that fell through: exit as plain python would
        raise SystemExit(state["code"])
    if not defer:
        _finalize(rec, state, on_finish)
    return int(state["code"]), rec


@contextlib.contextmanager
def recording(config: Config, **declared: Any) -> Iterator[Recorder]:
    """Record the enclosed block as one run (for notebooks and long-lived processes).

    Inside a process that is already being recorded (``limen run script.py``), the block's
    declarations are added to that run instead of starting a new one.
    """
    outer = active()
    if outer is not None:
        outer.declare(**{k: v for k, v in declared.items() if v not in (None, [], (), {})})
        yield outer
        return
    main_mod = sys.modules.get("__main__")
    main = getattr(main_mod, "__file__", None)
    rec = Recorder(config, argv=list(sys.argv), main=main if main and os.path.isfile(main) else None, **declared)
    rec.start()

    def done(status: str, code: int, exc: str | None = None) -> None:
        try:
            rec.finish(status, code, exc)
        except Exception as e:  # never mask the block's own exception
            print(f"limen: could not save the run record: {type(e).__name__}: {e}", file=sys.stderr)

    try:
        yield rec
    except KeyboardInterrupt:
        done("interrupted", 130)
        raise
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        done("ok" if code == 0 else "failed", code)
        raise
    except BaseException as e:
        done("failed", 1, f"{type(e).__name__}: {e}")
        raise
    else:
        done("ok", 0)
