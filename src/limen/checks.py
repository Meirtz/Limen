"""Checks on a single run record: did it finish, run the code it declared, read its treatment,
run its gates, and stay away from held-out data?"""

from __future__ import annotations

import datetime as _dt
import os
import re
from typing import Any

from . import fields
from .capture import NOT_EVALUATED, identity_token
from .findings import BLOCK, INFO, WARN, Finding
from .store import Store

GATE_NEVER_FAILED_CALLS = 20
NOT_EVALUATED_WARN = 0.10

# Commands that cannot read an experiment's environment variables in a way that matters.
_NON_CONSUMERS = frozenset(
    "git nvidia-smi uname hostname file ldconfig which whoami id date nproc lscpu free df du ls cat "
    "mkdir cp mv rm ln touch true false echo sleep".split()
)
_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
_REDIRECT = re.compile(r"\s*\d*>>?\s*(&\d+|\S+)|\s*<\s*\S+")


def _command(s: dict[str, Any]) -> list[str]:
    argv = list(s.get("argv") or [])
    if len(argv) >= 3 and os.path.basename(argv[0]) in _SHELLS and argv[1] == "-c":
        argv = argv[2:]
    if len(argv) == 1:  # os.system / shell=True string: ignore redirections, refuse compound commands
        cmd = _REDIRECT.sub("", argv[0])
        if re.search(r"[;&|`$()\n]", cmd):
            return argv
        argv = cmd.split()
    return argv


def children(subprocesses: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Child processes and workers that could have read files or variables (not `git`, `nvidia-smi`...)."""
    out = []
    for s in subprocesses or []:
        argv = _command(s)
        if argv and os.path.basename(argv[0]) in _NON_CONSUMERS:
            continue
        out.append(s)
    return out


def env_children(subprocesses: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Children that inherited the environment and could have read a variable the parent did not."""
    return [s for s in children(subprocesses) if s.get("env") == "inherited"]


def _real(p: str | None) -> str | None:
    return os.path.realpath(p) if p else None


def _in_output_dir(record: dict[str, Any], path: str) -> bool:
    """Is ``path`` under a directory the run wrote to through Python, or one named by its arguments?"""
    files = record.get("files") or {}
    reads = files.get("read") or {}
    written = files.get("written") or {}
    explicit = [w for w in written if not (reads.get(w) or {}).get("by")]
    dirs = {os.path.dirname(w) for w in explicit} | {w for w in explicit if (written[w] or {}).get("dir")}
    cwd = record.get("cwd") or ""
    values = [*list(record.get("argv") or [])[1:]]
    values += [v for v in (record.get("params") or {}).values() if isinstance(v, str)]
    for token in values:
        value = str(token).split("=")[-1]
        if value and not value.startswith("-"):
            dirs.add(os.path.abspath(os.path.join(cwd, value)))
    d = os.path.dirname(path)
    return any(d == x or d.startswith(x.rstrip(os.sep) + os.sep) for x in dirs if x and x != cwd)


def check_run(record: dict[str, Any], store: Store | None = None) -> list[Finding]:
    rid = record.get("id", "?")
    out: list[Finding] = []

    def add(sev: str, code: str, msg: str, subject: str = "", **ev: Any) -> None:
        out.append(Finding(sev, code, msg, subject=subject, run=rid, evidence=ev))

    declared = record.get("declared") or {}

    # -- did it finish? ---------------------------------------------------------------------
    status = record.get("status")
    if status == "running":
        add(BLOCK, "INCOMPLETE", "the record was never finalized: the process was killed outright or is still running")
        return out  # nothing else was captured
    if status != "ok":
        detail = record.get("exception") or record.get("signal") or f"exit code {record.get('exit_code')}"
        add(BLOCK, "RUN_FAILED", f"the run ended with status {status!r} ({detail}); its outputs are not results")

    # -- was the code that ran the code the project declares? --------------------------------
    for pkg, info in ((record.get("imports") or {}).get("packages") or {}).items():
        loaded, canonical = _real(info.get("loaded_from")), _real(info.get("canonical"))
        cands = info.get("candidates") or []
        if loaded and canonical and loaded != canonical:
            add(
                BLOCK,
                "SHADOWED",
                f"package {pkg!r} was imported from {loaded}, not from its source {canonical}",
                subject=pkg,
                loaded_from=loaded,
                canonical=canonical,
                candidates=cands,
            )
        elif len(cands) > 1:
            add(
                WARN,
                "AMBIGUOUS_IMPORT",
                f"{len(cands)} copies of {pkg!r} are importable from sys.path; the first one wins",
                subject=pkg,
                candidates=cands,
            )

    modules = record.get("modules") or {}
    main = record.get("main") or {}
    for n, m in modules.items():
        if m.get("shadows_stdlib"):
            add(
                BLOCK,
                "SHADOWS_STDLIB",
                f"module {n!r} from {m.get('path')} replaced the standard-library module",
                subject=n,
            )
    changed = [n for n, m in modules.items() if m.get("changed_during_run") or m.get("changed_before_run")]
    if main.get("changed_during_run"):
        changed.insert(0, "__main__")
    if changed:
        add(
            BLOCK,
            "CODE_CHANGED_DURING_RUN",
            f"{len(changed)} executed source file(s) changed while the run was going; the record keeps the "
            "bytes that were loaded, but anything imported or reloaded later may have used the new ones",
            modules=changed[:20],
        )
    edited = [n for n, m in modules.items() if m.get("edited_before_run")]
    if edited:
        add(
            WARN,
            "CODE_EDITED_BEFORE_RUN",
            f"{len(edited)} module(s) imported before the run was started were edited after this process started; "
            "if the edit came after the import, the code that ran is not what the record shows",
            modules=edited[:20],
        )
    external = [(n, m["path"]) for n, m in modules.items() if m.get("origin") == "external"]
    if main.get("origin") == "external":
        external.insert(0, ("__main__", main.get("path")))
    if external:
        add(
            WARN,
            "EXTERNAL_CODE",
            f"{len(external)} executed module(s) live outside the project's source roots (copies kept in the store)",
            modules=dict(external[:20]),
        )
    untracked = [n for n, m in modules.items() if m.get("git") == "untracked"]
    if main.get("git") == "untracked":
        untracked.insert(0, "__main__")
    if untracked:
        add(
            WARN,
            "UNTRACKED_CODE",
            f"{len(untracked)} executed project module(s) are not tracked by git",
            modules=untracked[:20],
        )
    modified = [n for n, m in modules.items() if m.get("git") == "modified"]
    if main.get("git") == "modified":
        modified.insert(0, "__main__")
    if modified:
        add(INFO, "MODIFIED_CODE", f"{len(modified)} executed module(s) differ from git HEAD", modules=modified[:20])

    if record.get("generated_code"):
        add(
            INFO,
            "GENERATED_CODE",
            f"{len(record['generated_code'])} module(s) were generated during the run (compiler caches, JIT kernels)"
            " and are not counted as code identity",
            modules=record["generated_code"][:20],
        )

    # -- what the record cannot see ----------------------------------------------------------
    kids = children(record.get("subprocesses"))
    if kids:
        add(
            WARN,
            "CHILD_PROCESSES",
            f"{sum(c.get('count', 1) for c in kids)} child process(es) or worker(s) ran; "
            "the files and variables they read are not in this record",
            commands=[" ".join(c["argv"][:6]) for c in kids[:8]],
        )
    env_kids = env_children(record.get("subprocesses"))
    if record.get("native_readers"):
        add(
            INFO,
            "NATIVE_READERS",
            "modules that read files natively were loaded; files they open themselves are not in this record",
            modules=record["native_readers"],
        )
    if record.get("env_tracked") is False:
        add(
            WARN,
            "ENV_NOT_OBSERVED",
            "os.environ had been replaced before the run, so environment reads were not recorded",
        )
    reads = (record.get("files") or {}).get("read") or {}
    newer = [p for p, v in reads.items() if v.get("by")]
    if newer:
        outside = [p for p in newer if reads[p].get("by") == "mtime" and not _in_output_dir(record, p)]
        add(
            WARN if outside else INFO,
            "INPUT_CHANGED_DURING_RUN",
            f"{len(newer)} file(s) the run read were created or modified after it started"
            + (
                " outside its output directories, possibly by another process"
                if outside
                else " (its own native writes)"
            )
            + "; they are not counted as inputs",
            files=(outside or newer)[:20],
        )

    # -- did the declared treatment reach the code? -----------------------------------------
    ident = fields.identity(record)
    env = record.get("env") or {}
    for spec in declared.get("treatment") or []:
        if spec.startswith("env:"):
            names = [k for k in env if fields.matches(spec, f"env:{k}")]
            set_first = [k for k in names if env[k].get("set_by_run")]
            if set_first:
                add(
                    BLOCK,
                    "TREATMENT_OVERRIDDEN",
                    f"the run set {', '.join(set_first)} itself before reading it, "
                    "so the declared treatment was not an input",
                    subject=spec,
                )
                continue
            rewritten = [k for k in names if env[k].get("set_after_read")]
            if rewritten:
                add(
                    WARN,
                    "TREATMENT_REWRITTEN",
                    f"the run read {', '.join(rewritten)} and then set it; later reads see the run's own value",
                    subject=spec,
                )
            if any(fields.matches(spec, f) for f in ident):
                continue
            if env_kids or record.get("env_bulk_read"):
                why = []
                if record.get("env_bulk_read"):
                    why.append(
                        f"the process copied os.environ in bulk ({', '.join(record.get('env_bulk_by') or ['?'])})"
                    )
                if env_kids:
                    why.append("child processes inherited the environment")
                add(
                    WARN,
                    "TREATMENT_UNVERIFIED",
                    "the process never read this variable by name; " + " and ".join(why),
                    subject=spec,
                )
            elif record.get("env_tracked") is not False:
                add(
                    BLOCK,
                    "TREATMENT_NOT_READ",
                    "the declared treatment variable was never read by the process, "
                    "so this arm cannot differ from its control",
                    subject=spec,
                )
        elif spec.startswith("param:") and not any(fields.matches(spec, f) for f in ident):
            add(
                BLOCK,
                "TREATMENT_NOT_READ",
                "the declared treatment parameter was never reported via limen.param()",
                subject=spec,
            )

    # -- gates ------------------------------------------------------------------------------
    gates = record.get("gates") or {}
    for name in declared.get("gates") or []:
        g = gates.get(name)
        if not g or not g.get("calls"):
            add(BLOCK, "GATE_NOT_EXECUTED", "a declared gate never ran, so nothing it guards was checked", subject=name)
    for name, g in gates.items():
        ctl = g.get("controls")
        if ctl and not ctl.get("ok", True):
            problems = []
            if ctl.get("negatives_passed"):
                problems.append("accepted a known-bad control")
            if ctl.get("positives_failed"):
                problems.append("rejected a known-good control")
            add(
                BLOCK,
                "GATE_CONTROLS_FAILED",
                "the gate " + " and ".join(problems),
                subject=name,
                negatives_passed=ctl.get("negatives_passed"),
                positives_failed=ctl.get("positives_failed"),
            )
        elif name in (declared.get("gates") or []) and not ctl and g.get("calls"):
            add(WARN, "GATE_UNCONTROLLED", "the gate ran without known-good/known-bad controls", subject=name)
        if (
            g.get("calls", 0) >= GATE_NEVER_FAILED_CALLS
            and not g.get("failed")
            and not g.get("errors")
            and not (ctl and ctl.get("negatives"))
        ):
            add(
                WARN,
                "GATE_NEVER_FAILED",
                f"the gate passed all {g['calls']} candidates and was never shown to reject anything",
                subject=name,
            )

    # -- outcomes ---------------------------------------------------------------------------
    outcomes = record.get("outcomes") or {}
    if outcomes:
        ne = [k for k, v in outcomes.items() if v.get("status") in NOT_EVALUATED]
        if len(ne) / len(outcomes) > NOT_EVALUATED_WARN:
            counts: dict[str, int] = {}
            for k in ne:
                s = outcomes[k]["status"]
                counts[s] = counts.get(s, 0) + 1
            add(WARN, "NOT_EVALUATED", f"{len(ne)}/{len(outcomes)} items were never evaluated", by_status=counts)
    if record.get("duplicate_outcomes"):
        add(
            WARN,
            "DUPLICATE_OUTCOMES",
            f"{record['duplicate_outcomes']} outcome(s) were reported twice; the last one was kept",
        )

    # -- held-out data ------------------------------------------------------------------------
    out.extend(check_holdout(record, store))
    return out


def _policy(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    pol = record.get("policy") or {}
    return list(pol.get("holdout") or []), list(pol.get("holdout_readers") or ["eval"])


def _is_holdout_path(record: dict[str, Any], path: str, ident: dict[str, Any] | None) -> bool:
    holdout, _ = _policy(record)
    roots = {h.rstrip(os.sep) for h in holdout} | {os.path.realpath(h).rstrip(os.sep) for h in holdout}
    forms = {os.path.abspath(path).rstrip(os.sep), os.path.realpath(path).rstrip(os.sep)}
    if ident and ident.get("realpath"):
        forms.add(str(ident["realpath"]).rstrip(os.sep))
    return any(f == r or f.startswith(r + os.sep) for f in forms for r in roots if r)


def _holdout_reads(record: dict[str, Any]) -> list[str]:
    reads = (record.get("files") or {}).get("read") or {}
    return [p for p, v in reads.items() if _is_holdout_path(record, p, v)]


def _time(s: Any) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


class _Lineage:
    """Which recorded runs wrote which content, and whether that content derives from held-out data."""

    def __init__(self, store: Store):
        self.by_token: dict[str, list[dict[str, Any]]] = {}
        for rec in store.all():
            for ident in ((rec.get("files") or {}).get("written") or {}).values():
                if ident.get("sha256") or ident.get("stat"):
                    self.by_token.setdefault(identity_token(ident), []).append(rec)
        self.memo: dict[str, list[str] | None] = {}

    def taint(self, record: dict[str, Any], depth: int = 0) -> list[str] | None:
        """A chain ``[run, ..., holdout file]`` if ``record`` read held-out data directly or through inputs."""
        rid = record.get("id", "?")
        if rid in self.memo:
            return self.memo[rid]
        self.memo[rid] = None  # break cycles
        direct = _holdout_reads(record)
        if direct:
            self.memo[rid] = [rid, direct[0]]
            return self.memo[rid]
        if depth >= 20:
            return None
        ended = _time(record.get("ended_at"))
        for ident in ((record.get("files") or {}).get("read") or {}).values():
            if ident.get("self_written") or not ident.get("size") or not (ident.get("sha256") or ident.get("stat")):
                continue
            token = identity_token(ident)
            chains: list[list[str]] = []
            clean = False
            for producer in self.by_token.get(token, []):
                if producer.get("id") == rid:
                    continue
                started = _time(producer.get("started_at"))
                if ended and started and started >= ended:
                    continue  # started after this run ended: cannot have produced what it read
                reads = (producer.get("files") or {}).get("read") or {}
                copied_from = [p for p, v in reads.items() if not v.get("self_written") and identity_token(v) == token]
                if copied_from:  # the producer passed this exact content through; judge its source instead
                    held = [p for p in copied_from if _is_holdout_path(producer, p, reads[p])]
                    if held:
                        chains.append([rid, producer.get("id", "?"), held[0]])
                    else:
                        clean = True
                    continue
                chain = self.taint(producer, depth + 1)
                if chain:
                    chains.append([rid, *chain])
                else:
                    clean = True
            if chains and not clean:  # identical bytes also produced without held-out data carry none of it
                self.memo[rid] = chains[0]
                return self.memo[rid]
        return None


def check_holdout(record: dict[str, Any], store: Store | None = None) -> list[Finding]:
    role = (record.get("declared") or {}).get("role")
    _, readers = _policy(record)
    if role in readers:
        return []
    rid = record.get("id", "?")
    direct = _holdout_reads(record)
    if direct:
        return [
            Finding(
                BLOCK,
                "HOLDOUT_READ",
                f"a run with role {role!r} read held-out data (allowed readers: {', '.join(readers)})",
                subject=direct[0],
                run=rid,
                evidence={"files": direct[:20]},
            )
        ]
    if store is None:
        return []
    chain = _Lineage(store).taint(record)
    if chain:
        return [
            Finding(
                BLOCK,
                "HOLDOUT_DERIVED",
                f"a run with role {role!r} read files derived from held-out data",
                subject=chain[-1],
                run=rid,
                evidence={"chain": chain},
            )
        ]
    return []
