"""Findings: what a check found, how serious it is, and the evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

BLOCK = "block"
WARN = "warn"
INFO = "info"
_ORDER = {BLOCK: 0, WARN: 1, INFO: 2}


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    subject: str = ""
    run: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        where = f" [{self.subject}]" if self.subject else ""
        run = f" (run {self.run})" if self.run else ""
        return safe(f"{self.severity.upper():5} {self.code}{where}: {self.message}{run}")


_CONTROL = {c: f"\\x{c:02x}" for c in range(32) if c != 9} | {127: "\\x7f"}


def safe(text: str) -> str:
    """Escape control characters so record contents cannot drive the terminal."""
    return text.translate(_CONTROL)


def sort(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (_ORDER.get(f.severity, 9), f.code, f.subject, f.run))


def blocking(findings: list[Finding]) -> bool:
    return any(f.severity == BLOCK for f in findings)


def render(findings: list[Finding], *, verbose: bool = False) -> str:
    if not findings:
        return "ok: no findings"
    lines = []
    groups: dict[tuple[str, str, str, str], list[Finding]] = {}
    for f in sort(findings):
        groups.setdefault((f.severity, f.code, f.subject, f.message), []).append(f)
    for group in groups.values():
        f = group[0]
        runs = [g.run for g in group if g.run]
        head = (
            f.line()
            if len(runs) <= 1
            else Finding(f.severity, f.code, f.message, f.subject).line()
            + (f" ({len(runs)} runs: {', '.join(runs[:3])}{', ...' if len(runs) > 3 else ''})")
        )
        lines.append(head)
        if verbose and f.evidence:
            for k, v in f.evidence.items():
                text = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
                lines.append(safe(f"      {k}: {text[:400]}"))
    n_block = sum(1 for sev, *_ in groups if sev == BLOCK)
    n_warn = sum(1 for sev, *_ in groups if sev == WARN)
    lines.append(f"{n_block} blocking, {n_warn} warnings")
    return "\n".join(lines)


def to_json(findings: list[Finding], **extra: Any) -> str:
    return json.dumps(
        {"blocking": blocking(findings), "findings": [asdict(f) for f in sort(findings)], **extra},
        indent=1,
        sort_keys=True,
        default=str,
    )
