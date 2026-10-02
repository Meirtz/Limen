"""``aosr``: boot an empty image, grow it on a task stream, evaluate frozen images, inspect them."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from aosr import config

if TYPE_CHECKING:
    from aosr.engine import Engine
    from aosr.llm import Model
    from aosr.sandbox import Sandbox
    from aosr.store import Store

ARC_REPO = "https://github.com/fchollet/ARC-AGI.git"
ARC_COMMIT = "399030444e0ab0cc8b4e199870fb20b863846f34"


def _store(args: argparse.Namespace) -> Store:
    from aosr.store import Store

    return Store(Path(args.store).expanduser())


def _sandbox() -> Sandbox:
    from aosr.sandbox import Sandbox

    return Sandbox(config.sandbox_cache_path())


def _model(spec: str, ledger_path: Path) -> Model:
    from aosr.llm import CallCache, Ledger, make_model

    return make_model(spec, cache=CallCache(config.llm_cache_path()), ledger=Ledger(ledger_path))


def _engine(args: argparse.Namespace, store: Store) -> Engine:
    if args.world == "arc":
        from aosr.engine import ArcEngine

        return ArcEngine(store, _sandbox())
    from aosr.apps import AppEngine

    return AppEngine(store, data_root=config.appworld_root())


def cmd_fetch(args: argparse.Namespace) -> int:
    dest = config.data_dir() / "ARC-AGI"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", ARC_REPO, str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", "origin", ARC_COMMIT], check=False)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", ARC_COMMIT], check=True)
    print(f"ARC-AGI at {ARC_COMMIT[:12]} in {dest}")
    return 0


def cmd_boot(args: argparse.Namespace) -> int:
    from aosr.grow import boot

    print(boot(_store(args)))
    return 0


def cmd_grow(args: argparse.Namespace) -> int:
    from aosr.grow import GrowConfig, grow
    from aosr.kernel import Settings

    store = _store(args)
    engine = _engine(args, store)
    out = Path(args.out).expanduser()
    stream = engine.load("stream", args.n)
    cps = tuple(int(x) for x in args.checkpoints.split(",") if x) if args.checkpoints else ()
    model = _model(args.model, out / "ledger.jsonl")
    engineer = _model(args.engineer, out / "ledger.jsonl") if args.epoch_every else None
    cfg = GrowConfig(
        batch=args.batch,
        settings=Settings(budget=args.budget),
        checkpoints=cps,
        run=args.run,
        epoch_every=args.epoch_every,
        engineer=engineer,
        window=args.window,
        margin=args.margin,
    )
    print(grow(store, store.resolve(args.start), stream, engine, model, out, cfg))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from aosr.evaluate import Arm, run_arm, summarize
    from aosr.kernel import Settings

    store = _store(args)
    engine = _engine(args, store)
    out = Path(args.out).expanduser()
    tasks = engine.load(args.split, args.n, args.offset)
    settings = Settings(budget=args.budget, context=args.context, sample_offset=args.sample_offset)
    arm = Arm(args.arm, store.resolve(args.image), _model(args.model, out.with_suffix(".ledger.jsonl")), settings)
    rows = run_arm(store, arm, tasks, engine, out)
    print(json.dumps(summarize({"arm": args.arm, "budget": args.budget}, rows)))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from aosr.evaluate import compare, load_arm, summarize

    arms = {p: load_arm(Path(p)) for p in args.files}
    print("| arm | n | budget | solved | rate | by budget | calls | USD | USD/solve | with library | presolved |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for h, rows in arms.values():
        s = summarize(h, rows)
        print(
            f"| {s['arm']} | {s['n']} | {s['budget']} | {s['solved']} | {s['rate']:.3f} | {s['by_budget'][1:]} | "
            f"{s['calls']} | {s['usd']:.2f} | {s['usd_per_solve']} | {s['solved_with_library']} | {s['presolved']} |"
        )
    for spec in args.compare or []:
        a, b = spec.split(",")
        (pa, ba), (pb, bb) = (x.rsplit("@", 1) for x in (a, b))
        print(f"{Path(pb).stem}@{bb} minus {Path(pa).stem}@{ba}: {compare(arms[pa], arms[pb], int(ba), int(bb))}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from aosr.store import Image

    store = _store(args)
    digest = store.resolve(args.image)
    image = Image(store, store.head(digest))
    print(json.dumps({"image": digest, "note": image.head.note, **image.summary()}, indent=1))
    depths = image.depths()
    for n, e in image.head.caps.items():
        c = image.cap(n)
        print(f"{e.admitted:4d} {e.status:7s} uses={e.uses:<3d} depth={depths[n]} {c['sig']}: {c['doc']}")
        if args.source:
            print("    " + c["src"].replace("\n", "\n    "))
    return 0


def cmd_knockout(args: argparse.Namespace) -> int:
    from aosr.store import Image

    store = _store(args)
    digest = store.resolve(args.image)
    image = Image(store, store.head(digest))
    if args.admitted_before is not None:
        names = {n for n, e in image.head.caps.items() if e.admitted < args.admitted_before}
    else:
        names = set(args.names.split(","))
    head = image.knockout(names)
    head.parent = digest
    new = store.commit(head)
    if args.ref:
        store.set_ref(args.ref, new)
    print(new)
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    store = _store(args)
    for d in store.lineage(store.resolve(args.image)):
        h = store.get(d)
        print(f"{d[:12]} seq={h['seq']:<4d} caps={len(h['caps']):<4d} programs={len(h['programs']):<4d} {h['note']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aosr", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    store_default = str(config.home() / "store")

    def add(name: str, fn: Callable[[argparse.Namespace], int], help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(fn=fn)
        if name != "fetch":
            sp.add_argument("--store", default=store_default)
        return sp

    add("fetch", cmd_fetch, "fetch ARC-AGI-1 at the pinned commit")
    add("boot", cmd_boot, "create head_0, the empty image")
    g = add("grow", cmd_grow, "grow an image over the stream split")
    g.add_argument("--world", default="arc", choices=["arc", "appworld"])
    g.add_argument("--from", dest="start", default="head_0")
    g.add_argument("--run", required=True)
    g.add_argument("--n", type=int, default=300)
    g.add_argument("--model", default="haiku-nothink")
    g.add_argument("--budget", type=int, default=2)
    g.add_argument("--batch", type=int, default=10)
    g.add_argument("--checkpoints", default="")
    g.add_argument("--epoch-every", type=int, default=0, help="kernel evolution every N stream tasks")
    g.add_argument("--engineer", default="haiku-think")
    g.add_argument("--window", type=int, default=20)
    g.add_argument("--margin", type=int, default=2)
    g.add_argument("--out", required=True)
    e = add("eval", cmd_eval, "evaluate a frozen image on held-out tasks")
    e.add_argument("--world", default="arc", choices=["arc", "appworld"])
    e.add_argument("--image", required=True)
    e.add_argument("--arm", required=True)
    e.add_argument("--split", default="dev")
    e.add_argument("--n", type=int, default=None)
    e.add_argument("--offset", type=int, default=0)
    e.add_argument("--model", default="haiku-nothink")
    e.add_argument("--budget", type=int, default=2)
    e.add_argument("--context", default="full", choices=["full", "helpers", "precedents", "none"])
    e.add_argument("--sample-offset", type=int, default=1000)
    e.add_argument("--out", required=True)
    r = sub.add_parser("report", help="summarize evaluated arms and compare them")
    r.set_defaults(fn=cmd_report)
    r.add_argument("files", nargs="+")
    r.add_argument("--compare", action="append", help="A@budget,B@budget (file paths)")
    s = add("show", cmd_show, "summarize an image and list its capabilities")
    s.add_argument("image")
    s.add_argument("--source", action="store_true")
    k = add("knockout", cmd_knockout, "derive an image without some capabilities and their dependents")
    k.add_argument("image")
    k.add_argument("--names", default="")
    k.add_argument("--admitted-before", type=int, default=None)
    k.add_argument("--ref", default="")
    lg = add("lineage", cmd_lineage, "list an image's ancestors")
    lg.add_argument("image")
    args = p.parse_args(argv)
    rc: int = args.fn(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
