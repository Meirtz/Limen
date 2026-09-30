"""Command line: ``limen run | check | compare | trace | leak | ls | show``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__, checks, findings, trace
from .compare import compare
from .config import Config, load
from .fields import SpecError, parse_spec
from .leak import overlap, read_ids
from .store import RecordNotFound, Store

EXIT_OK, EXIT_BLOCKED, EXIT_USAGE = 0, 1, 2


def _protocol(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise SpecError(f"--protocol expects KEY=VALUE, got {pair!r}")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def _select(store: Store, refs: list[str]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        for part in ref.split(","):
            for r in store.select(part):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    runs.append(r)
    return runs


def _label(r: dict[str, Any]) -> str:
    d = r.get("declared") or {}
    return ":".join(x for x in (d.get("name"), d.get("arm")) if x) or "-"


def _config_for(command: list[str]) -> Config:
    """Configuration for the project the script belongs to (not the cwd), as ``python script.py`` resolves it."""
    if command and command[0] != "-m" and os.path.exists(command[0]):
        target = Path(command[0]).resolve()
        return load(target if target.is_dir() else target.parent)
    return load()


def cmd_run(a: argparse.Namespace) -> int:
    from .runner import run

    command = a.command
    for spec in a.treatment:
        parse_spec(spec)
    cfg = _config_for(command)

    def summary(record: dict[str, Any]) -> None:
        if a.quiet:
            return
        found = checks.check_run(record, Store(cfg.store))
        n_block = sum(f.severity == findings.BLOCK for f in found)
        n_warn = sum(f.severity == findings.WARN for f in found)
        note = f"; {n_block} blocking, {n_warn} warnings (limen check {record['id']})" if found else ""
        where = "" if cfg.store == Path.cwd().resolve() / ".limen" else f" in {cfg.store}"
        print(f"limen: recorded run {record['id']}{where} [{record['status']}]{note}", file=sys.stderr)

    code, _ = run(
        cfg,
        command,
        defer=True,
        on_finish=summary,
        name=a.name,
        arm=a.arm,
        role=a.role,
        treatment=a.treatment,
        gates=a.gate,
        protocol=_protocol(a.protocol),
    )
    return code


def cmd_check(a: argparse.Namespace) -> int:
    store = Store(load().store)
    runs = _select(store, a.refs or ["latest"])
    found = []
    for r in runs:
        found.extend(checks.check_run(r, store))
    if a.json:
        print(findings.to_json(found, runs=[r["id"] for r in runs]))
    else:
        for r in runs:
            print(findings.safe(f"run {r['id']} {_label(r)} [{r.get('status')}]"))
        print(findings.render(found, verbose=a.verbose))
    return EXIT_BLOCKED if findings.blocking(found) else EXIT_OK


def cmd_compare(a: argparse.Namespace) -> int:
    cfg = load()
    store = Store(cfg.store)
    runs_a, runs_b = _select(store, [a.a]), _select(store, [a.b])
    comp = compare(
        runs_a,
        runs_b,
        treatment=a.treatment or None,
        waive=[*cfg.waive, *a.waive],
        metrics=a.metric or None,
        store=store,
        check_runs=not a.no_run_checks,
    )
    if a.json:
        print(
            findings.to_json(
                comp.findings,
                a=comp.runs_a,
                b=comp.runs_b,
                treatment=comp.treatment,
                effects=comp.effects,
                explained=comp.explained,
            )
        )
    else:
        print(f"A: {len(runs_a)} run(s) {sorted({_label(r) for r in runs_a})}")
        print(f"B: {len(runs_b)} run(s) {sorted({_label(r) for r in runs_b})}")
        print(f"treatment: {', '.join(comp.treatment) or '(none declared)'}")
        for diffs in comp.explained.values():
            for fld, (va, vb) in diffs.items():
                print(findings.safe(f"  {fld}: A={va} | B={vb}"))
        for e in comp.effects:
            ci = e.get("ci95")
            ci_s = f"  95% CI [{ci[0]:+.4g}, {ci[1]:+.4g}]" if ci else ""
            print(f"effect {e['metric']}: A={e['a_mean']:.4g} B={e['b_mean']:.4g} delta={e['delta']:+.4g}{ci_s}")
        print(findings.render(comp.findings, verbose=a.verbose))
    return EXIT_BLOCKED if findings.blocking(comp.findings) else EXIT_OK


def cmd_trace(a: argparse.Namespace) -> int:
    cfg = load()
    node, found = trace.trace(a.path, Store(cfg.store), depth=a.depth, max_bytes=cfg.max_hash_bytes)
    print(findings.safe(trace.render(node)).replace("\\x0a", "\n"))
    if found:
        print(findings.render(found, verbose=a.verbose))
    return EXIT_BLOCKED if findings.blocking(found) else EXIT_OK


def cmd_leak(a: argparse.Namespace) -> int:
    held = read_ids(a.holdout, a.holdout_key or a.key)
    if not held:
        raise ValueError(f"{a.holdout}: no held-out ids found")
    total = 0
    for path in a.data:
        ids = read_ids(path, a.key)
        if not ids:
            raise ValueError(f"{path}: no ids found")
        hits = overlap(held, ids)
        total += len(hits)
        status = "LEAK " if hits else "ok   "
        sample = f": {', '.join(sorted(hits)[:10])}{' ...' if len(hits) > 10 else ''}" if hits else ""
        print(f"{status}{path}: {len(hits)} of {len(held)} held-out items{sample}")
    return EXIT_BLOCKED if total else EXIT_OK


def cmd_ls(a: argparse.Namespace) -> int:
    for r in Store(load().store).all():
        if a.name and (r.get("declared") or {}).get("name") != a.name:
            continue
        main = (r.get("main") or {}).get("path") or " ".join(str(x) for x in (r.get("argv") or [])[:1])
        role = (r.get("declared") or {}).get("role") or "-"
        print(findings.safe(f"{r['id']}  {_label(r):24} {role!s:8} {r.get('status', '?')!s:11} {main}"))
    return EXIT_OK


def cmd_show(a: argparse.Namespace) -> int:
    for r in _select(Store(load().store), [a.ref]):
        print(json.dumps(r, indent=1, sort_keys=True, default=str))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="limen", description="Check that an experiment ran the way it claims.")
    p.add_argument("--version", action="version", version=f"limen {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "run",
        help="run a Python script or module and record what it executed",
        usage="limen run [options] [--] (script.py | -m module) [args ...]",
    )
    r.add_argument("--name", help="experiment name (groups runs)")
    r.add_argument("--arm", help="arm label, e.g. control / treatment")
    r.add_argument("--role", help="what the run does: eval, train, generate, tune, select, ...")
    r.add_argument(
        "--treatment",
        action="append",
        default=[],
        metavar="SPEC",
        help="the input this arm changes, e.g. env:WIKI_ROOT, arg:--lr, file:data/wiki/, module:pkg.x",
    )
    r.add_argument("--gate", action="append", default=[], metavar="NAME", help="a gate that must run")
    r.add_argument(
        "--protocol",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a protocol field both arms must share, e.g. tolerance=1e-4",
    )
    r.add_argument("-q", "--quiet", action="store_true", help="do not print the run summary")
    r.set_defaults(fn=cmd_run, command=[])

    c = sub.add_parser("check", help="check recorded runs")
    c.add_argument("refs", nargs="*", help="run id, id prefix, name, name:arm, or latest (default)")
    c.add_argument("--json", action="store_true")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(fn=cmd_check)

    m = sub.add_parser("compare", help="check that arm B differs from arm A only in the treatment")
    m.add_argument("a", help="control runs: id, name, name:arm (comma-separated for several)")
    m.add_argument("b", help="treatment runs")
    m.add_argument("--treatment", action="append", default=[], metavar="SPEC", help="override the declared treatment")
    m.add_argument("--waive", action="append", default=[], metavar="SPEC", help="a difference that does not matter")
    m.add_argument("--metric", action="append", default=[], help="metrics to estimate (default: all shared)")
    m.add_argument("--no-run-checks", action="store_true", help="skip per-run checks")
    m.add_argument("--json", action="store_true")
    m.add_argument("-v", "--verbose", action="store_true")
    m.set_defaults(fn=cmd_compare)

    t = sub.add_parser("trace", help="which recorded run produced this file, from what")
    t.add_argument("path")
    t.add_argument("--depth", type=int, default=6)
    t.add_argument("-v", "--verbose", action="store_true")
    t.set_defaults(fn=cmd_trace)

    lk = sub.add_parser("leak", help="find held-out item ids inside training or tuning files")
    lk.add_argument("--holdout", required=True, help="file listing held-out items")
    lk.add_argument("--key", help="id field in the data files (dotted for nesting)")
    lk.add_argument("--holdout-key", help="id field in the holdout file, if different")
    lk.add_argument("data", nargs="+")
    lk.set_defaults(fn=cmd_leak)

    ls = sub.add_parser("ls", help="list recorded runs")
    ls.add_argument("--name")
    ls.set_defaults(fn=cmd_ls)

    sh = sub.add_parser("show", help="print run records as JSON")
    sh.add_argument("ref")
    sh.set_defaults(fn=cmd_show)
    return p


_RUN_VALUE_OPTS = {"--name", "--arm", "--role", "--treatment", "--gate", "--protocol"}


def _split_run(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``run`` arguments into Limen's options and the command to execute.

    The command starts at ``--``, at ``-m``, or at the first token that is not a Limen option,
    and everything after it belongs to the executed program.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return argv[:i], argv[i + 1 :]
        if tok == "-m":
            return argv[:i], argv[i:]
        if tok in _RUN_VALUE_OPTS:
            i += 2
            continue
        if tok.split("=", 1)[0] in _RUN_VALUE_OPTS or tok in ("-q", "--quiet", "-h", "--help"):
            i += 1
            continue
        return argv[:i], argv[i:]
    return argv, []


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if argv[:1] == ["run"]:
        opts, command = _split_run(argv[1:])
        argv = ["run", *opts]
    a = parser.parse_args(argv)
    if a.cmd == "run":
        if not command:
            parser.error("run: nothing to run (expected script.py or -m module)")
        a.command = command
    try:
        return int(a.fn(a))
    except (RecordNotFound, SpecError, ValueError, OSError) as e:
        print(f"limen: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
