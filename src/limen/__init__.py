"""Limen: check that an experiment ran the way it claims.

Record what a run actually executed (``limen run script.py`` or ``with limen.run(...)``), then
check it: did the declared treatment reach the code, did anything else differ between arms,
were both arms scored the same way, did every gate run and can it fail, and did anything read
held-out data?
"""

from __future__ import annotations

from typing import Any, TypeVar

from . import capture
from ._version import __version__
from .gate import Case, GateControlError, gate, run_controls

__all__ = [
    "Case",
    "GateControlError",
    "__version__",
    "declare",
    "gate",
    "metric",
    "outcome",
    "param",
    "run",
    "run_controls",
]

T = TypeVar("T")


def run(
    name: str | None = None,
    *,
    arm: str | None = None,
    role: str | None = None,
    treatment: list[str] | tuple[str, ...] = (),
    gates: list[str] | tuple[str, ...] = (),
    protocol: dict[str, Any] | None = None,
) -> Any:
    """Record the enclosed block as one run::

    with limen.run("sweep", arm="wiki", treatment=["env:WIKI_ROOT"]):
        main()
    """
    import os
    import sys
    from pathlib import Path

    from .config import load
    from .runner import recording

    outer = capture.active()
    if outer is not None:
        config = outer.config  # nested: the declarations merge into the run already being recorded
    else:
        main = getattr(sys.modules.get("__main__"), "__file__", None)
        start = Path(main).resolve().parent if isinstance(main, str) and os.path.isfile(main) else None
        with capture._internal():
            config = load(start)
    return recording(
        config, name=name, arm=arm, role=role, treatment=list(treatment), gates=list(gates), protocol=protocol
    )


def outcome(item: Any, status: str, value: Any = None) -> None:
    """Report one scored item. ``status`` is ``pass`` or ``fail`` if it was evaluated, else
    ``error``, ``timeout``, ``infra`` or ``skip``. No-op outside a recorded run."""
    rec = capture.active()
    if rec is not None:
        rec.outcome(item, status, value)
    elif status not in capture.STATUSES:
        raise ValueError(f"outcome status must be one of {capture.STATUSES}, got {status!r}")


def metric(name: str, value: Any) -> None:
    """Report a run-level metric (the last value reported wins). No-op outside a recorded run."""
    rec = capture.active()
    if rec is not None:
        rec.metric(name, value)


def param(name: str, value: T) -> T:
    """Report the value a parameter actually took, and return it unchanged::

    lr = limen.param("lr", cfg.lr)
    """
    rec = capture.active()
    if rec is not None:
        rec.param(name, value)
    return value


def declare(**fields: Any) -> None:
    """Add declarations from inside the run: ``treatment=``, ``gates=``, ``protocol=``, ``role=``..."""
    rec = capture.active()
    if rec is not None:
        rec.declare(**fields)
