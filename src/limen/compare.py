"""Compare two arms of an experiment against what the comparison claims to test.

A comparison of arm A (control) and arm B (treatment) is valid when:

1. the declared treatment is *effective*: it was observed, it differs between the arms, and every
   run of an arm has that arm's treatment value (otherwise ``PLACEBO`` or ``TREATMENT_NOT_APPLIED``);
2. nothing else differs: every other input field agrees across the arms, in value and in mix,
   unless waived (otherwise ``CONFOUND``); only run-level knobs (``param:``/``arg:``/``env:``
   fields such as seeds) may vary inside both arms as replicates;
3. every run is scored on the same items, and items that were never evaluated (errors, timeouts,
   infrastructure failures) are equally rare in both arms (otherwise ``ITEMS_DIFFER`` or
   ``ASYMMETRIC_NOT_EVALUATED``);
4. every run involved passes its own checks (see :mod:`limen.checks`).

It also estimates the effect and warns when it is within noise or cannot be estimated.
"""

from __future__ import annotations

import math
import os
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import fields
from .capture import NOT_EVALUATED
from .checks import check_run, env_children
from .findings import BLOCK, INFO, WARN, Finding
from .store import Store

NOT_EVALUATED_GAP = 0.05
NOT_EVALUATED_MIN_ITEMS = 3
IMBALANCE = 0.25
FEW_ITEMS = 20
BOOTSTRAP_SAMPLES = 2000
MAX_LISTED = 12

# Launcher and rendezvous identifiers that differ per job and cannot change results. RANK,
# LOCAL_RANK, WORLD_SIZE, SLURM_PROCID and array task ids are deliberately not here: they select
# shards, seeds or batch sizes.
DEFAULT_WAIVE = (
    "env:MASTER_ADDR",
    "env:MASTER_PORT",
    "env:TORCHELASTIC_RUN_ID",
    "env:SLURM_JOB_ID",
    "env:SLURM_JOBID",
    "env:SLURM_STEP_ID",
    "env:SLURM_JOB_NODELIST",
    "env:SLURM_NODELIST",
    "env:SLURMD_NODENAME",
    "env:HOSTNAME",
)
_REPLICATE_KINDS = ("param", "arg", "env")
_REPLICATE_INDEX = re.compile(r"seed|rank|fold|shard|trial|rep|replica|run|worker|split_idx|index", re.I)
# shell and scheduler variables that differ between sessions and jobs; never reported as possible confounds
_VOLATILE_ENV = re.compile(
    r"^(PWD|OLDPWD|SHLVL|_|TERM.*|COLUMNS|LINES|SSH_.*|TMUX.*|STY|WINDOW|WINDOWID|DISPLAY|XDG_.*|SECURITYSESSIONID|"
    r"__CF.*|Apple_.*|LaunchInstanceID|ITERM_.*|KITTY_.*|VSCODE_.*|SLURM_.*|PBS_.*|LSB_.*|OMPI_.*|PMI_.*|"
    r"HOSTNAME|MallocNanoZone|LOGNAME|USER|MAIL|OLDPWD|TMPDIR|CONDA_PROMPT_MODIFIER|PS1|HISTFILE|LESS.*|PAGER)$"
)
_PRESENCE_KINDS = ("file", "module", "env", "param")

# two-sided 97.5% Student-t quantiles
_T975 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    12: 2.179,
    15: 2.131,
    20: 2.086,
    25: 2.060,
    30: 2.042,
    40: 2.021,
    60: 2.000,
    120: 1.980,
}


def _t975(df: float) -> float:
    """Quantile for the largest tabulated df not above ``df`` (conservative for fractional df)."""
    q = _T975[1]
    for k in sorted(_T975):
        if df < k:
            break
        q = _T975[k]
    return q


@dataclass
class Comparison:
    findings: list[Finding]
    treatment: list[str]
    runs_a: list[str]
    runs_b: list[str]
    effects: list[dict[str, Any]] = field(default_factory=list)
    explained: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def _values(idents: list[dict[str, str]], key: str) -> list[str]:
    return [i.get(key, fields.ABSENT) for i in idents]


def _fmt(values: set[str]) -> str:
    vs = sorted(values)
    text = ", ".join(v if len(v) <= 80 else v[:77] + "..." for v in vs[:4])
    return text + (f" (+{len(vs) - 4} more)" if len(vs) > 4 else "")


def _fmt_mix(values: list[str]) -> str:
    return ", ".join(f"{n}x {v if len(v) <= 40 else v[:37] + '...'}" for v, n in Counter(values).most_common(4))


def _imbalanced(va: list[str], vb: list[str]) -> bool:
    ca, cb = Counter(va), Counter(vb)
    return any(abs(ca[v] / len(va) - cb[v] / len(vb)) > IMBALANCE for v in set(ca) | set(cb))


def compare(
    runs_a: list[dict[str, Any]],
    runs_b: list[dict[str, Any]],
    *,
    treatment: list[str] | None = None,
    waive: list[str] | tuple[str, ...] = (),
    metrics: list[str] | None = None,
    store: Store | None = None,
    check_runs: bool = True,
    seed: int = 0,
) -> Comparison:
    if not runs_a or not runs_b:
        raise ValueError("both arms need at least one run")
    if treatment is None:
        treatment = []
        for r in runs_a + runs_b:
            for spec in (r.get("declared") or {}).get("treatment") or []:
                if spec not in treatment:
                    treatment.append(spec)
    treatment = [fields.parse_spec(s) for s in treatment]
    waive = [fields.parse_spec(s) for s in (*DEFAULT_WAIVE, *waive)]
    runs = runs_a + runs_b

    findings: list[Finding] = []
    if check_runs:
        for r in runs:
            findings.extend(check_run(r, store))

    ids_a = [fields.identity(r, treatment) for r in runs_a]
    ids_b = [fields.identity(r, treatment) for r in runs_b]
    keys = sorted(set().union(*ids_a, *ids_b))
    comp = Comparison(findings, treatment, [r["id"] for r in runs_a], [r["id"] for r in runs_b])

    if not treatment:
        findings.append(
            Finding(WARN, "NO_TREATMENT", "no treatment was declared, so every difference counts as a confound")
        )

    # 1. the treatment must be observed, must differ, and must be applied to every run --------
    children = any(env_children(r.get("subprocesses")) for r in runs)
    bulk = sorted({b for r in runs if r.get("env_bulk_read") for b in (r.get("env_bulk_by") or ["?"])})
    for spec in treatment:
        matched = [k for k in keys if fields.matches(spec, k)]
        differing = [k for k in matched if set(_values(ids_a, k)) != set(_values(ids_b, k))]
        if differing:
            comp.explained[spec] = {
                k: [_fmt(set(_values(ids_a, k))), _fmt(set(_values(ids_b, k)))] for k in differing[:MAX_LISTED]
            }
            continue
        if matched:
            findings.append(
                Finding(
                    BLOCK,
                    "PLACEBO",
                    "the treatment was observed but is identical in both arms",
                    subject=spec,
                    evidence={k: _fmt(set(_values(ids_a, k))) for k in matched[:MAX_LISTED]},
                )
            )
            continue
        findings.append(_unobserved(spec, runs_a, runs_b, children, bulk))

    tkeys = [k for k in keys if any(fields.matches(s, k) for s in treatment)]
    unobserved_env = [
        s[len("env:") :]
        for s in treatment
        if s.startswith("env:") and not s.endswith(("*", "/")) and not any(fields.matches(s, k) for k in keys)
    ]
    if tkeys or unobserved_env:

        def signature(r: dict[str, Any], i: dict[str, str]) -> tuple[str, ...]:
            return (
                *(i.get(k, fields.ABSENT) for k in tkeys),
                *(str(fields.declared_env_value(r, n)) for n in unobserved_env),
            )

        sig_a = [signature(r, i) for r, i in zip(runs_a, ids_a, strict=True)]
        sig_b = [signature(r, i) for r, i in zip(runs_b, ids_b, strict=True)]
        if set(sig_a) != set(sig_b):
            mixed = [r["id"] for r, s in zip(runs_a, sig_a, strict=True) if s in set(sig_b)]
            mixed += [r["id"] for r, s in zip(runs_b, sig_b, strict=True) if s in set(sig_a)]
            if mixed:
                findings.append(
                    Finding(
                        BLOCK,
                        "TREATMENT_NOT_APPLIED",
                        "some runs have the same treatment value as runs of the other arm, so the arms are mixed",
                        evidence={"runs": mixed[:MAX_LISTED]},
                    )
                )

    # 2. nothing else may differ ---------------------------------------------------------------
    confounds: list[tuple[str, list[str], list[str]]] = []
    replicates: list[str] = []
    for k in keys:
        if any(fields.matches(s, k) for s in treatment) or any(fields.matches(w, k) for w in waive):
            continue
        va, vb = _values(ids_a, k), _values(ids_b, k)
        sa, sb = set(va), set(vb)
        replicable = k.split(":", 1)[0] in _REPLICATE_KINDS
        if sa == sb:
            if len(sa) > 1 and not replicable and _imbalanced(va, vb):
                confounds.append((k, va, vb))
            continue
        if replicable and len(sa) > 1 and len(sb) > 1:
            index_like = bool(_REPLICATE_INDEX.search(k.split(":", 1)[-1]))
            if index_like or (sa & sb and not _imbalanced(va, vb)):
                replicates.append(k)
                continue
        confounds.append((k, va, vb))
    confounds, restated = _split_restated(confounds, ids_a + ids_b, treatment)
    if restated:
        findings.append(
            Finding(
                INFO,
                "TREATMENT_RECORDED_TWICE",
                "fields that name the treatment's setting and equal its value in every run "
                "were treated as the treatment",
                evidence={"fields": restated[:MAX_LISTED]},
            )
        )
    treatment_paths = _treatment_paths(runs, ids_a + ids_b, treatment)
    one_sided: list[str] = []
    explained: list[str] = []
    for k, va, vb in confounds:
        sa, sb = set(va), set(vb)
        presence_only = k.split(":", 1)[0] in _PRESENCE_KINDS and (sa == {fields.ABSENT} or sb == {fields.ABSENT})
        if presence_only:
            if _explained_by_paths(k, runs, treatment_paths):
                explained.append(k)
            else:
                one_sided.append(k)
            continue
        text = f"A={_fmt(sa)} | B={_fmt(sb)}" if sa != sb else f"A={_fmt_mix(va)} | B={_fmt_mix(vb)}"
        findings.append(Finding(BLOCK, "CONFOUND", f"differs between arms: {text}", subject=k))
    if one_sided:
        findings.append(
            Finding(
                WARN,
                "INPUT_ONLY_IN_ONE_ARM",
                f"{len(one_sided)} input(s) were read by one arm only; if the treatment does not "
                "explain them, they are confounds",
                evidence={"fields": one_sided[:MAX_LISTED]},
            )
        )
    if explained:
        findings.append(
            Finding(
                INFO,
                "EXPLAINED_BY_TREATMENT",
                f"{len(explained)} input(s) read by one arm only lie under a treatment path",
                evidence={"fields": explained[:MAX_LISTED]},
            )
        )
    if replicates:
        findings.append(
            Finding(
                INFO,
                "REPLICATE_FIELDS",
                "run-level settings that vary within both arms were treated as replicates",
                evidence={"fields": replicates[:MAX_LISTED]},
            )
        )

    findings.extend(_bulk_differences(runs_a, runs_b, keys, treatment, waive))
    findings.extend(_dropped_differences(runs_a, runs_b, treatment))

    if store is not None:
        findings.extend(_omitted_runs(runs, store))

    # 3. same items, same rate of never-evaluated items -------------------------------------------
    findings.extend(_outcome_parity(runs_a, runs_b))

    # effect estimates ------------------------------------------------------------------------
    comp.effects = _effects(runs_a, runs_b, metrics, seed, findings)
    return comp


def _bulk_differences(
    runs_a: list[dict[str, Any]], runs_b: list[dict[str, Any]], keys: list[str], treatment: list[str], waive: list[str]
) -> list[Finding]:
    """Variables the project or its config loader copied in bulk that differ between the arms."""
    if not all(r.get("env_bulk_read") for r in runs_a + runs_b):
        return []
    names = set().union(*[set(r.get("env_bulk") or {}) for r in runs_a + runs_b])
    differing = []
    for n in sorted(names):
        key = f"env:{n}"
        if key in keys or _VOLATILE_ENV.match(n) or any(fields.matches(s, key) for s in (*treatment, *waive)):
            continue
        va = {(r.get("env_bulk") or {}).get(n, fields.UNSET) for r in runs_a}
        vb = {(r.get("env_bulk") or {}).get(n, fields.UNSET) for r in runs_b}
        if va != vb:
            differing.append(key)
    if not differing:
        return []
    return [
        Finding(
            WARN,
            "POSSIBLE_CONFOUND",
            f"{len(differing)} variable(s) differ between arms and reached the code only through a bulk copy "
            "of the environment (e.g. a settings loader); if the code uses them, they are confounds",
            evidence={"fields": differing[:MAX_LISTED]},
        )
    ]


def _dropped_differences(
    runs_a: list[dict[str, Any]], runs_b: list[dict[str, Any]], treatment: list[str]
) -> list[Finding]:
    """Fields treated as output locations or labels whose values differ: listed so a mistake is visible."""
    da: list[dict[str, str]] = []
    db: list[dict[str, str]] = []
    for runs, out in ((runs_a, da), (runs_b, db)):
        for r in runs:
            d: dict[str, str] = {}
            fields.identity(r, treatment, dropped=d)
            out.append(d)
    keys = sorted(set().union(*da, *db))
    differing = [k for k in keys if set(_values(da, k)) != set(_values(db, k))]
    if not differing:
        return []
    return [
        Finding(
            INFO,
            "TREATED_AS_OUTPUTS",
            "these differing values were treated as output locations or run labels, not inputs",
            evidence={k: [_fmt(set(_values(da, k))), _fmt(set(_values(db, k)))] for k in differing[:MAX_LISTED]},
        )
    ]


def _unobserved(
    spec: str, runs_a: list[dict[str, Any]], runs_b: list[dict[str, Any]], children: bool, bulk: list[str]
) -> Finding:
    """No run read the treatment field. For env treatments, use the value each run started with."""
    if spec.startswith("env:") and not spec.endswith(("*", "/")):
        name = spec[len("env:") :]
        va = [fields.declared_env_value(r, name) for r in runs_a]
        vb = [fields.declared_env_value(r, name) for r in runs_b]
        if all(v is not None for v in va + vb):
            untracked = any(r.get("env_tracked") is False for r in runs_a + runs_b)
            if untracked and set(va) != set(vb):
                return Finding(
                    WARN,
                    "TREATMENT_UNVERIFIED",
                    f"{name} differs between arms, but environment reads were not observed",
                    subject=spec,
                )
            if set(va) == set(vb):
                return Finding(
                    BLOCK,
                    "PLACEBO",
                    f"{name} had the same value in both arms ({_fmt({str(v) for v in va})}) and was never read",
                    subject=spec,
                )
            if not children and not bulk:
                return Finding(
                    BLOCK,
                    "PLACEBO",
                    f"{name} was set differently in the arms but no run ever read it",
                    subject=spec,
                )
    if spec.startswith("env:") and (children or bulk):
        why = []
        if bulk:
            why.append(f"os.environ was copied in bulk ({', '.join(bulk)})")
        if children:
            why.append("child processes inherited the environment")
        return Finding(
            WARN, "TREATMENT_UNVERIFIED", "no run read this variable by name; " + " and ".join(why), subject=spec
        )
    return Finding(
        BLOCK, "PLACEBO", "the treatment was not observed in any run: the arms cannot differ in it", subject=spec
    )


def _norm(v: str | None) -> str | None:
    if v is None or v in (fields.ABSENT, fields.UNSET):
        return None
    t = v.strip().strip('"')
    try:
        return repr(float(t))
    except ValueError:
        return t


def _stem(key: str) -> str:
    """``arg:--use-rag``, ``param:use_rag`` and ``env:USE_RAG`` all name the setting ``use_rag``."""
    return key.split(":", 1)[-1].lstrip("-").lower().replace("-", "_")


def restates_treatment(key: str, idents: list[dict[str, str]], treatment: list[str]) -> bool:
    """Is ``key`` the treatment recorded again (``param:lr`` alongside ``arg:--lr``)?

    True when ``key`` names the same setting as a treatment field and has its value in every run
    where the treatment is present, and at least one such run exists.
    """
    matched_any = False
    for ident in idents:
        tvals = {
            _norm(v)
            for k, v in ident.items()
            if any(fields.matches(s, k) for s in treatment) and _stem(k) == _stem(key)
        } - {None}
        if not tvals:
            continue
        if _norm(ident.get(key)) not in tvals:
            return False
        matched_any = True
    return matched_any


def _split_restated(
    confounds: list[tuple[str, list[str], list[str]]], idents: list[dict[str, str]], treatment: list[str]
) -> tuple[list[tuple[str, list[str], list[str]]], list[str]]:
    kept: list[tuple[str, list[str], list[str]]] = []
    restated: list[str] = []
    for c in confounds:
        if restates_treatment(c[0], idents, treatment):
            restated.append(c[0])
        else:
            kept.append(c)
    return kept, restated


def _treatment_paths(runs: list[dict[str, Any]], idents: list[dict[str, str]], treatment: list[str]) -> list[str]:
    """Path-like values of treatment fields (and their parent, the set of alternatives), root-relative."""
    out: set[str] = set()
    for r, ident in zip(runs, idents, strict=True):
        root, cwd = r.get("root") or "", r.get("cwd") or ""
        for k, v in ident.items():
            if not any(fields.matches(s, k) for s in treatment) or k.startswith(("file:", "module:")):
                continue
            if isinstance(v, str) and v and ("/" in v or v.startswith(".")):
                ap = os.path.abspath(os.path.join(cwd, v))
                rel = os.path.relpath(ap, root).replace(os.sep, "/") if root and ap.startswith(root) else ap
                out.add(rel)
                parent = os.path.dirname(rel.rstrip("/"))
                if parent not in ("", ".", "/"):
                    out.add(parent)
        for spec in treatment:
            if spec.startswith("file:"):
                out.add(spec[len("file:") :].rstrip("*"))
    return sorted(out)


def _under_any(path: str, prefixes: list[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def _explained_by_paths(key: str, runs: list[dict[str, Any]], prefixes: list[str]) -> bool:
    if key.startswith("file:"):
        return _under_any(key[len("file:") :], prefixes)
    if key.startswith("module:"):  # code loaded from under a treatment path (e.g. a candidate directory)
        name = key[len("module:") :]
        paths = [
            os.path.relpath(m["path"], r.get("root") or "/").replace(os.sep, "/")
            for r in runs
            for n, m in (r.get("modules") or {}).items()
            if n == name and m.get("path")
        ]
        return bool(paths) and all(_under_any(p, prefixes) for p in paths)
    return False


def _omitted_runs(runs: list[dict[str, Any]], store: Store) -> list[Finding]:
    """Completed runs of the same name and arm that the comparison leaves out (selective reporting)."""
    included = {r.get("id") for r in runs}
    labels = {
        ((r.get("declared") or {}).get("name"), (r.get("declared") or {}).get("arm"))
        for r in runs
        if (r.get("declared") or {}).get("name")
    }
    out = []
    everything = store.all()
    for name, arm in sorted(labels, key=str):
        left_out = [
            r["id"]
            for r in everything
            if (r.get("declared") or {}).get("name") == name
            and (r.get("declared") or {}).get("arm") == arm
            and r.get("status") == "ok"
            and r.get("id") not in included
        ]
        if left_out:
            label = f"{name}:{arm}" if arm else str(name)
            out.append(
                Finding(
                    WARN,
                    "OMITTED_RUNS",
                    f"{len(left_out)} other completed run(s) of {label} are not part of this comparison",
                    subject=label,
                    evidence={"runs": left_out[:MAX_LISTED]},
                )
            )
    return out


def _items(runs: list[dict[str, Any]]) -> set[str]:
    return set().union(*[set((r.get("outcomes") or {}).keys()) for r in runs])


def _outcome_parity(runs_a: list[dict[str, Any]], runs_b: list[dict[str, Any]]) -> list[Finding]:
    out: list[Finding] = []
    runs = runs_a + runs_b
    if not all(r.get("outcomes") for r in runs):
        if any(r.get("outcomes") for r in runs):
            out.append(Finding(BLOCK, "ITEMS_DIFFER", "some runs report per-item outcomes and others report none"))
        return out
    ia, ib = _items(runs_a), _items(runs_b)
    if ia != ib:
        out.append(
            Finding(
                BLOCK,
                "ITEMS_DIFFER",
                f"the arms were scored on different items: {len(ia - ib)} only in A, {len(ib - ia)} only in B",
                evidence={"only_a": sorted(ia - ib)[:MAX_LISTED], "only_b": sorted(ib - ia)[:MAX_LISTED]},
            )
        )
    else:
        partial = {r["id"]: len(ia) - len(r["outcomes"]) for r in runs if len(r["outcomes"]) < len(ia)}
        if partial:
            out.append(
                Finding(
                    BLOCK,
                    "ITEMS_DIFFER",
                    f"{len(partial)} run(s) reported outcomes for only part of the {len(ia)} items",
                    evidence={"missing_per_run": dict(sorted(partial.items())[:MAX_LISTED])},
                )
            )

    def rate(runs: list[dict[str, Any]]) -> tuple[int, int, dict[str, int]]:
        total, ne = 0, 0
        by: dict[str, int] = {}
        for r in runs:
            for v in (r.get("outcomes") or {}).values():
                total += 1
                if v.get("status") in NOT_EVALUATED:
                    ne += 1
                    by[v["status"]] = by.get(v["status"], 0) + 1
        return total, ne, by

    ta, na, ba = rate(runs_a)
    tb, nb, bb = rate(runs_b)
    ra, rb = na / ta if ta else 0.0, nb / tb if tb else 0.0
    per_run_gap = abs(na / max(len(runs_a), 1) - nb / max(len(runs_b), 1))
    if abs(ra - rb) >= NOT_EVALUATED_GAP and per_run_gap >= NOT_EVALUATED_MIN_ITEMS:
        out.append(
            Finding(
                BLOCK,
                "ASYMMETRIC_NOT_EVALUATED",
                f"items never evaluated: A {na}/{ta} ({ra:.0%}) vs B {nb}/{tb} ({rb:.0%}); "
                "a pass-rate difference would partly measure the harness, not the treatment",
                evidence={"a": ba, "b": bb},
            )
        )
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _var(xs: list[float]) -> float:
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _effects(
    runs_a: list[dict[str, Any]],
    runs_b: list[dict[str, Any]],
    metrics: list[str] | None,
    seed: int,
    findings: list[Finding],
) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    runs = runs_a + runs_b
    names = metrics
    if names is None:
        common = set.intersection(*[set(r.get("metrics") or {}) for r in runs])
        names = []
        for n in sorted(common):
            vals = [(r.get("metrics") or {}).get(n) for r in runs]
            nums = [_num(v) for v in vals]
            if all(x is not None for x in nums):
                names.append(n)
            elif any(x is not None for x in nums):  # numeric somewhere: a NaN/inf or a stray value hides a run
                bad = [r["id"] for r, x in zip(runs, nums, strict=True) if x is None]
                findings.append(
                    Finding(
                        WARN,
                        "METRIC_NOT_FINITE",
                        f"not a finite number in {len(bad)} run(s)",
                        subject=n,
                        evidence={"runs": bad[:MAX_LISTED]},
                    )
                )
    no_reps = []
    for name in names:
        a = [_num((r.get("metrics") or {}).get(name)) for r in runs_a]
        b = [_num((r.get("metrics") or {}).get(name)) for r in runs_b]
        if any(v is None for v in a + b):
            findings.append(
                Finding(BLOCK, "METRIC_MISSING", "not every run reported a finite number for this metric", subject=name)
            )
            continue
        a_f, b_f = [float(v) for v in a if v is not None], [float(v) for v in b if v is not None]
        eff: dict[str, Any] = {
            "metric": name,
            "a_mean": _mean(a_f),
            "b_mean": _mean(b_f),
            "delta": _mean(b_f) - _mean(a_f),
            "n_a": len(a_f),
            "n_b": len(b_f),
        }
        if len(a_f) >= 2 and len(b_f) >= 2:
            va, vb = _var(a_f) / len(a_f), _var(b_f) / len(b_f)
            se = math.sqrt(va + vb)
            denom = (va**2 / (len(a_f) - 1) if va else 0.0) + (vb**2 / (len(b_f) - 1) if vb else 0.0)
            df = (va + vb) ** 2 / denom if denom else float(min(len(a_f), len(b_f)) - 1)
            half = _t975(df) * se
            eff["ci95"] = [eff["delta"] - half, eff["delta"] + half]
            if eff["ci95"][0] <= 0 <= eff["ci95"][1]:
                findings.append(
                    Finding(
                        WARN,
                        "EFFECT_WITHIN_NOISE",
                        f"delta {eff['delta']:+.4g}, 95% CI [{eff['ci95'][0]:+.4g}, {eff['ci95'][1]:+.4g}] includes 0",
                        subject=name,
                    )
                )
        else:
            no_reps.append(name)
        effects.append(eff)
    if no_reps:
        findings.append(
            Finding(
                WARN,
                "NO_REPLICATES",
                "an arm has a single run, so the noise level is unknown",
                subject=", ".join(no_reps),
            )
        )

    if all(r.get("outcomes") for r in runs):
        items = sorted(_items(runs_a) & _items(runs_b))
        if items:
            effects.append(_item_effect(runs_a, runs_b, items, seed, findings))
    return effects


def _item_effect(
    runs_a: list[dict[str, Any]], runs_b: list[dict[str, Any]], items: list[str], seed: int, findings: list[Finding]
) -> dict[str, Any]:
    def per_item(runs: list[dict[str, Any]], evaluated_only: bool = False) -> list[float | None]:
        vals: list[float | None] = []
        for it in items:
            xs = []
            for r in runs:
                o = (r.get("outcomes") or {}).get(it)
                if o is None or (evaluated_only and o["status"] in NOT_EVALUATED):
                    continue
                xs.append(1.0 if o["status"] == "pass" else 0.0)
            vals.append(_mean(xs) if xs else None)
        return vals

    pa, pb = per_item(runs_a), per_item(runs_b)
    diffs = [y - x for x, y in zip(pa, pb, strict=True) if x is not None and y is not None]
    eff: dict[str, Any] = {
        "metric": "item_pass_rate",
        "a_mean": _mean([x for x in pa if x is not None]),
        "b_mean": _mean([y for y in pb if y is not None]),
        "delta": _mean(diffs),
        "n_items": len(diffs),
    }
    rng = random.Random(seed)
    boots = sorted(_mean([rng.choice(diffs) for _ in diffs]) for _ in range(BOOTSTRAP_SAMPLES))
    eff["ci95"] = [boots[int(0.025 * BOOTSTRAP_SAMPLES)], boots[int(0.975 * BOOTSTRAP_SAMPLES) - 1]]
    ea, eb = per_item(runs_a, True), per_item(runs_b, True)
    ed = [y - x for x, y in zip(ea, eb, strict=True) if x is not None and y is not None]
    if ed:
        eff["delta_evaluated_only"] = _mean(ed)
    if len(diffs) < FEW_ITEMS:
        findings.append(
            Finding(
                WARN,
                "FEW_ITEMS",
                f"only {len(diffs)} shared items; the pass-rate interval is not reliable",
                subject="item_pass_rate",
            )
        )
    elif eff["ci95"][0] <= 0 <= eff["ci95"][1]:
        findings.append(
            Finding(
                WARN,
                "EFFECT_WITHIN_NOISE",
                f"pass-rate delta {eff['delta']:+.3f}, 95% CI [{eff['ci95'][0]:+.3f}, {eff['ci95'][1]:+.3f}] "
                f"over {len(diffs)} items includes 0",
                subject="item_pass_rate",
            )
        )
    if ed and abs(eff["delta"]) > 1e-12 and (eff["delta"] > 0) != (eff["delta_evaluated_only"] > 0):
        findings.append(
            Finding(
                WARN,
                "SIGN_DEPENDS_ON_ERRORS",
                f"the pass-rate delta is {eff['delta']:+.3f} over all items but "
                f"{eff['delta_evaluated_only']:+.3f} over evaluated items only",
                subject="item_pass_rate",
            )
        )
    return eff
