"""Gates that are shown to work before they are trusted.

A gate is a function that accepts or rejects a candidate (a correctness check, an anti-cheat
scan, an acceptance filter). Wrapping it with :func:`gate` and giving it known-good and
known-bad controls runs those controls before the first real candidate: a gate that accepts a
known-bad control cannot fail, and a gate that rejects a known-good control is broken. Either
way it would silently corrupt everything downstream, so by default Limen raises before any
real candidate is judged.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from . import capture

F = TypeVar("F", bound=Callable[..., Any])


class GateControlError(RuntimeError):
    """A gate failed its own controls."""


@dataclass
class Case:
    """Arguments for one control call, with a label for reports."""

    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    label: str | None = None

    @classmethod
    def of(cls, value: Any) -> Case:
        return value if isinstance(value, Case) else cls(args=(value,))

    def name(self, index: int, kind: str) -> str:
        return self.label or f"{kind}[{index}]"


def run_controls(
    fn: Callable[..., Any], positives: Iterable[Any] = (), negatives: Iterable[Any] = ()
) -> dict[str, Any]:
    """Evaluate ``fn`` on known-good and known-bad cases; report what went wrong."""
    pos, neg = [Case.of(p) for p in positives], [Case.of(n) for n in negatives]
    positives_failed, negatives_passed, errors = [], [], []
    for i, c in enumerate(pos):
        try:
            ok = bool(fn(*c.args, **c.kwargs))
        except Exception as e:  # a gate that crashes on good input is broken
            ok = False
            errors.append(f"{c.name(i, 'positive')}: {type(e).__name__}: {e}")
        if not ok:
            positives_failed.append(c.name(i, "positive"))
    for i, c in enumerate(neg):
        try:
            ok = bool(fn(*c.args, **c.kwargs))
        except Exception:  # raising on bad input is a rejection
            ok = False
        if ok:
            negatives_passed.append(c.name(i, "negative"))
    return {
        "ok": not positives_failed and not negatives_passed,
        "positives": len(pos),
        "negatives": len(neg),
        "positives_failed": positives_failed,
        "negatives_passed": negatives_passed,
        "errors": errors,
    }


def gate(
    name: str | None = None,
    *,
    positives: Iterable[Any] = (),
    negatives: Iterable[Any] = (),
    on_control_failure: str = "raise",
) -> Callable[[F], F]:
    """Decorate a gate function; controls run once, before the first real call.

    ``positives``/``negatives`` are candidates (passed as the single argument) or :class:`Case`
    objects. ``on_control_failure`` is ``"raise"`` (default) or ``"record"``.
    """
    if on_control_failure not in ("raise", "record"):
        raise ValueError("on_control_failure must be 'raise' or 'record'")
    pos, neg = list(positives), list(negatives)

    def decorate(fn: F) -> F:
        gname = name or fn.__qualname__
        lock = threading.Lock()
        state: dict[str, dict[str, Any] | None] = {"controls": None}

        def controls() -> dict[str, Any] | None:
            if not (pos or neg):
                return None
            with lock:
                if state["controls"] is None:
                    state["controls"] = run_controls(fn, pos, neg)
            return state["controls"]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctl = controls()
            rec = capture.active()
            if rec is not None and ctl is not None and not (rec.gates.get(gname) or {}).get("controls"):
                rec.gate_event(gname, "controls", ctl)
            if ctl is not None and not ctl["ok"] and on_control_failure == "raise":
                raise GateControlError(
                    f"gate {gname!r} failed its controls: "
                    f"accepted known-bad {ctl['negatives_passed']}, rejected known-good {ctl['positives_failed']}"
                )
            try:
                result = fn(*args, **kwargs)
            except Exception:
                if rec is not None:
                    rec.gate_event(gname, "error")
                raise
            if rec is not None:
                rec.gate_event(gname, "pass" if result else "fail")
            return result

        wrapper.limen_gate = gname  # type: ignore[attr-defined]
        wrapper.limen_controls = controls  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorate
